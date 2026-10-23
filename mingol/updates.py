"""Is a newer release out? The update object in the admin panel's status.json (mingol/adminport.py).

The running build's commit comes from server-revision.json, which packaging/build_setup.py and
setup.py's update write next to run.py and the Docker image copies in (MINGOL_REVISION and
MINGOL_BUILT_AT override it). It is compared with the newest release of a public GitHub
repository (MINGOL_UPDATE_REPO, default PSChuze/golfforeverybody; empty turns the check off)
through GitHub's REST API, without a token: the release's tag, the tag's commit, and the
compare from the running commit to it for how far behind it is and what changed.

The answer is kept for 30 minutes (10 when the check failed: an address without a token gets 60
calls an hour) and fetched on a thread of its own, so status.json waits at most WAIT seconds.
Every failure is an "unknown" answer with a reason, never an error.
"""
import datetime
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request

API = "https://api.github.com"
DEFAULT_REPO = "PSChuze/golfforeverybody"
HOW = "Run Golf Server Setup and choose Update."
TTL = 1800.0             # s an answer is kept
TTL_FAILED = 600.0       # s a failed check is kept before trying again
WAIT = 0.8               # s status.json waits for a check in flight
TIMEOUT = 8.0            # s per GitHub request
MAX_CHANGES = 50
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
REVISION_FILE = "server-revision.json"
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Failed(Exception):
    """A check that ended without an answer; the message is the panel's "why"."""


def fetch(url, timeout=TIMEOUT):
    """GET a GitHub API url -> (HTTP status, parsed JSON or None)."""
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                               "X-GitHub-Api-Version": "2022-11-28",
                                               "User-Agent": "golfforeverybody-update-check"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
        except ValueError:
            body = None
        return e.code, body


def iso(stamp):
    """A GitHub date ("2025-01-02T12:00:00Z" or with an offset) as ISO 8601 UTC, or ""."""
    if not isinstance(stamp, str) or not stamp:
        return ""
    try:
        t = datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if t.tzinfo is None:
        t = t.replace(tzinfo=datetime.timezone.utc)
    return t.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _date(c):
    commit = c.get("commit") or {}
    return iso((commit.get("committer") or {}).get("date") or (commit.get("author") or {}).get("date"))


def _subject(c):
    return str((c.get("commit") or {}).get("message") or "").split("\n", 1)[0].strip()[:200]


def read_revision(folder=HERE):
    """(commit, built_at) of this build: the environment, else server-revision.json, else ("", "")."""
    doc = {}
    try:
        with open(os.path.join(folder, REVISION_FILE), encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        pass
    if not isinstance(doc, dict):
        doc = {}
    commit = os.environ.get("MINGOL_REVISION") or str(doc.get("commit") or "")
    built = os.environ.get("MINGOL_BUILT_AT") or str(doc.get("built_at") or "")
    return commit.strip().lower(), built.strip()


def write_revision(folder, commit, built_at=None, tag=""):
    """Record the commit (and when it was built) for the image to report."""
    doc = {"commit": commit, "built_at": built_at or now_iso()}
    if tag:
        doc["tag"] = tag
    path = os.path.join(folder, REVISION_FILE)
    with open(path + ".tmp", "w", encoding="utf-8", newline="\n") as f:
        json.dump(doc, f)
        f.write("\n")
    os.replace(path + ".tmp", path)


def _get(fetch, path):
    try:
        status, body = fetch(API + path)
    except (OSError, ValueError) as e:      # timeouts, refused connections, bad JSON
        raise Failed("GitHub did not answer (%s)" % type(e).__name__)
    if status in (403, 429):
        raise Failed("GitHub refused the check for now (too many requests from this address)")
    return status, body


def latest_release(repo, fetch=fetch):
    """The newest release -> {"tag", "commit", "date", "zip"}. Raises Failed."""
    if not REPO_RE.match(repo or ""):
        raise Failed("MINGOL_UPDATE_REPO is not in the form owner/name")
    status, rel = _get(fetch, "/repos/%s/releases/latest" % repo)
    if status == 404:
        raise Failed("%s has no release on GitHub yet" % repo)
    if status != 200 or not isinstance(rel, dict) or not rel.get("tag_name"):
        raise Failed("GitHub gave an unexpected answer (%s)" % status)
    tag = str(rel["tag_name"])
    status, head = _get(fetch, "/repos/%s/commits/%s" % (repo, tag))
    if status != 200 or not isinstance(head, dict) or not SHA_RE.match(str(head.get("sha") or "")):
        raise Failed("GitHub did not give the commit of release %s (%s)" % (tag, status))
    return {"tag": tag, "commit": str(head["sha"]), "date": _date(head),
            "zip": "https://github.com/%s/archive/refs/tags/%s.zip" % (repo, tag)}


def compare(repo, running, release, fetch=fetch):
    """How far `running` is behind the release -> (behind, changes newest first). Raises Failed."""
    if release["commit"].startswith(running) or running.startswith(release["commit"]):
        return 0, []
    status, cmp = _get(fetch, "/repos/%s/compare/%s...%s" % (repo, running, release["commit"]))
    if status == 404:
        raise Failed("this build's commit is not in %s" % repo)
    if status != 200 or not isinstance(cmp, dict) or not isinstance(cmp.get("ahead_by"), int):
        raise Failed("GitHub gave an unexpected answer (%s)" % status)
    # base = the running commit, head = the release: the release's commits the build lacks are "ahead"
    changes = [{"commit": str(c.get("sha") or ""), "date": _date(c), "subject": _subject(c)}
               for c in reversed(cmp.get("commits") or [])
               if isinstance(c, dict) and len(c.get("parents") or []) < 2]
    return cmp["ahead_by"], changes[:MAX_CHANGES]


def unknown(why, latest=None):
    return {"latest": latest, "behind": None, "changes": [], "update": "unknown", "why": why, "how": HOW}


class UpdateCheck:
    """The update object for one repository and running commit, cached."""

    def __init__(self, repo, revision, built_at="", fetch=fetch, clock=time.monotonic):
        self.repo = (repo or "").strip()
        self.revision = (revision or "").strip().lower()
        self.built_at = (built_at or "").strip()
        self.fetch = fetch
        self.clock = clock
        self.lock = threading.Lock()
        self.answer = None        # the last check's result (without "running")
        self.until = 0.0          # clock time it goes stale
        self.thread = None

    def get(self, wait=WAIT):
        """The update object now: the cached answer, refreshed in the background when stale."""
        with self.lock:
            stale = self.answer is None or self.clock() >= self.until
            if stale and self.thread is None:
                self.thread = threading.Thread(target=self._refresh, name="mingol-update-check", daemon=True)
                self.thread.start()
            thread, answer = self.thread, self.answer
        if answer is None and thread is not None:
            thread.join(wait)
            with self.lock:
                answer = self.answer
        if answer is None:
            answer = unknown("still asking GitHub, try again in a moment")
        running = {"commit": self.revision or "unknown", "built_at": self.built_at or "unknown"}
        return dict(answer, running=running)

    def _refresh(self):
        try:
            answer, ttl = self.check(), TTL
        except Failed as e:
            answer, ttl = unknown(str(e)), TTL_FAILED
        except Exception as e:   # never let the check break status.json
            print("updates: the check failed: %r" % e, file=sys.stderr, flush=True)
            answer, ttl = unknown("the check failed (%s)" % type(e).__name__), TTL_FAILED
        with self.lock:
            self.answer, self.until, self.thread = answer, self.clock() + ttl, None

    def check(self):
        """Ask GitHub -> the answer (latest, behind, changes, update, why, how). Raises Failed."""
        rel = latest_release(self.repo, self.fetch)
        latest = {"commit": rel["commit"], "date": rel["date"]}
        if not SHA_RE.match(self.revision):
            return unknown("this build does not know its commit (no %s)" % REVISION_FILE, latest)
        behind, changes = compare(self.repo, self.revision, rel, self.fetch)
        return {"latest": latest, "behind": behind, "changes": changes,
                "update": "available" if behind > 0 else "current", "why": None, "how": HOW}


def from_env():
    """The check for this build, or None when MINGOL_UPDATE_REPO is set empty."""
    repo = os.environ.get("MINGOL_UPDATE_REPO", DEFAULT_REPO).strip()
    if not repo:
        return None
    commit, built = read_revision()
    return UpdateCheck(repo, commit, built)
