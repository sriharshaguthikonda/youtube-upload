"""Randomised crash/fault recovery: duplicates and corruption must be impossible.

Each scenario kills the "process" a random number of times at random points (before or after the
server committed a chunk), sprinkles transient HTTP errors and connection resets, restarts with
brand-new objects over the same state directory, and finally lets the upload finish.
Invariants (checked every time):
  * exactly one video exists on the channel for the file;
  * the bytes the server received are identical to the file;
  * no resumable-session record is left behind;
  * the ledger knows the video.
Seeds are fixed, so any failure is reproducible.
"""
import random

import pytest

from tests.fake_youtube import FakeYouTubeServer, SimulatedCrash
from tests.helpers import CHUNK, make_options, sha256_of, write_mp4
from youtube_upload import report, uploader

SEEDS = list(range(40))


def new_process(server, state, clock, *argv):
    options = make_options(*argv)
    return uploader.AccountUploader(
        server.client(), options, "work", state, log=lambda m: None,
        sleep=clock.sleep, monotonic=clock.monotonic)


@pytest.mark.parametrize("seed", SEEDS)
def test_random_crashes_and_faults_never_duplicate_or_corrupt(seed, tmp_path, clock):
    rnd = random.Random(seed)
    chunks = rnd.randint(2, 9)
    size = chunks * CHUNK + rnd.randint(0, CHUNK - 1)
    video = write_mp4(tmp_path / "v.mp4", size=size, seed="s%d" % seed)
    server = FakeYouTubeServer()
    state = tmp_path / "state"
    extra = ["--skip-if-exists", "hash"] if rnd.random() < 0.7 else []

    crashes = rnd.randint(0, 4)
    outcomes = []
    for attempt in range(crashes + 1):
        # arm 0-2 transient faults and (except on the last attempt) one process death
        for _ in range(rnd.randint(0, 2)):
            kind = rnd.choice(["503", "429", "reset"])
            if kind == "503":
                server.inject("PUT", "/session/", status=503)
            elif kind == "429":
                server.inject("PUT", "/session/", status=429, headers={"retry-after": "2"})
            else:
                server.inject("PUT", "/session/", exc=ConnectionResetError("reset"))
        if attempt < crashes:
            at = rnd.randint(1, chunks) * CHUNK
            server.inject("PUT", "/session/", exc=SimulatedCrash(), after_bytes=min(at, size),
                          after_commit=rnd.random() < 0.7)
        up = new_process(server, state, clock, *extra)
        try:
            result = up.upload_one(str(video), 0, 1)
            outcomes.append(result)
        except SimulatedCrash:
            outcomes.append(None)       # the process died; the next iteration is a restart
        server.faults.clear()           # a restarted process starts on a healthy network

    if not outcomes or outcomes[-1] is None:
        outcomes.append(new_process(server, state, clock, *extra).upload_one(str(video), 0, 1))
    final = outcomes[-1]

    assert final.status in (report.UPLOADED, report.SKIPPED), (seed, final.message)
    assert final.succeeded
    assert len(server.videos) == 1, "duplicate or missing video (seed %d)" % seed
    (video_id,) = server.videos
    assert server.uploaded_sha256[video_id] == sha256_of(video), "corrupted bytes (seed %d)" % seed
    assert final.video_id == video_id
    sessions = list((state / "sessions").glob("*.json")) if (state / "sessions").exists() else []
    assert sessions == [], "stale session records left (seed %d)" % seed
    ledger = new_process(server, state, clock, *extra).ledger
    assert ledger.get(sha256_of(video))["video_id"] == video_id


@pytest.mark.parametrize("seed", range(10))
def test_many_files_with_random_crashes_each_uploaded_exactly_once(seed, tmp_path, clock):
    rnd = random.Random(1000 + seed)
    files = [write_mp4(tmp_path / ("f%d.mp4" % i), size=rnd.randint(2, 5) * CHUNK + 17, seed="m%d-%d" % (seed, i))
             for i in range(4)]
    server = FakeYouTubeServer()
    state = tmp_path / "state"
    pending = list(files)
    guard = 0
    while pending and guard < 40:
        guard += 1
        if rnd.random() < 0.6:
            server.inject("PUT", "/session/", exc=SimulatedCrash(),
                          after_bytes=rnd.randint(1, 4) * CHUNK, after_commit=rnd.random() < 0.5)
        up = new_process(server, state, clock, "--skip-if-exists", "hash")
        try:
            for path in list(pending):
                result = up.upload_one(str(path), 0, 1)
                assert result.succeeded, result.message
                pending.remove(path)
        except SimulatedCrash:
            pass
        server.faults.clear()
    assert pending == []
    assert len(server.videos) == len(files)
    received = sorted(server.uploaded_sha256.values())
    assert received == sorted(sha256_of(f) for f in files)
