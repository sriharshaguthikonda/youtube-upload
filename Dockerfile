FROM python:3.12-slim

ENV PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1 \
    YOUTUBE_UPLOAD_HOME=/config

WORKDIR /src
COPY pyproject.toml README.md ./
COPY youtube_upload ./youtube_upload
RUN pip install . \
    && useradd --create-home python \
    && mkdir -p /config /data \
    && chown python /config /data

USER python
WORKDIR /data
# Tokens live here. Sign in once on a machine with a browser, then mount the
# resulting config directory:  docker run -v "$PWD/cfg:/config" -v "$PWD:/data" ...
VOLUME ["/config"]
ENTRYPOINT ["youtube-upload"]
