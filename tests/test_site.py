"""Website replica over real HTTP: BBS (PS2 entry, agree, read, post), www (PS2 login, Japan Tour
record submit -> ranking + my data), archive serving with link rewriting, the update check."""
import asyncio
import json
import os
import re
import struct
import tempfile
import unittest
import urllib.parse

from mingol.accounts import Store
from mingol.site import bbs as bbsmod
from mingol.site.bbs import BBS
from mingol.site.common import Sessions
from mingol.site.httpd import Site
from mingol.site.patch import Patch, xor
from mingol.site.www import WWW

PS2 = "CLAPHANZ/100"
HANDLE = "Ｇｏｌｆ"      # full-width "Golf": two bytes a character in Shift_JIS

# A seeded thread in the shape of seed/bbs.json (made-up posts; no seed ships).
SEED = {"boards": [{"board": 1, "threads": []}, {"board": 2, "threads": [
    {"thread_id": 61, "title": "Course tips", "highest_no_seen": 494, "posts": [
        {"no": 0, "author": "Starter", "date": "05/10/26 10:00", "body": "Share your tips here!"},
        {"no": 494, "author": "Replier", "date": "05/10/26 11:00", "body": "> tips\nAim left on 3."}]}]}]}


async def http(port, method, path, host, body=b"", ua=PS2):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(("%s %s HTTP/1.0\r\nHost: %s\r\nUser-Agent: %s\r\nContent-Type: application/x-www-form-urlencoded\r\n"
             "Content-Length: %d\r\n\r\n" % (method, path, host, ua, len(body))).encode("latin1") + body)
    data = await r.read()
    w.close()
    head, _, payload = data.partition(b"\r\n\r\n")
    status = int(head.split(b" ")[1])
    return status, head.decode("latin1"), payload


def sj(s):
    return urllib.parse.quote(s, encoding="cp932")


def links(html):
    return [h.replace("&amp;", "&") for h in re.findall(r'href="([^"]+)"', html)]


class SiteTest(unittest.TestCase):
    def test_site(self):
        asyncio.run(self.flow())

    async def flow(self):
        os.environ["MINGOL_LOG_DIR"] = tempfile.mkdtemp()
        data = tempfile.mkdtemp()
        store = Store(os.path.join(data, "accounts.json"))
        store.create("K1", HANDLE, "04UD")
        seed = os.path.join(data, "seed-bbs.json")
        with open(seed, "w", encoding="utf-8") as f:
            json.dump(SEED, f)
        self.addCleanup(setattr, bbsmod, "SEED", bbsmod.SEED)
        bbsmod.SEED = seed
        # a one-page archive: an official-era page with an absolute link
        os.makedirs(os.path.join(data, "archive", "raw"))
        with open(os.path.join(data, "archive", "raw", "faq.html"), "wb") as f:
            f.write('<a href="http://www.mgo.mingol.net:80/tour/pc/faq/x.php">FAQ</a>'.encode("cp932"))
        with open(os.path.join(data, "archive", "index.jsonl"), "w") as f:
            f.write(json.dumps({"timestamp": "20050101000000", "http": 200, "local": "raw/faq.html",
                                "original": "http://www.mgo.mingol.net:80/tour/pc/faq/index.php?S=AB"}) + "\n")
        sessions = Sessions()
        site = Site([(lambda h: h.startswith("bbs."), BBS(store, sessions, data)),
                     (lambda h: h.startswith("patch."), Patch()),
                     (lambda h: True, WWW(store, sessions, data))])
        srv = await site.serve("127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        bbs = "bbs.mgo.mingol.net"

        # --- BBS: PS2 entry -> rules -> agree -> index -> board 2 -> archived thread 61
        st, _, body = await http(port, "GET", "/ps2/dbbbs.fcgi?hn=%s&pw=04UD" % sj(HANDLE), bbs)
        html = body.decode("cp932")
        self.assertEqual(st, 200)
        # PS2 browser quirks: it keeps '&amp;' in hrefs, resolves '/x' against the current folder,
        # and never wraps text
        raw_hrefs = re.findall(r'href="([^"]+)"', html)
        self.assertTrue(raw_hrefs and all("&amp;" not in h and not h.startswith("/") for h in raw_hrefs))
        blocks = re.sub(r"</?(p|hr|tr|table|div|form|textarea)\b[^>]*>", "<br>", html, flags=re.I)
        text_lines = re.sub(r"<(?!br)[^>]+>", "", blocks).split("<br>")
        self.assertTrue(all(len(t.encode("cp932")) <= 46 for t in text_lines))
        agree = [u for u in links(html) if "agree" in u][0]
        _, _, body = await http(port, "GET", agree, bbs)
        html = body.decode("cp932")
        self.assertIn(HANDLE, html)                                   # logged in from hn/pw
        board2 = [u for u in links(html) if "b=2" in u][0]
        _, _, body = await http(port, "GET", board2, bbs)
        thread = [u for u in links(body.decode("cp932")) if "of=61" in u][0]
        _, _, body = await http(port, "GET", thread, bbs)
        html = body.decode("cp932")
        self.assertIn("Share your tips here!", html)                     # seeded starter
        tok = re.search(r"S=([0-9A-F]+)", thread).group(1)

        # --- reply in Shift_JIS, then it shows up with the next original number
        form = "S=%s&p_res=&of=61&body=%s" % (tok, sj(HANDLE + "! revival"))
        st, head, _ = await http(port, "POST", "/ps2/dbbbs.fcgi", bbs, form.encode("latin1"))
        self.assertEqual(st, 302)
        loc = re.search(r"Location: (\S+)", head).group(1)
        _, _, body = await http(port, "GET", loc, bbs)
        html = body.decode("cp932")
        self.assertIn(HANDLE + "! revival", html)
        self.assertIn("NO.495", html)                                   # continues after 494

        # --- www: PS2 login, Japan Tour API, record -> ranking + my data
        st, _, body = await http(port, "GET", "/tour/mgo.fcgi?HANDLE=%s&PASS=04UD&LOGIN=0" % sj(HANDLE),
                                 "www.mgo.mingol.net")
        html = body.decode("cp932")
        self.assertIn("Logged in as <b>%s</b>" % HANDLE, html)
        _, _, body = await http(port, "POST", "/tour/mgzantei.fcgi", "www.mgo.mingol.net", b"P_Max")
        self.assertEqual(body, b"1")
        rec = bytearray(0xBC)
        struct.pack_into("<Ii", rec, 4, 1, 0)
        struct.pack_into("<i", rec, 0x30, -7)
        rec[0x3C:0x3C + 8] = HANDLE.encode("cp932")
        rec[0x4C:0x4C + 4] = b"gg!!"
        _, _, body = await http(port, "POST", "/tour/mgzantei.fcgi", "www.mgo.mingol.net",
                                b"P_Record&Record=" + rec.hex().upper().encode() + b"&Attest=00")
        self.assertEqual(body, b"0\n")
        mydata = [u for u in links(html) if "P_Mydata" in u][0]
        _, _, body = await http(port, "GET", mydata, "www.mgo.mingol.net")
        self.assertIn("gg!!", body.decode("cp932"))
        _, _, body = await http(port, "GET", "/tour/mgo.fcgi?P_Rank", "www.mgo.mingol.net", ua="Mozilla/5.0")
        html = body.decode("cp932")
        self.assertIn(HANDLE, html)
        self.assertIn("-7", html)
        _, _, body = await http(port, "GET", "/tour/mgo.fcgi?P_Fame", "www.mgo.mingol.net", ua="Mozilla/5.0")
        self.assertIn("Hall of Fame", body.decode("cp932"))

        # --- archive: served at its original path, absolute host links made relative
        st, _, body = await http(port, "GET", "/tour/pc/faq/index.php?S=ZZ", "www.mgo.mingol.net", ua="Mozilla/5.0")
        self.assertEqual(st, 200)
        self.assertIn(b'href="/tour/pc/faq/x.php"', body)

        # --- the update check: "END" alone, XORed, means no patches
        st, head, body = await http(port, "GET", "/patch/ipatcher.inf", "patch.mgo.mingol.net", ua="")
        self.assertEqual((st, body, xor(body)), (200, bytes.fromhex("2821213833"), b"END\r\n"))
        self.assertIn("application/octet-stream", head)
        st, _, _ = await http(port, "GET", "/patch/other.bin", "patch.mgo.mingol.net", ua="")
        self.assertEqual(st, 404)
        srv.close()

    def test_no_seed(self):
        """Without archived data the site starts empty: two boards, no rankings, no archive."""
        data = tempfile.mkdtemp()
        store = Store(os.path.join(data, "accounts.json"))
        self.addCleanup(setattr, bbsmod, "SEED", bbsmod.SEED)
        bbsmod.SEED = os.path.join(data, "missing.json")
        b = BBS(store, Sessions(), data)
        self.assertEqual([(x["id"], x["name"]) for x in b.state.data["boards"]],
                         [(1, "Clubhouse 1"), (2, "Clubhouse 2")])
        self.assertEqual(b.add("Erin", "hello", board=1, title="First"), 1)
        w = WWW(store, Sessions(), data)
        self.assertEqual((w.archived["rankings"], w.archived_tours["listed"]), ([], []))


if __name__ == "__main__":
    unittest.main()
