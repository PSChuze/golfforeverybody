"""The update notice in status.json (mingol/updates.py) against a fake GitHub API."""
import json
import os
import tempfile
import unittest
from unittest import mock

from mingol import updates

REPO = "example/golf"
OLD = "a" * 40
NEW = "b" * 40
MID = "c" * 40
MERGE = "d" * 40


def commit(sha, date, message, parents=1):
    return {"sha": sha, "parents": [{"sha": "0" * 40}] * parents,
            "commit": {"message": message, "committer": {"date": date}}}


class FakeGitHub:
    """Answers the three API paths the check asks, from a table; records them."""

    def __init__(self, routes=None, down=False):
        self.routes = routes if routes is not None else {
            "/repos/%s/releases/latest" % REPO: (200, {"tag_name": "v1.1.0"}),
            "/repos/%s/commits/v1.1.0" % REPO: (200, commit(NEW, "2025-03-02T10:00:00+01:00", "Release")),
            "/repos/%s/compare/%s...%s" % (REPO, OLD, NEW): (200, {"status": "ahead", "ahead_by": 3, "commits": [
                commit(MID, "2025-03-01T08:00:00Z", "play: tidy rooms\n\nlonger text"),
                commit(MERGE, "2025-03-01T09:00:00Z", "Merge branch x", parents=2),
                commit(NEW, "2025-03-02T09:00:00Z", "gate: news length")]}),
        }
        self.down = down
        self.asked = []

    def __call__(self, url):
        path = url[len(updates.API):]
        self.asked.append(path)
        if self.down:
            raise OSError("network unreachable")
        return self.routes.get(path, (404, {"message": "Not Found"}))


class UpdateObject(unittest.TestCase):
    def check(self, revision, gh, built="2025-02-01T00:00:00Z"):
        return updates.UpdateCheck(REPO, revision, built, fetch=gh).get(wait=5)

    def test_available(self):
        got = self.check(OLD, FakeGitHub())
        self.assertEqual(got["update"], "available")
        self.assertIsNone(got["why"])
        self.assertEqual(got["behind"], 3)
        self.assertEqual(got["running"], {"commit": OLD, "built_at": "2025-02-01T00:00:00Z"})
        self.assertEqual(got["latest"], {"commit": NEW, "date": "2025-03-02T09:00:00Z"})
        # newest first, no merge commits, first line only
        self.assertEqual(got["changes"], [
            {"commit": NEW, "date": "2025-03-02T09:00:00Z", "subject": "gate: news length"},
            {"commit": MID, "date": "2025-03-01T08:00:00Z", "subject": "play: tidy rooms"}])
        self.assertEqual(got["how"], "Run Golf Server Setup and choose Update.")
        json.dumps(got)

    def test_current(self):
        gh = FakeGitHub()
        got = self.check(NEW[:12], gh)
        self.assertEqual((got["update"], got["behind"], got["changes"]), ("current", 0, []))
        self.assertFalse(any("/compare/" in p for p in gh.asked))    # same commit: nothing to compare

    def test_ahead_of_the_release_is_current(self):
        gh = FakeGitHub()
        gh.routes["/repos/%s/compare/%s...%s" % (REPO, MID, NEW)] = (200, {"status": "behind", "ahead_by": 0,
                                                                          "commits": []})
        self.assertEqual(self.check(MID, gh)["update"], "current")

    def test_changes_capped(self):
        gh = FakeGitHub()
        many = [commit("%040x" % i, "2025-01-01T00:00:00Z", "change %d" % i) for i in range(80)]
        gh.routes["/repos/%s/compare/%s...%s" % (REPO, OLD, NEW)] = (200, {"ahead_by": 80, "commits": many})
        got = self.check(OLD, gh)
        self.assertEqual((got["behind"], len(got["changes"])), (80, updates.MAX_CHANGES))
        self.assertEqual(got["changes"][0]["subject"], "change 79")

    def test_offline_is_unknown(self):
        got = self.check(OLD, FakeGitHub(down=True))
        self.assertEqual(got["update"], "unknown")
        self.assertEqual(got["why"], "GitHub did not answer (OSError)")
        self.assertIsNone(got["behind"])
        self.assertEqual(got["changes"], [])
        self.assertEqual(got["running"]["commit"], OLD)

    def test_unknown_reasons(self):
        gh = FakeGitHub()
        got = self.check("", gh, built="")
        self.assertEqual(got["update"], "unknown")
        self.assertIn("does not know its commit", got["why"])
        self.assertEqual(got["latest"]["commit"], NEW)            # the release is still shown
        self.assertEqual(got["running"], {"commit": "unknown", "built_at": "unknown"})
        self.assertIn("not in", self.check("e" * 40, gh)["why"])
        self.assertIn("no release", self.check(OLD, FakeGitHub(routes={}))["why"])
        limited = FakeGitHub(routes={"/repos/%s/releases/latest" % REPO: (403, {})})
        self.assertIn("too many requests", self.check(OLD, limited)["why"])
        self.assertIn("owner/name", updates.UpdateCheck("nope", OLD, fetch=FakeGitHub()).get(wait=5)["why"])

    def test_cached(self):
        gh = FakeGitHub()
        now = [1000.0]
        uc = updates.UpdateCheck(REPO, OLD, fetch=gh, clock=lambda: now[0])
        uc.get(wait=5)
        asked = len(gh.asked)
        now[0] += updates.TTL - 1
        uc.get(wait=5)
        self.assertEqual(len(gh.asked), asked)
        now[0] += 2
        uc.get(wait=5)
        uc.thread and uc.thread.join(5)
        self.assertGreater(len(gh.asked), asked)

    def test_never_blocks(self):
        import threading
        gate = threading.Event()

        def slow(url):
            gate.wait(5)
            raise OSError("late")
        try:
            got = updates.UpdateCheck(REPO, OLD, fetch=slow).get(wait=0.05)
            self.assertEqual(got["update"], "unknown")
            self.assertIn("still asking", got["why"])
        finally:
            gate.set()


class Revision(unittest.TestCase):
    def test_file_and_environment(self):
        d = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MINGOL_REVISION", None)
            os.environ.pop("MINGOL_BUILT_AT", None)
            self.assertEqual(updates.read_revision(d), ("", ""))
            updates.write_revision(d, NEW.upper(), "2025-03-02T09:00:00Z", tag="v1.1.0")
            self.assertEqual(updates.read_revision(d), (NEW, "2025-03-02T09:00:00Z"))
            os.environ["MINGOL_REVISION"] = OLD
            self.assertEqual(updates.read_revision(d)[0], OLD)

    def test_off_when_the_repo_is_empty(self):
        with mock.patch.dict(os.environ, {"MINGOL_UPDATE_REPO": ""}):
            self.assertIsNone(updates.from_env())
        with mock.patch.dict(os.environ, {"MINGOL_UPDATE_REPO": REPO}):
            self.assertEqual(updates.from_env().repo, REPO)


if __name__ == "__main__":
    unittest.main()
