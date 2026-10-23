"""setup.py and hostsetup/openlobby.py without Docker: the settings the answers turn into, the
ports players need, the checks against real servers, and the docker commands that find and
start the shared DNS, Feega and pair with a panel (a fake docker records them)."""
import asyncio
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
from unittest import mock

import setup
from hostsetup import openlobby
from hostsetup import progress as proglib


class FakeDocker:
    """Answers docker commands from a table and records them."""

    def __init__(self, running=None, labels=None, fail=()):
        self.running = running or {}         # label -> [container names]
        self.labels = labels or {}           # (container, key) -> value
        self.fail = set(fail)                # first words of commands that fail
        self.calls = []

    def __call__(self, args, timeout=600, cwd=None):
        self.calls.append(list(args))
        if args[0] in self.fail or " ".join(args[:2]) in self.fail:
            return 1, "error"
        if args[:2] == ["ps", "--filter"]:
            return 0, "\n".join(self.running.get(args[2][len("label="):], []))
        if args[0] == "inspect":
            key = args[2].split('"')[1]
            return 0, self.labels.get((args[3], key), "<no value>")
        if args[:2] == ["compose", "-f"] and "up" in args:
            with open(args[2], encoding="utf-8") as f:
                role = "http" if "name: openlobby-http" in f.read() else "dns"
            self.running["org.openlobby.role=" + role] = ["openlobby-%s-%s-1" % (role, role)]
        if args[:3] == ["compose", "-p", "freega"]:
            self.running["org.openlobby.game.key=feega"] = ["freega-feega-1"]
        return 0, ""


class Settings(unittest.TestCase):
    def test_first_settings(self):
        c = setup.first_settings("192.0.2.10", "Test Server", setup.read_env(setup.ENV_EXAMPLE))
        self.assertEqual(c["MINGOL_PUBLIC_IP"], "192.0.2.10")
        self.assertEqual(c["MINGOL_NEWS"], "Welcome to Test Server!")
        self.assertRegex(c["MINGOL_ADMIN_TOKEN"], r"^[0-9a-f]{48}$")
        self.assertNotIn("MINGOL_ADMIN_TOKEN", setup.first_settings("192.0.2.10", "x", {"MINGOL_ADMIN_TOKEN": "t"}))

    def test_ports(self):
        self.assertEqual(setup.ports_to_open({}), [12102, 12103, 12104, 12105, 80, 14020])
        self.assertEqual(setup.ports_to_open({}, feega_here=False), [12102, 12103, 12104, 12105, 80])
        self.assertEqual(setup.port_ranges(setup.ports_to_open({})), [(80, 80), (12102, 12105), (14020, 14020)])

    def test_who_plays_and_addresses(self):
        self.assertEqual(setup.who_plays({"MINGOL_PUBLIC_IP": "10.0.0.5"}, {}), "home")
        self.assertEqual(setup.who_plays({"MINGOL_PUBLIC_IP": "203.0.113.7"}, {}), "internet")
        self.assertEqual(setup.who_plays({"MINGOL_PUBLIC_IP": "10.0.0.5"}, {"who": "internet"}), "internet")
        self.assertEqual(setup.to_ip("203.0.113.7"), "203.0.113.7")
        self.assertEqual(setup.to_ip("localhost"), "127.0.0.1")
        self.assertEqual(setup.to_ip("10.1"), "")              # inet_aton takes it; players' consoles do not

    def test_env_file(self):
        d = tempfile.mkdtemp()
        env = os.path.join(d, ".env")
        setup.write_env({"MINGOL_PUBLIC_IP": "192.0.2.10", "MINGOL_FEEGA_TOKEN": "abc", "NEW_ONE": "1"},
                        path=env, template=setup.ENV_EXAMPLE)
        got = setup.read_env(env)
        self.assertEqual((got["MINGOL_PUBLIC_IP"], got["MINGOL_FEEGA_TOKEN"], got["NEW_ONE"]),
                         ("192.0.2.10", "abc", "1"))
        with open(env, encoding="utf-8") as f:
            self.assertIn("# Required: the address players' consoles reach", f.read())

    def test_unpack_keeps_the_hosts_files(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, ".env"), "w") as f:
            f.write("MINGOL_PUBLIC_IP=192.0.2.10\n")
        archive = os.path.join(d, "src.zip")
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("golfforeverybody/compose.yml", "name: mingol\n")
            z.writestr("golfforeverybody/.env", "MINGOL_PUBLIC_IP=\n")
            z.writestr("golfforeverybody/mingol/run.py", "")
        written = setup.unpack_server(archive, d, "v1.0.0")
        self.assertEqual(sorted(written), ["compose.yml", "mingol/run.py"])
        self.assertEqual(setup.read_env(os.path.join(d, ".env"))["MINGOL_PUBLIC_IP"], "192.0.2.10")


class Checks(unittest.TestCase):
    """regi_answers and patch_answers against the real servers on local ports."""

    def test_checks(self):
        from mingol.regi_gate import Regi
        from mingol.site.httpd import Site
        from mingol.site.patch import Patch
        os.environ["MINGOL_LOG_DIR"] = tempfile.mkdtemp()
        loop = asyncio.new_event_loop()
        ready = threading.Event()
        ports = {}

        async def serve():
            regi = await Regi("127.0.0.1", 12103).serve("127.0.0.1", 0)
            web = await Site([(lambda h: h.startswith("patch."), Patch())]).serve("127.0.0.1", 0)
            ports["regi"] = regi.sockets[0].getsockname()[1]
            ports["web"] = web.sockets[0].getsockname()[1]
            ready.set()
        t = threading.Thread(target=lambda: (loop.run_until_complete(serve()), loop.run_forever()), daemon=True)
        t.start()
        ready.wait(5)
        try:
            self.assertTrue(setup.regi_answers(port=ports["regi"]))
            self.assertTrue(setup.patch_answers(ports["web"]))
            self.assertFalse(setup.regi_answers(port=ports["web"], timeout=1))
        finally:
            loop.call_soon_threadsafe(loop.stop)


class OpenLobby(unittest.TestCase):
    def test_reuses_a_running_dns(self):
        dock = FakeDocker(running={"org.openlobby.role=dns": ["pol-dns-1"]})
        self.assertEqual(openlobby.find_dns(dock), "pol-dns-1")
        ok, _ = openlobby.dns_names("pol-dns-1", "mingol", ["mgo.mingol.net"], dock)
        self.assertTrue(ok)
        self.assertEqual(dock.calls[-1], ["exec", "pol-dns-1", "python", "stub.py", "dns-names", "add", "mingol",
                                          "mgo.mingol.net"])
        self.assertFalse(any(c[0] == "compose" for c in dock.calls))     # never a second one

    def test_starts_the_dns_when_there_is_none(self):
        dock = FakeDocker()
        d = tempfile.mkdtemp()
        self.assertEqual(openlobby.start_dns(d, "192.0.2.10", dock), "openlobby-dns-dns-1")
        with open(os.path.join(d, "compose.yml"), encoding="utf-8") as f:
            text = f.read()
        for want in ("org.openlobby.role: dns", 'POL_ADVERTISE: "192.0.2.10"', '"0.0.0.0:53:53/udp"',
                     'command: ["dns"]', "dns-data:/data", openlobby.DNS_IMAGE):
            self.assertIn(want, text)

    def test_reuses_a_running_web_server(self):
        dock = FakeDocker(running={"org.openlobby.role=http": ["pol-http-1"]})
        self.assertEqual(openlobby.find_http(dock), "pol-http-1")
        self.assertTrue(openlobby.http_hosts("pol-http-1", "mingol", setup.SITE_UPSTREAM, setup.HTTP_HOSTS, dock)[0])
        self.assertEqual(dock.calls[-1], ["exec", "pol-http-1", "python", "stub.py", "http-hosts", "add", "mingol",
                                          "http://mingol:12105", "patch.mgo.mingol.net", "www.mgo.mingol.net",
                                          "bbs.mgo.mingol.net"])
        self.assertTrue(openlobby.http_remove("pol-http-1", "mingol", dock)[0])
        self.assertEqual(dock.calls[-1], ["exec", "pol-http-1", "python", "stub.py", "http-hosts", "remove", "mingol"])
        self.assertFalse(any(c[0] == "compose" for c in dock.calls))     # never a second one

    def test_starts_the_web_server_when_there_is_none(self):
        dock = FakeDocker()
        d = tempfile.mkdtemp()
        self.assertEqual(openlobby.start_http(d, dock), "openlobby-http-http-1")
        self.assertIn(["network", "inspect", "openlobby"], dock.calls)
        with open(os.path.join(d, "compose.yml"), encoding="utf-8") as f:
            text = f.read()
        for want in ("org.openlobby.role: http", '"80:80"', 'command: ["http"]', "http-data:/data",
                     "external: true", "- openlobby", openlobby.HTTP_IMAGE):
            self.assertIn(want, text)

    def test_ensure_http(self):
        dock = FakeDocker(running={"org.openlobby.role=http": ["pol-http-1"]}, fail={"exec"})
        log = io.StringIO()
        with mock.patch.object(setup, "docker_run", dock), mock.patch("sys.stdout", io.StringIO()):
            self.assertFalse(setup.ensure_http(setup.proglib.Progress(out=io.StringIO()), log))
            dock.fail = set()
            self.assertTrue(setup.ensure_http(setup.proglib.Progress(out=io.StringIO()), log))

    def test_old_dns_image(self):
        dock = FakeDocker(running={"org.openlobby.role=dns": ["pol-dns-1"]}, fail={"exec"})
        self.assertFalse(openlobby.dns_names("pol-dns-1", "mingol", ["mgo.mingol.net"], dock)[0])

    def test_network(self):
        dock = FakeDocker(fail={"network inspect"})
        self.assertTrue(openlobby.ensure_network(dock))
        self.assertEqual(dock.calls, [["network", "inspect", "openlobby"], ["network", "create", "openlobby"]])

    def test_feega_from_a_folder(self):
        src = tempfile.mkdtemp()
        for name, text in (("compose.yml", "name: freega\n"),
                           (".env.example", "# admin token\nFEEGA_ADMIN_TOKEN=\nFEEGA_API_TOKEN=\n#FEEGA_ADVERTISE=\n")):
            with open(os.path.join(src, name), "w") as f:
                f.write(text)
        dest = os.path.join(tempfile.mkdtemp(), "Freega")
        dock = FakeDocker()
        self.assertIsNone(openlobby.find_feega(dock))
        ok, msg = openlobby.start_feega(dest, "192.0.2.10", src, run=dock)
        self.assertTrue(ok, msg)
        env = setup.read_env(os.path.join(dest, ".env"))
        self.assertRegex(env["FEEGA_ADMIN_TOKEN"], r"^[0-9a-f]{48}$")
        self.assertEqual((env["FEEGA_ADVERTISE"], env["FEEGA_API_TOKEN"]), ("192.0.2.10", ""))
        self.assertIn(["compose", "-p", "freega", "--env-file", ".env", "up", "-d", "--build"], dock.calls)
        self.assertEqual(openlobby.find_feega(dock), "freega-feega-1")
        # a second run keeps the tokens
        openlobby.feega_env(dest, "203.0.113.7")
        self.assertEqual(setup.read_env(os.path.join(dest, ".env")), env)

    def test_feega_source(self):
        def fetch(req, timeout=0):
            return io.BytesIO(json.dumps({"tag_name": "v1.2.0"}).encode())
        self.assertEqual(openlobby.feega_source("", fetch),
                         "https://github.com/PSChuze/freega/archive/refs/tags/v1.2.0.zip")
        self.assertEqual(openlobby.feega_source("v0.9.0"), "https://github.com/PSChuze/freega/archive/refs/tags/v0.9.0.zip")

        def down(req, timeout=0):
            raise OSError("offline")
        self.assertIsNone(openlobby.feega_source("", down))

    def test_panel_and_pairing(self):
        dock = FakeDocker(running={"org.openlobby.role=panel": ["openlobby-web-1"]},
                          labels={("openlobby-web-1", "org.openlobby.panel.url"): "http://127.0.0.1:8090/"})
        self.assertEqual(openlobby.find_panel(dock), "http://127.0.0.1:8090")
        sent = []

        class Answer(io.BytesIO):
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def opener(req, timeout=0):
            sent.append((req.full_url, json.loads(req.data)))
            return Answer(json.dumps({"ok": True, "title": "x", "pairing": "secret-1"}).encode())
        ok, got, answer = openlobby.pair("http://127.0.0.1:8090", " ABCD2345 ", "mingol", "Everybody's Golf Online",
                                         "http://mingol:12190", "tok", opener=opener)
        self.assertEqual((ok, got, answer), (True, "secret-1", None))
        self.assertEqual(sent[0], ("http://127.0.0.1:8090/api/pair",
                                   {"code": "ABCD2345", "key": "mingol", "title": "Everybody's Golf Online",
                                    "url": "http://mingol:12190", "token": "tok"}))
        ok, _, _ = openlobby.pair_update("http://127.0.0.1:8090", "mingol", "secret-1", "tok2", opener=opener)
        self.assertTrue(ok)
        self.assertEqual(sent[1], ("http://127.0.0.1:8090/api/pair/update",
                                   {"key": "mingol", "pairing": "secret-1", "token": "tok2"}))

        import urllib.error

        def refused(req, timeout=0):
            raise urllib.error.HTTPError(req.full_url, 403, "no", {}, io.BytesIO(b'{"error": "bad code"}'))
        self.assertEqual(openlobby.pair("http://x", "c", "mingol", "t", "u", "tok", opener=refused),
                         (False, "The code is wrong, used or older than 10 minutes.", None))


class FirstRun(unittest.TestCase):
    def test_dry_run_writes_nothing(self):
        d = tempfile.mkdtemp()
        answers = iter(["2", "203.0.113.7", "Test Server"])
        out = io.StringIO()
        with mock.patch("builtins.input", lambda prompt="": next(answers)), \
                mock.patch.object(setup, "public_address", lambda: "203.0.113.7"), \
                mock.patch("sys.stdout", out):
            setup.use_root(d)
            try:
                setup.ENV_EXAMPLE = os.path.join(os.path.dirname(os.path.abspath(setup.__file__)), ".env.example")
                setup.first_run(dry_run=True)
            finally:
                setup.use_root(os.path.dirname(os.path.abspath(setup.__file__)))
        text = out.getvalue()
        self.assertIn("MINGOL_PUBLIC_IP=203.0.113.7", text)
        self.assertIn("MINGOL_NEWS=Welcome to Test Server!", text)
        self.assertEqual(os.listdir(d), [])


OLD = "a" * 40
NEW = "b" * 40


class FakeHost:
    """setup.py's way out to Docker, faked: docker compose answers from a table and every
    command is recorded."""

    def __init__(self, running=("game",), build_fails=False, ps_fails=False):
        self.running = list(running)
        self.build_fails = build_fails
        self.ps_fails = ps_fails
        self.calls = []                  # (argv, cwd)
        self.seen_at_build = None        # (the build folder's code, the server folder's code, .env there?)

    def run(self, cmd, check=True, capture=False, **kw):
        self.calls.append((list(cmd), None))
        if cmd[:3] == ["docker", "compose", "ps"]:
            return subprocess.CompletedProcess(cmd, 1 if self.ps_fails else 0,
                                               stdout="\n".join(self.running) + "\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    def run_logged(self, cmd, log, on_line=None, progress=None, cwd=None, env=None):
        self.calls.append((list(cmd), cwd))
        if cmd[:3] == ["docker", "compose", "build"]:
            with open(os.path.join(cwd, "mingol", "code.py"), encoding="utf-8") as f:
                staged = f.read()
            with open(os.path.join(setup.ROOT, "mingol", "code.py"), encoding="utf-8") as f:
                live = f.read()
            self.seen_at_build = (staged, live, os.path.exists(os.path.join(cwd, ".env")))
            if self.build_fails:
                return 1, ["ERROR: failed to solve: process did not complete successfully"]
        return 0, []

    def docker(self):
        return [c for c, _ in self.calls if c[0] == "docker"]


def write(d, rel, text):
    os.makedirs(os.path.dirname(os.path.join(d, rel)), exist_ok=True)
    with open(os.path.join(d, rel), "w", encoding="utf-8") as f:
        f.write(text)


def make_server(d):
    """An installed server folder at v1.0.0 (commit OLD) with the host's own files."""
    files = {"compose.yml": "name: mingol\n", "Dockerfile": "FROM python\n", "setup.py": "# old\n",
             "run.py": "", "mingol/code.py": "old", "mingol/gone.py": "removed by the update"}
    for rel, text in files.items():
        write(d, rel, text)
    write(d, ".setup-files", "\n".join(sorted(files)) + "\nserver-revision.json\n")
    write(d, ".setup-version", "v1.0.0\n")
    setup.updates.write_revision(d, OLD, "2025-01-01T00:00:00Z", tag="v1.0.0")
    write(d, ".env", "MINGOL_PUBLIC_IP=192.0.2.10\nMINGOL_ADMIN_TOKEN=t\n")
    write(d, "state/data/accounts.json", "{}")
    write(d, "backups/golf-1.zip", "zip")
    write(d, "logs/play.jsonl", "x")


def make_release(path):
    """GitHub's source zip of a release: everything in one top folder."""
    with zipfile.ZipFile(path, "w") as z:
        for rel, text in (("compose.yml", "name: mingol\n"), ("Dockerfile", "FROM python\n"),
                          ("setup.py", "# new\n"), ("run.py", ""), ("mingol/code.py", "new"),
                          (".env", "MINGOL_PUBLIC_IP=\n"), ("state/data/accounts.json", "[]")):
            z.writestr("golfforeverybody-1.1.0/" + rel, text)


class Update(unittest.TestCase):
    """setup.py --update --yes: no question, no stdin, the old version kept until the new one builds."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        make_server(self.root)
        self.zip = os.path.join(tempfile.mkdtemp(), "release.zip")
        make_release(self.zip)
        self.release = {"tag": "v1.1.0", "commit": NEW, "date": "2025-03-02T09:00:00Z",
                        "zip": "https://example.invalid/v1.1.0.zip"}
        self.addCleanup(setup.use_root, os.path.dirname(os.path.abspath(setup.__file__)))

    def update(self, host, release=None, argv=None):
        """Run the update in-process with stdin closed -> (exit code, printed lines)."""
        out = io.StringIO()
        stdin = io.StringIO("")
        stdin.close()
        temps = []
        real_mkdtemp = tempfile.mkdtemp

        def mkdtemp(*a, **k):
            temps.append(real_mkdtemp(*a, **k))
            return temps[-1]

        def latest():
            if isinstance(release, Exception):
                raise release
            return release or self.release

        def no_questions(prompt=""):
            raise AssertionError("asked: %s" % prompt)

        cwd = os.getcwd()
        with mock.patch.object(setup, "docker_state", lambda: "ok"), \
                mock.patch.object(setup, "run", host.run), \
                mock.patch.object(setup.proglib, "run_logged", host.run_logged), \
                mock.patch.object(setup, "latest_release", latest), \
                mock.patch.object(setup, "download", lambda url, path, timeout=60: shutil.copy(self.zip, path)), \
                mock.patch.object(setup, "wait_ready", lambda env, timeout=120, hosts=(): "127.0.0.1"), \
                mock.patch.object(setup, "patch_answers", lambda *a, **k: True), \
                mock.patch.object(setup.tempfile, "mkdtemp", mkdtemp), \
                mock.patch("builtins.input", no_questions), \
                mock.patch("sys.stdin", stdin), mock.patch("sys.stdout", out):
            try:
                code = setup.main(argv or ["--update", "--yes", "--folder", self.root])
            finally:
                os.chdir(cwd)
        self.temps = temps
        return code, out.getvalue().strip().splitlines()

    def read(self, rel):
        with open(os.path.join(self.root, rel), encoding="utf-8") as f:
            return f.read()

    def test_already_current(self):
        setup.updates.write_revision(self.root, NEW, tag="v1.1.0")
        host = FakeHost()
        code, lines = self.update(host)
        self.assertEqual((code, lines[-1]), (0, "Already up to date (v1.1.0)."))
        self.assertEqual(host.docker(), [["docker", "compose", "ps", "--status", "running", "--services"]])
        self.assertEqual(self.read("mingol/code.py"), "old")

    def test_available(self):
        host = FakeHost(running=["game"])
        code, lines = self.update(host)
        self.assertEqual(code, 0, lines)
        self.assertEqual(lines[-1], "Updated to v1.1.0 (bbbbbbb); restarted game.")
        # built from the new files beside the old ones, with the host's settings
        self.assertEqual(host.seen_at_build, ("new", "old", True))
        build = [cwd for c, cwd in host.calls if c[:3] == ["docker", "compose", "build"]]
        self.assertNotEqual(os.path.normpath(build[0]), os.path.normpath(self.root))
        self.assertIn(["docker", "compose", "up", "-d", "--no-deps", "--no-build", "game"], host.docker())
        # the code is new, the host's own files are as they were
        self.assertEqual((self.read("mingol/code.py"), self.read("setup.py")), ("new", "# new\n"))
        self.assertFalse(os.path.exists(os.path.join(self.root, "mingol", "gone.py")))
        self.assertEqual(self.read(".env"), "MINGOL_PUBLIC_IP=192.0.2.10\nMINGOL_ADMIN_TOKEN=t\n")
        self.assertEqual(self.read("state/data/accounts.json"), "{}")
        self.assertEqual((self.read("backups/golf-1.zip"), self.read("logs/play.jsonl")), ("zip", "x"))
        self.assertEqual(setup.updates.read_revision(self.root)[0], NEW)
        self.assertEqual(self.read(".setup-version").strip(), "v1.1.0")
        self.assertFalse(any(os.path.exists(t) for t in self.temps))          # no leftovers
        # only this compose project: Feega and the shared DNS and web server are left alone
        self.assertTrue(all(c[:2] == ["docker", "compose"] and "-f" not in c and "-p" not in c
                            for c in host.docker()))
        # safe to run again: now it is current
        code, lines = self.update(FakeHost())
        self.assertEqual((code, lines[-1]), (0, "Already up to date (v1.1.0)."))

    def test_stopped_services_stay_stopped(self):
        host = FakeHost(running=[])
        code, lines = self.update(host)
        self.assertEqual((code, lines[-1]), (0, "Updated to v1.1.0 (bbbbbbb); the server stays stopped."))
        self.assertFalse(any("up" in c for c in host.docker()))
        self.assertEqual(self.read("mingol/code.py"), "new")

    def test_build_failure_keeps_the_old_code(self):
        host = FakeHost(build_fails=True)
        code, lines = self.update(host)
        self.assertEqual(code, 1)
        self.assertTrue(lines[-1].startswith("Building the new version failed, so the old one keeps running"))
        self.assertTrue(lines[-1].endswith("."))
        self.assertEqual(self.read("mingol/code.py"), "old")
        self.assertEqual(setup.updates.read_revision(self.root)[0], OLD)
        self.assertFalse(any("up" in c for c in host.docker()))
        self.assertFalse(any(os.path.exists(t) for t in self.temps))

    def test_offline(self):
        code, lines = self.update(FakeHost(), release=setup.updates.Failed("GitHub did not answer (OSError)"))
        self.assertEqual(code, 1)
        self.assertEqual(lines[-1], "Could not find the newest release (GitHub did not answer (OSError)), "
                                    "so nothing was updated.")

    def test_docker_does_not_list(self):
        code, lines = self.update(FakeHost(ps_fails=True))
        self.assertEqual((code, lines[-1]), (1, "Docker did not list the server's services, so nothing was updated."))
        self.assertEqual(self.read("mingol/code.py"), "old")

    def test_not_a_server_folder(self):
        code, lines = self.update(FakeHost(), argv=["--update", "--yes", "--folder", tempfile.mkdtemp()])
        self.assertEqual(code, 1)
        self.assertIn("no compose.yml", lines[-1])

    def test_runs_as_a_script_with_stdin_closed(self):
        # python setup.py from the source, as the deployer runs it; stops before any docker command
        here = os.path.dirname(os.path.abspath(setup.__file__))
        empty = tempfile.mkdtemp()
        r = subprocess.run([sys.executable, os.path.join(here, "setup.py"), "--update", "--yes", "--folder", empty],
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, timeout=60, cwd=here)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertRegex(r.stdout.strip().splitlines()[-1], r"no compose\.yml\), so there is nothing to update\.$")
        self.assertEqual(os.listdir(empty), [])

    def test_progress_lines_without_a_terminal(self):
        out = io.StringIO()
        now = [0.0]
        p = proglib.Progress(out, clock=lambda: now[0])
        p.begin("Building the new server image", "a few minutes")
        for _ in range(5):
            now[0] += 61
            p.tick()
        p.end()
        lines = out.getvalue().splitlines()
        self.assertEqual(lines[0], "Building the new server image (a few minutes)...")
        self.assertEqual(sum("so far" in line for line in lines), 2)       # at 2 and 4 minutes
        self.assertEqual(lines[-1], "  Done: Building the new server image (5 minutes)")

    def test_the_folder_has_what_setup_py_needs(self):
        here = os.path.dirname(os.path.abspath(setup.__file__))
        for rel in ("setup.py", "hostsetup/openlobby.py", "hostsetup/progress.py", "mingol/updates.py"):
            self.assertTrue(os.path.exists(os.path.join(here, rel)), rel)
        self.assertEqual(setup.UPDATER_COMMAND, ["python", "setup.py", "--update", "--yes"])


class UpdaterPairing(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        write(self.root, ".env", "MINGOL_ADMIN_TOKEN=tok\n")
        setup.use_root(self.root)
        self.addCleanup(setup.use_root, os.path.dirname(os.path.abspath(setup.__file__)))
        self.sent = []

    def panel(self, answer):
        """openlobby's POST to the panel, answered with `answer` and recorded."""
        def post(url, doc, opener):
            self.sent.append((url, doc))
            return 200, answer
        return mock.patch.object(openlobby, "_post", post)

    def test_pairing_sends_the_updater(self):
        want = {"folder": self.root, "command": ["python", "setup.py", "--update", "--yes"], "timeout_minutes": 60}
        self.assertEqual(setup.updater(), want)
        out = io.StringIO()
        with mock.patch("builtins.input", lambda prompt="": "ABCD2345"), mock.patch("sys.stdout", out), \
                self.panel({"ok": True, "pairing": "secret-1", "updater": "requested"}):
            self.assertTrue(setup.pair_with_panel(setup.read_env(), "http://127.0.0.1:8090"))
        url, doc = self.sent[0]
        self.assertEqual((url, doc["updater"]), ("http://127.0.0.1:8090/api/pair", want))
        self.assertIn("its owner must approve it on Settings > Modules", out.getvalue())
        self.assertEqual(setup.read_choices()["updater_folder"], self.root)
        self.assertEqual(setup.read_env()["OPENLOBBY_PAIRING"], "secret-1")

    def test_resent_when_the_folder_changes(self):
        setup.write_env({"OPENLOBBY_PAIRING": "secret-1"})
        setup.write_choices({"updater_folder": "/somewhere/else"})
        dock = FakeDocker(running={"org.openlobby.role=panel": ["openlobby-web-1"]},
                          labels={("openlobby-web-1", "org.openlobby.panel.url"): "http://127.0.0.1:8090"})
        with mock.patch.object(setup, "docker_run", dock), self.panel({"ok": True, "updater": "approved"}):
            setup.resend_updater()
            setup.resend_updater()                     # already sent: no second request
        self.assertEqual(len(self.sent), 1)
        url, doc = self.sent[0]
        self.assertEqual(url, "http://127.0.0.1:8090/api/pair/update")
        self.assertEqual((doc["pairing"], doc["token"], doc["updater"]["folder"]), ("secret-1", "tok", self.root))
        self.assertEqual(setup.read_choices()["updater_folder"], self.root)


class SeveralPanels(unittest.TestCase):
    """More than one container labelled org.openlobby.role=panel: ask, or pair with none."""
    A, B = "http://127.0.0.1:8090", "http://127.0.0.1:8091"

    def dock(self):
        return FakeDocker(running={"org.openlobby.role=panel": ["openlobby-web-1", "lab-panel-1"]},
                          labels={("openlobby-web-1", "org.openlobby.panel.url"): self.A + "/",
                                  ("lab-panel-1", "org.openlobby.panel.url"): self.B})

    def test_listed(self):
        self.assertEqual(openlobby.find_panels(self.dock()),
                         [("openlobby-web-1", self.A), ("lab-panel-1", self.B)])

    def test_never_guessed(self):
        told = []
        self.assertIsNone(openlobby.find_panel(self.dock(), tell=told.append))
        self.assertEqual(len(told), 1)
        for text in ("openlobby-web-1 at " + self.A, "lab-panel-1 at " + self.B, "run Setup and choose the panel"):
            self.assertIn(text, told[0])
        self.assertIsNone(openlobby.find_panel(self.dock()))          # nobody to tell: still none

    def test_chosen(self):
        seen = []

        def choose(panels):
            seen.append(panels)
            return 1
        self.assertEqual(openlobby.find_panel(self.dock(), choose=choose), self.B)
        self.assertEqual(seen, [[("openlobby-web-1", self.A), ("lab-panel-1", self.B)]])
        self.assertIsNone(openlobby.find_panel(self.dock(), choose=lambda panels: None))

    def test_one_panel_unchanged(self):
        dock = FakeDocker(running={"org.openlobby.role=panel": ["openlobby-web-1"]},
                          labels={("openlobby-web-1", "org.openlobby.panel.url"): self.A})
        asked = []
        self.assertEqual(openlobby.find_panel(dock, choose=asked.append, tell=asked.append), self.A)
        self.assertEqual(asked, [])

    def test_setup_asks_at_a_terminal(self):
        out = io.StringIO()
        with mock.patch.object(setup, "docker_run", self.dock()), mock.patch.object(setup, "interactive", lambda: True),                 mock.patch("builtins.input", lambda prompt="": "2"), mock.patch("sys.stdout", out):
            self.assertEqual(setup.find_panel(), self.B)
        self.assertIn("1. openlobby-web-1 (%s)" % self.A, out.getvalue())
        self.assertIn("2. lab-panel-1 (%s)" % self.B, out.getvalue())

    def test_setup_without_a_terminal(self):
        out = io.StringIO()

        def no_input(prompt=""):
            raise AssertionError("asked without a terminal")
        with mock.patch.object(setup, "docker_run", self.dock()), mock.patch.object(setup, "interactive", lambda: False),                 mock.patch("builtins.input", no_input), mock.patch("sys.stdout", out):
            self.assertIsNone(setup.find_panel())
        lines = [line for line in out.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)
        self.assertIn("run Golf Server Setup and choose the panel", lines[0])
        self.assertIn("lab-panel-1 at " + self.B, lines[0])

    def test_update_yes_sends_nothing(self):
        root = tempfile.mkdtemp()
        write(root, ".env", "MINGOL_ADMIN_TOKEN=tok\nOPENLOBBY_PAIRING=secret-1\n")
        setup.use_root(root)
        self.addCleanup(setup.use_root, os.path.dirname(os.path.abspath(setup.__file__)))
        sent = []
        with mock.patch.object(setup, "docker_run", self.dock()), mock.patch.object(setup, "interactive", lambda: False),                 mock.patch.object(openlobby, "_post", lambda url, doc, opener: sent.append(url) or (200, {})),                 mock.patch("sys.stdout", io.StringIO()):
            setup.resend_updater()
        self.assertEqual(sent, [])


if __name__ == "__main__":
    unittest.main()
