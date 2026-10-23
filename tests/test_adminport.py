"""The OpenLobby admin port (mingol/adminport.py) over HTTP, against the real play and gate servers:
python -m unittest tests.test_adminport

Three scripted players sign in (tests/test_play.py's client); the panel's side is plain urllib.
Checks the token, the description, the status, and that each action reaches the players as the
packets the servers already send: a disconnect is the cmd 0x1c "left" the others get when a client
hangs up, login news is the gate's type 0x0e answer.
"""
import asyncio
import calendar
import json
import os
import struct
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request

from mingol import adminport, tours, updates
from mingol.accounts import Store
from mingol.feega import Feega, FeegaError
from mingol.play import Play
from mingol.regi_gate import Gate
from mingol.site.common import Sessions
from mingol.site.www import WWW
from tests import test_play
from tests.test_play import Client, cmd, is_cmd

TOKEN = "golf-test-token"


def http(base, path, body=None, token=TOKEN):
    req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                 headers={"X-Devtool-Token": token, "X-Devtool-Actor": "admin"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def page(base, path, post=False):
    """A tool page as the panel's frame loads it -> (status, text)."""
    req = urllib.request.Request(base + path, data=b"x=1" if post else None,
                                 headers={"X-Devtool-Token": TOKEN, "X-Devtool-Actor": "admin"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def jst(y, mo, d, h=0, mi=0):
    """Unix seconds of a Japan-time wall clock."""
    return calendar.timegm((y, mo, d, h, mi, 0)) - tours.JST


class CalendarTest(unittest.TestCase):
    def test_one_entry_per_japan_day(self):
        got = adminport.tour_calendar(jst(2026, 10, 1), jst(2026, 11, 1))
        self.assertEqual(len(got), 32)        # Sep 30 too: its 23:45 tour ends at 00:26 on Oct 1
        self.assertEqual([e["title"] for e in got[:2]] + [got[-1]["title"]],
                         ["Tournaments, Sep 30", "Tournaments, Oct 1", "Tournaments, Oct 31"])
        day = got[21]
        self.assertEqual((day["id"], day["kind"], day["start"], day["end"]),
                         ("tours-20261021", "tour", jst(2026, 10, 21), jst(2026, 10, 21, 23, 45) + 2460))
        self.assertEqual(len(day["times"]), 96)
        self.assertEqual(day["times"][:2], [[jst(2026, 10, 21), jst(2026, 10, 21, 0, 41)],
                                            [jst(2026, 10, 21, 0, 15), jst(2026, 10, 21, 0, 56)]])
        self.assertEqual(day["times"], [[s, tours.end_of(s)] for _t, s in tours.schedule(jst(2026, 10, 21, 12))])
        self.assertEqual(day["about"], "96 nine-hole Open Tours, one every 15 minutes of the Japan day; "
                                       "entry opens 15 minutes before each start, 100 players at most.")

    def test_only_the_days_touching_the_range(self):
        # just after midnight JST: the day before's last tour (23:45 to 00:26) is still on
        got = adminport.tour_calendar(jst(2026, 10, 22, 0, 10), jst(2026, 10, 22, 0, 20))
        self.assertEqual([e["id"] for e in got], ["tours-20261021", "tours-20261022"])
        got = adminport.tour_calendar(jst(2026, 10, 22, 1), jst(2026, 10, 22, 2))
        self.assertEqual([e["id"] for e in got], ["tours-20261022"])
        got = adminport.tour_calendar(jst(2026, 10, 22, 0, 30), jst(2026, 10, 23))
        self.assertEqual([e["id"] for e in got], ["tours-20261022"])
        self.assertEqual(adminport.tour_calendar(jst(2026, 10, 22), jst(2026, 10, 22)), [])


class AdminPortTest(unittest.TestCase):
    login = test_play.PlayTest.login           # signs a player in and puts them in lobby 1

    def test_off_without_a_port(self):
        os.environ.pop("MINGOL_ADMIN_PORT", None)

        async def go():
            return adminport.start(None, None, log=lambda m: None)
        self.assertIsNone(asyncio.run(go()))

    def test_wide_bind_needs_a_token(self):
        with self.assertRaises(ValueError):
            adminport.build(None, None, None, token="", bind="0.0.0.0", port=0)

    def test_panel(self):
        asyncio.run(self.flow())

    async def flow(self):
        os.environ["MINGOL_LOG_DIR"] = tempfile.mkdtemp()
        store = Store(os.path.join(tempfile.mkdtemp(), "a.json"))
        bob, _ = store.create("K1", "Bob", "pw")
        alice, _ = store.create("K2", "Alice", "pw")
        carol, _ = store.create("K3", "Carol", "sekritcarol")
        play = Play("127.0.0.1", store)
        gate = Gate("127.0.0.1", 12104, store, news="Welcome back")
        psrv = await play.serve("127.0.0.1", 0)
        gsrv = await gate.serve("127.0.0.1", 0)
        pport = psrv.sockets[0].getsockname()[1]
        gport = gsrv.sockets[0].getsockname()[1]
        ap = adminport.build(asyncio.get_running_loop(), play, gate, None, token=TOKEN,
                             bind="127.0.0.1", port=0, containers=["mingol-game-1"])
        host, n = ap.start()
        base = "http://%s:%d" % (host, n)
        call = lambda *a, **k: asyncio.to_thread(http, base, *a, **k)
        try:
            # the token on every request
            self.assertEqual((await call("/openlobby/module.json", token=""))[0], 403)
            self.assertEqual((await call("/openlobby/status.json", token="nope"))[0], 403)
            self.assertEqual((await call("/openlobby/actions/news", {"text": "x"}, token=""))[0], 403)
            self.assertEqual(gate.news, "Welcome back")

            st, mod = await call("/openlobby/module.json")
            self.assertEqual(st, 200)
            self.assertEqual((mod["key"], mod["title"], mod["short"], mod["platform"]),
                             ("mingol", "Everybody's Golf Online", "Everybody's Golf", "PS2 HDD"))
            self.assertEqual({a["id"]: a["kind"] for a in mod["actions"]}, {"kick": "kick", "news": ""})
            self.assertEqual(mod["services"][0]["containers"], ["mingol-game-1"])
            self.assertTrue(mod["calendar"])
            self.assertEqual([(t["id"], t["path"]) for t in mod["tools"]], [("japantour", "japantour")])
            self.assertTrue(mod["player"])
            self.assertFalse(mod["help"])

            lo = jst(2026, 10, 22)
            st, cal = await call("/openlobby/calendar.json?from=%d&to=%d" % (lo + 3600, lo + 7200))
            self.assertEqual((st, [e["id"] for e in cal["events"]]), (200, ["tours-20261022"]))
            self.assertEqual(cal["events"][0], adminport.tour_calendar(lo + 3600, lo + 7200)[0])

            st, doc = await call("/openlobby/status.json")
            self.assertEqual((st, doc["up"], doc["online"], doc["players"]), (200, True, 0, []))
            self.assertIn("Open Tour", doc["summary"])

            # Bob and Alice in lobby 1, Carol in a room of her own
            cl = await self.login(pport, bob)
            ca = await self.login(pport, alice)
            cm = await self.login(pport, carol)
            create = bytearray(0x74)
            struct.pack_into("<HHII", create, 0, 9, 1, 0, 0)
            struct.pack_into("<H", create, 0x10, 2)
            struct.pack_into("<H", create, 0x16, 1)
            cm.send(5, bytes(create))
            await cm.until(is_cmd(600))
            cm.send(6, b"/caption Sunday round\0")
            await asyncio.sleep(0.2)

            st, doc = await call("/openlobby/status.json")
            self.assertEqual(doc["online"], 3)
            self.assertEqual(doc["players"], [{"name": "Alice", "where": "lobby"},
                                              {"name": "Bob", "where": "lobby"},
                                              {"name": "Carol", "where": "room: Sunday round"}])
            rooms = {r["name"]: r for r in doc["rooms"]}
            self.assertEqual((rooms["Lobby 1"]["players"], rooms["Lobby 1"]["state"]), (2, "lobby"))
            self.assertEqual((rooms["Sunday round"]["players"], rooms["Sunday round"]["max"],
                              rooms["Sunday round"]["state"]), (1, 2, "open"))
            taking = [r["name"] for r in doc["rooms"] if r["state"] == "taking entries"]
            if taking:
                self.assertEqual(doc["summary"], "%s JST is taking entries." % taking[0])
            else:
                self.assertRegex(doc["summary"], r"^Next tour: Open Tour \d\d:\d\d JST\.$")
            self.assertEqual({f["label"] for f in doc["facts"]}, {"accounts", "Japan Tour rounds"})
            self.assertEqual(doc["facts"][0]["value"], 3)

            # the account window: the feega logins the account linked on the website, by key
            def lookup(links, handles="Bob,Alice"):
                q = {"polid": "ABCD1234", "handles": handles, "members": "7"}
                if links is not None:
                    q["links"] = json.dumps(links)
                return call("/openlobby/player.json?" + urllib.parse.urlencode(q))
            st, out = await lookup({"feega": [{"key": "K3", "name": "CarolFeega"}]})
            self.assertEqual((st, out), (200, {"characters": [
                {"name": "Carol", "detail": "in a room", "online": True,
                 "facts": [{"label": "Play data", "value": "none yet"}]}]}))
            raw = json.dumps(out)
            for secret in ("sekritcarol", "K3", "127.0.0.1", "user_no", "profile", "gamedb"):
                self.assertNotIn(secret, raw)
            # a login this server never saw, another game's login, and no links at all: nothing,
            # even though the handles name players who are online
            self.assertEqual(await lookup({"feega": [{"key": "9999999999", "name": "Bob"}]}), (200, {}))
            self.assertEqual(await lookup({"other1": [{"key": "K1", "name": "Bob"}]}), (200, {}))
            self.assertEqual(await lookup(None), (200, {}))
            self.assertEqual(await lookup({}), (200, {}))

            # an account's Kick: {"polid", "handles"}; Alice's socket closes, Bob sees Alice leave
            st, out = await call("/openlobby/actions/kick", {"polid": "ABCD1234", "handles": "Dave,Alice"})
            self.assertEqual(out, {"ok": True, "msg": "Disconnected Alice."})
            async def eof():
                while await ca.r.read(65536):
                    pass
            await asyncio.wait_for(eof(), 3)
            await cl.until(lambda f: is_cmd(0x1C)(f) and struct.unpack_from("<I", f.payload, 0x18)[0] == alice["user_no"])
            self.assertNotIn(alice["user_no"], play.players)
            st, out = await call("/openlobby/actions/kick", {"name": "Dave"})
            self.assertEqual(out, {"ok": False, "msg": "Not online."})
            st, out = await call("/openlobby/actions/kick", {})
            self.assertFalse(out["ok"])
            st, doc = await call("/openlobby/status.json")
            self.assertEqual([p["name"] for p in doc["players"]], ["Bob", "Carol"])

            # login news: the gate's answer to the NEWS request (type 0x0e)
            st, out = await call("/openlobby/actions/news", {"text": "  Course closes at 22:00  "})
            self.assertEqual(out, {"ok": True, "msg": "Shown at the next sign-in."})
            r, w = await asyncio.open_connection("127.0.0.1", gport)
            g = Client(r, w)
            g.send(0x0E, b"")
            f = await g.until(lambda f: f.type == 0x0E)
            self.assertEqual(f.payload, b"Course closes at 22:00\0")
            w.close()
            st, out = await call("/openlobby/actions/news", {"text": "x" * 300})
            self.assertFalse(out["ok"])
            self.assertEqual(gate.news, "Course closes at 22:00")
            st, out = await call("/openlobby/actions/news", {"text": ""})
            self.assertEqual((out["ok"], gate.news), (True, ""))

            self.assertEqual((await call("/openlobby/actions/announce", {"text": "x"}))[0], 404)
            for c in (cl, cm):
                c.send(5, cmd(0x0C))
                await c.until(is_cmd(0x0C))
                c.w.close()
        finally:
            ap.stop()
            psrv.close()
            gsrv.close()


class PlayerLookupTest(unittest.TestCase):
    """adminport.player_lookup: every account made under a linked feega login, offline ones too."""

    def test_accounts_under_the_linked_logins(self):
        data = tempfile.mkdtemp()
        store = Store(os.path.join(data, "accounts.json"))
        erin, _ = store.create("1000000001", "Erin", "hunter22")
        store.create("1000000001", "Erin2", "hunter22")
        store.create("1000000002", "Frank", "pw2")
        store.set_gamedb(erin["user_no"], b"" * 0xA0)
        play = Play("127.0.0.1", store)
        www = WWW(store, Sessions(), data)
        www.state.data["records"] += [
            {"handle": "Frank", "total": 68, "tour": "Spring Open", "comment": "", "t": "2025/01/01 21:00"},
            {"handle": "Erin", "total": 71, "tour": "Spring Open", "comment": "", "t": "2025/01/01 21:30"}]
        who = {"polid": "ABCD1234", "handles": ["Frank"], "members": ["7"],
               "links": {"feega": [{"key": "1000000001", "name": "erin"}, {"key": "1000000001", "name": "erin"}]}}
        got = adminport.player_lookup(play, www, who)
        self.assertEqual(got, {"characters": [
            {"name": "Erin", "detail": "offline", "online": False,
             "facts": [{"label": "Play data", "value": "yes"}, {"label": "Japan Tour", "value": "place 2, best 71"}]},
            {"name": "Erin2", "detail": "offline", "online": False,
             "facts": [{"label": "Play data", "value": "none yet"}]}]})
        raw = json.dumps(got)
        for secret in ("hunter22", "1000000001", "0101"):
            self.assertNotIn(secret, raw)
        # the handle names Frank, but no link of the account's does
        self.assertIsNone(adminport.player_lookup(play, www, dict(who, links={})))
        self.assertIsNone(adminport.player_lookup(play, www, dict(who, links={"feega": [{"key": "", "name": "x"}]})))
        self.assertIsNone(adminport.player_lookup(play, None, dict(who, links={"feega": [{"key": "42", "name": "x"}]})))
        self.assertIsNone(adminport.player_lookup(None, www, who))


class NewsAndJapanTourTest(unittest.TestCase):
    """Login news kept in the data dir, and the Japan Tour page over the website's records."""

    def setUp(self):
        self.data = tempfile.mkdtemp()
        os.environ.pop("MINGOL_NEWS_FILE", None)

    def test_login_news_is_kept_across_a_restart(self):
        self.assertEqual(adminport.load_news(self.data, "From MINGOL_NEWS"), "From MINGOL_NEWS")

        async def flow():
            store = Store(os.path.join(self.data, "accounts.json"))
            gate = Gate("127.0.0.1", 12104, store, news=adminport.load_news(self.data, "Welcome back"))
            ap = adminport.build(asyncio.get_running_loop(), None, gate, None, token=TOKEN, bind="127.0.0.1",
                                 port=0, containers=[], data_dir=self.data)
            base = "http://%s:%d" % ap.start()
            try:
                st, out = await asyncio.to_thread(http, base, "/openlobby/actions/news", {"text": " Rain  delay "})
                self.assertEqual(out, {"ok": True, "msg": "Shown at the next sign-in."})
                self.assertEqual(gate.news, "Rain delay")
                with open(adminport.news_path(self.data), encoding="utf-8") as f:
                    doc = json.load(f)
                self.assertEqual((doc["text"], doc["by"]), ("Rain delay", "admin"))
                # what run.py does at the next start
                self.assertEqual(adminport.load_news(self.data, "Welcome back"), "Rain delay")
                st, out = await asyncio.to_thread(http, base, "/openlobby/actions/news", {"text": "x" * 300})
                self.assertFalse(out["ok"])
                self.assertEqual(adminport.load_news(self.data, "Welcome back"), "Rain delay")
                st, out = await asyncio.to_thread(http, base, "/openlobby/actions/news", {"text": ""})
                self.assertEqual(out, {"ok": True, "msg": "Login news cleared."})
                self.assertEqual(adminport.load_news(self.data, "Welcome back"), "")   # cleared stays cleared
            finally:
                ap.stop()
        asyncio.run(flow())
        self.assertEqual(sorted(os.listdir(self.data)), ["login-news.json"])
        with open(adminport.news_path(self.data), "w", encoding="utf-8") as f:
            f.write("{broken")
        self.assertEqual(adminport.load_news(self.data, "Welcome back"), "Welcome back")
        os.environ["MINGOL_NEWS_FILE"] = os.path.join(self.data, "elsewhere.json")
        try:
            self.assertEqual(adminport.news_path(self.data), os.path.join(self.data, "elsewhere.json"))
        finally:
            os.environ.pop("MINGOL_NEWS_FILE")

    def test_japan_tour_page(self):
        store = Store(os.path.join(self.data, "accounts.json"))
        www = WWW(store, Sessions(), self.data)
        www.state.data["records"] += [
            {"handle": "Bob", "total": 74, "tour": "Spring Open", "comment": "windy", "t": "2025/01/01 21:00"},
            {"handle": "Carol", "total": 70, "tour": "Spring Open", "comment": "<b>birdies</b>", "t": "2025/01/01 21:30"},
            {"handle": "Bob", "total": 69, "tour": "Spring Open", "comment": "", "t": "2025/01/02 20:00"},
            {"handle": "Alice", "total": 70, "tour": "Spring Open", "comment": "", "t": "2025/01/02 20:30"}]

        async def flow(w):
            ap = adminport.build(asyncio.get_running_loop(), None, None, w, token=TOKEN, bind="127.0.0.1",
                                 port=0, containers=[])
            base = "http://%s:%d" % ap.start()
            try:
                return [await asyncio.to_thread(page, base, "/japantour"),
                        await asyncio.to_thread(page, base, "/japantour", True)]
            finally:
                ap.stop()
        (st, html), (pst, _) = asyncio.run(flow(www))
        self.assertEqual((st, pst), (200, 405))
        self.assertNotIn("<script", html)
        self.assertIn("4 from 3 players", html)
        ranking = html.split("Latest rounds")[0]
        # best total per player, ties share a place (Carol posted first)
        self.assertLess(ranking.index("<td>Bob</td>"), ranking.index("<td>Carol</td>"))
        self.assertLess(ranking.index("<td>Carol</td>"), ranking.index("<td>Alice</td>"))
        self.assertIn("<td>1</td><td>Bob</td><td>69</td>", ranking)
        self.assertIn("<td>2</td><td>Carol</td><td>70</td>", ranking)
        self.assertIn("<td>2</td><td>Alice</td><td>70</td>", ranking)
        self.assertNotIn("<td>74</td>", ranking)
        self.assertIn("&lt;b&gt;birdies&lt;/b&gt;", html)
        latest = html.split("Latest rounds")[1]
        self.assertLess(latest.index("2025/01/02 20:30"), latest.index("2025/01/01 21:00"))   # newest first
        self.assertIn("<td>74</td>", latest)
        (st, html), _ = asyncio.run(flow(None))
        self.assertEqual(st, 200)
        self.assertIn("does not run in this process", html)


class FakeFeega:
    """The Feega account API on a local port: two accounts, a token, and a way to go down."""

    ACCOUNTS = {"1000000001": "ErinFeega", "1000000002": "frank"}

    def __init__(self, token="feega-test-token"):
        import http.server
        import threading
        accounts, want = self.ACCOUNTS, token
        self.calls = []
        calls = self.calls

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                calls.append(self.path)
                if self.headers.get("X-Feega-Token") != want:
                    return self.answer(401, {"error": "token"})
                url = urllib.parse.urlsplit(self.path)
                if url.path.startswith("/v1/accounts/"):
                    key = url.path.rsplit("/", 1)[1]
                    if key in accounts:
                        return self.answer(200, {"key": key, "account": accounts[key]})
                elif url.path == "/v1/accounts":
                    name = urllib.parse.parse_qs(url.query).get("name", [""])[0]
                    for key, acct in accounts.items():
                        if acct.lower() == name.lower():
                            return self.answer(200, {"key": key, "account": acct})
                self.answer(404, {"error": "no_account"})

            def answer(self, code, doc):
                body = json.dumps(doc).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class FeegaLookupTest(unittest.TestCase):
    """The account window asks the Feega service for keys and login names (mingol/feega.py)."""

    def setUp(self):
        self.fake = FakeFeega()
        self.addCleanup(self.fake.close)
        self.data = tempfile.mkdtemp()
        self.store = Store(os.path.join(self.data, "accounts.json"))
        self.store.create("1000000001", "Erin", "pw")
        self.store.create("1000000002", "Frank", "pw")
        self.play = Play("127.0.0.1", self.store)

    def test_client(self):
        f = Feega(self.fake.url, "feega-test-token")
        self.assertEqual(f.account("1000000001"), "ErinFeega")
        self.assertIsNone(f.account("1000000009"))
        self.assertIsNone(f.account("not-a-key"))            # never sent
        self.assertEqual(f.key_of("FRANK"), "1000000002")
        self.assertIsNone(f.key_of("nobody"))
        self.assertNotIn("/v1/accounts/not-a-key", self.fake.calls)
        with self.assertRaises(FeegaError):
            Feega(self.fake.url, "wrong").account("1000000001")
        with self.assertRaises(FeegaError):
            Feega("", "").account("1000000001")

    def test_lookup_through_feega(self):
        feega = Feega(self.fake.url, "feega-test-token")
        # a link with a key gets the login's name now; one with only a name gets its key
        who = {"polid": "ABCD1234", "handles": [], "members": [],
               "links": {"feega": [{"key": "1000000001", "name": "old-name"}, {"name": "frank"}]}}
        logins = adminport.feega_logins(who, feega)
        self.assertEqual(logins, {"1000000001": "ErinFeega", "1000000002": "frank"})
        got = adminport.player_lookup(self.play, None, who, logins)
        self.assertEqual([(c["name"], c["facts"][0]) for c in got["characters"]],
                         [("Erin", {"label": "Feega login", "value": "ErinFeega"}),
                          ("Frank", {"label": "Feega login", "value": "frank"})])
        # Feega down: the links' own keys still find the accounts, a name-only link does not
        self.fake.close()
        down = Feega(self.fake.url, "feega-test-token", timeout=0.5)
        logins = adminport.feega_logins(who, down)
        self.assertEqual(logins, {"1000000001": None})
        got = adminport.player_lookup(self.play, None, who, logins)
        self.assertEqual(got, {"characters": [{"name": "Erin", "detail": "offline", "online": False,
                                               "facts": [{"label": "Play data", "value": "none yet"}]}]})

    def test_over_http(self):
        async def flow():
            ap = adminport.build(asyncio.get_running_loop(), self.play, None, None, token=TOKEN,
                                 bind="127.0.0.1", port=0, containers=[],
                                 feega=Feega(self.fake.url, "feega-test-token"))
            base = "http://%s:%d" % ap.start()
            try:
                q = urllib.parse.urlencode({"polid": "ABCD1234", "handles": "", "members": "",
                                            "links": json.dumps({"feega": [{"name": "ErinFeega"}]})})
                st, out = await asyncio.to_thread(http, base, "/openlobby/player.json?" + q)
                st2, mod = await asyncio.to_thread(http, base, "/openlobby/module.json")
                st3, picks = await asyncio.to_thread(http, base, "/openlobby/players.json?q=fr&limit=5")
                return (st, out), (st2, mod), (st3, picks)
            finally:
                ap.stop()
        (st, out), (st2, mod), (st3, picks) = asyncio.run(flow())
        self.assertEqual(st, 200)
        self.assertEqual(out["characters"][0]["name"], "Erin")
        self.assertEqual(out["characters"][0]["facts"][0], {"label": "Feega login", "value": "ErinFeega"})
        self.assertEqual((mod["accounts"], mod["players"]), ("feega", True))
        self.assertEqual(st3, 200)
        self.assertEqual(picks["players"], [{"id": "Frank", "label": "Frank", "detail": "offline", "online": False}])


class UpdateNotice(unittest.TestCase):
    """status.json carries the update object when the port has a check (mingol/updates.py)."""

    def status(self, check):
        store = Store(os.path.join(tempfile.mkdtemp(), "accounts.json"))
        play = Play("127.0.0.1", store)
        gate = Gate("127.0.0.1", 12104, store)

        async def flow():
            ap = adminport.build(asyncio.get_running_loop(), play, gate, None, token=TOKEN, bind="127.0.0.1",
                                 port=0, containers=[], update_check=check)
            base = "http://%s:%d" % ap.start()
            try:
                return await asyncio.to_thread(http, base, "/openlobby/status.json")
            finally:
                ap.stop()
        return asyncio.run(flow())

    def test_update_object(self):
        from tests.test_updates import NEW, OLD, REPO, FakeGitHub
        st, doc = self.status(updates.UpdateCheck(REPO, OLD, "2025-02-01T00:00:00Z", fetch=FakeGitHub()))
        self.assertEqual((st, doc["up"]), (200, True))
        self.assertEqual(doc["update"]["update"], "available")
        self.assertEqual(doc["update"]["latest"]["commit"], NEW)
        self.assertEqual(doc["update"]["running"]["commit"], OLD)
        st, doc = self.status(updates.UpdateCheck(REPO, OLD, fetch=FakeGitHub(down=True)))
        self.assertEqual((st, doc["up"], doc["update"]["update"]), (200, True, "unknown"))
        self.assertEqual((doc["update"]["latest"], doc["update"]["behind"]), ({"commit": None, "date": None}, 0))
        self.assertNotIn("update", self.status(None)[1])


if __name__ == "__main__":
    unittest.main()
