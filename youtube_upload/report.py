"""What actually happened in a run: per account, per video (#6, #9).

The CLI used to exit 0 even if every upload failed. The report is the single
source of truth for the summary, the exit code and the GUI's final message.
"""
UPLOADED = "uploaded"
SKIPPED = "skipped"            # already on the channel; nothing uploaded
FAILED = "failed"
NOT_ATTEMPTED = "not_attempted"
CANCELLED = "cancelled"

EXIT_OK = 0
EXIT_OPTIONS = 2
EXIT_REQUEST = 3
EXIT_AUTH = 4
EXIT_CANCELLED = 6
EXIT_PARTIAL = 7


class VideoResult(object):
    def __init__(self, account, path):
        self.account = account
        self.path = str(path)
        self.status = NOT_ATTEMPTED
        self.video_id = None
        self.url = None
        self.message = ""
        self.exit_code = EXIT_REQUEST   # used only when the status is a failure
        self.abort_account = False      # True: sign-in/quota/retry failure, stop this account
        self.warnings = []              # informational (title was changed, ...)
        self.problems = []              # requested follow-ups that failed (thumbnail, playlist)

    @property
    def succeeded(self):
        return self.status in (UPLOADED, SKIPPED) and not self.problems


class RunReport(object):
    def __init__(self):
        self.results = []
        self.account_errors = {}        # label -> (message, exit code)
        self.cancelled = False

    def add(self, result):
        self.results.append(result)
        return result

    def account_failed(self, label, message, exit_code):
        self.account_errors[label or "default"] = (message, exit_code)

    @property
    def ok(self):
        return (not self.cancelled and not self.account_errors
                and all(r.succeeded for r in self.results))

    def count(self, status):
        return sum(1 for r in self.results if r.status == status)

    @property
    def video_ids(self):
        return [r.video_id for r in self.results if r.video_id and r.status in (UPLOADED, SKIPPED)]

    def exit_code(self):
        if self.cancelled:
            return EXIT_CANCELLED
        if self.ok:
            return EXIT_OK
        if any(r.succeeded for r in self.results):
            return EXIT_PARTIAL
        codes = [r.exit_code for r in self.results if r.status in (FAILED, NOT_ATTEMPTED)]
        codes += [code for _, code in self.account_errors.values()]
        problems = [r for r in self.results if r.problems]
        if problems and not codes:
            return EXIT_PARTIAL
        # authentication problems outrank request problems, which outrank option problems
        for code in (EXIT_AUTH, EXIT_REQUEST, EXIT_OPTIONS):
            if code in codes:
                return code
        return EXIT_REQUEST

    def summary_lines(self):
        lines = []
        for label, (message, _) in self.account_errors.items():
            lines.append("ACCOUNT {0}: {1}".format(label, message))
        for r in self.results:
            tag = {UPLOADED: "UPLOADED", SKIPPED: "SKIPPED (already on channel)",
                   FAILED: "FAILED", NOT_ATTEMPTED: "NOT ATTEMPTED",
                   CANCELLED: "CANCELLED"}[r.status]
            head = "[{0}] {1}: {2}".format(r.account or "default", r.path, tag)
            if r.url:
                head += " -> " + r.url
            if r.message:
                head += " ({0})".format(r.message)
            lines.append(head)
            for problem in r.problems:
                lines.append("    PROBLEM: {0}".format(problem))
            for warning in r.warnings:
                lines.append("    note: {0}".format(warning))
        done = self.count(UPLOADED)
        skipped = self.count(SKIPPED)
        failed = self.count(FAILED) + self.count(NOT_ATTEMPTED) + self.count(CANCELLED)
        lines.append("Summary: {0} uploaded, {1} skipped, {2} failed or not attempted{3}.".format(
            done, skipped, failed,
            ", {0} account error(s)".format(len(self.account_errors)) if self.account_errors else ""))
        return lines
