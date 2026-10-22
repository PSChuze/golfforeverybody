"""setup.py and hostsetup/openlobby.py without Docker: the settings the answers turn into, the
ports players need, the checks against real servers, and the docker commands that find and
start the shared DNS, Feega and pair with a panel (a fake docker records them)."""
import asyncio
import io
import json
import os
import tempfile
import threading
import unittest
import zipfile
from unittest import mock

import setup
from hostsetup import openlobby


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
        ok, got = openlobby.pair("http://127.0.0.1:8090", " ABCD2345 ", "mingol", "Everybody's Golf Online",
                                 "http://mingol:12190", "tok", opener)
        self.assertEqual((ok, got), (True, "secret-1"))
        self.assertEqual(sent[0], ("http://127.0.0.1:8090/api/pair",
                                   {"code": "ABCD2345", "key": "mingol", "title": "Everybody's Golf Online",
                                    "url": "http://mingol:12190", "token": "tok"}))
        ok, _ = openlobby.pair_update("http://127.0.0.1:8090", "mingol", "secret-1", "tok2", opener=opener)
        self.assertTrue(ok)
        self.assertEqual(sent[1], ("http://127.0.0.1:8090/api/pair/update",
                                   {"key": "mingol", "pairing": "secret-1", "token": "tok2"}))

        import urllib.error

        def refused(req, timeout=0):
            raise urllib.error.HTTPError(req.full_url, 403, "no", {}, io.BytesIO(b'{"error": "bad code"}'))
        self.assertEqual(openlobby.pair("http://x", "c", "mingol", "t", "u", "tok", refused),
                         (False, "The code is wrong, used or older than 10 minutes."))


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


if __name__ == "__main__":
    unittest.main()
