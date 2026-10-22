"""www.mgo.mingol.net replica: /tour/mgo.fcgi (official site), /tour/mgzantei.fcgi (Japan Tour API
the game calls), and optionally pages of the original site from a local Wayback mirror.

Original: one FastCGI script with pages P_Top, P_News, P_Tour, P_Rank, P_Fame, P_Mydata and a
login (HANDLE/PASS; the PS2 GETs ...&LOGIN=0). Rankings show live New Nationals submissions
(P_Record), plus archived tables when seed/rankings.json is there (none ship). Archived pages
are served from <data>/archive when it exists (raw/ + a Wayback CDX-style index.jsonl): the
newest capture before the 2005-11-01 shutdown at its original path, and the 2005-12 memorial
site under /memorial/. Absolute links to the old host are made relative.
"""
import json
import os
import re
import struct
import time
import urllib.parse

from .. import tours
from ..web import JapanTour
from .common import JsonState, login, page, text_html
from .httpd import Response, esc

SEED_DIR = os.path.join(os.path.dirname(__file__), "seed")
SHUTDOWN = "20051101"
MEMORIAL_FROM = "20051217"
MIME = {".html": "text/html; charset=Shift_JIS", ".htm": "text/html; charset=Shift_JIS",
        ".php": "text/html; charset=Shift_JIS", ".fcgi": "text/html; charset=Shift_JIS",
        ".gif": "image/gif", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
        ".css": "text/css", ".js": "application/x-javascript", ".txt": "text/plain"}
NEWS = [
    ("", "The servers are back",
     "Everybody's Golf Online is running again on a fan server. Lobbies, rooms, real "
     "tournaments (a new one every 15 minutes), New Nationals and the Clubhouse BBS are open."),
]


def load_seed(name, empty):
    """An archived table from seed/<name> (none ship), else `empty`."""
    try:
        with open(os.path.join(SEED_DIR, name), encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return empty


def parse_record(hexrec):
    """P_Record Record (0xbc): +4 tour code, +0x30 total, +0x38 server time, +0x3c handle (16),
    +0x4c comment (0x48), +0x94 tour name (0x28). Field meanings are inferred."""
    r = bytes.fromhex(hexrec)
    if len(r) < 0xBC:
        return None
    cstr = lambda b: b.split(b"\0", 1)[0].decode("cp932", "replace")
    return {"tour_code": struct.unpack_from("<I", r, 4)[0],
            "total": struct.unpack_from("<i", r, 0x30)[0],
            "handle": cstr(r[0x3C:0x4C]), "comment": cstr(r[0x4C:0x94]), "tour": cstr(r[0x94:0xBC])}


class Archive:
    """Lookup over the Wayback mirror: (kind, path) -> local file, newest capture wins."""

    def __init__(self, root):
        self.root = root
        self.official, self.memorial = {}, {}
        idx = os.path.join(root, "index.jsonl")
        if not os.path.exists(idx):
            return
        with open(idx, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("http") != 200 or "www.mgo.mingol.net" not in r.get("original", ""):
                    continue
                path = urllib.parse.urlsplit(r["original"]).path or "/"
                ts = r["timestamp"]
                table = (self.official if ts < SHUTDOWN else
                         self.memorial if ts >= MEMORIAL_FROM else None)
                if table is not None and (path not in table or table[path][0] < ts):
                    table[path] = (ts, r["local"])

    def get(self, path):
        if path.startswith("/memorial"):
            sub = path[len("/memorial"):] or "/"
            hit = self.memorial.get(sub) or self.memorial.get(sub.rstrip("/") + "/index.html")
            return hit, "/memorial"
        return self.official.get(path), ""

    def serve(self, path):
        hit, prefix = self.get(path)
        if not hit:
            return None
        local = os.path.join(self.root, hit[1])
        if not os.path.exists(local):
            return None
        with open(local, "rb") as f:
            data = f.read()
        ext = os.path.splitext(path)[1].lower() or ".html"
        ctype = MIME.get(ext, "application/octet-stream")
        if ctype.startswith("text/html"):
            data = re.sub(rb"https?://(www\.)?mgo\.mingol\.net(:80)?", b"", data)
            if prefix:
                data = re.sub(rb'((?:href|src|action)=["\'])/', rb"\1" + prefix.encode() + b"/", data)
        return Response(data, ctype=ctype)


class WWW:
    def __init__(self, store, sessions, data_dir):
        self.accounts = store
        self.sessions = sessions
        self.state = JsonState(os.path.join(data_dir, "www.json"), lambda: {"records": []})
        self.archived = load_seed("rankings.json", {"rankings": [], "hall_of_fame": []})
        self.archived_tours = load_seed("tournaments.json", {"listed": []})
        self.archive = Archive(os.path.join(data_dir, "archive"))
        self.jt = JapanTour()

    # ------------------------------------------------------------------ routing
    def handle(self, req):
        if req.path.endswith("mgzantei.fcgi"):
            return Response(self.japan_tour(req.body), ctype="text/plain")
        if req.path in ("/", "/tour/") or req.path.endswith("mgo.fcgi"):
            return self.mgo(req)
        hit = self.archive.serve(req.path)
        if hit:
            return hit
        return Response(page(req, "Not found", "<p>That page was not archived.</p>",
                             self.nav(req, None)), 404)

    # ------------------------------------------------------------------ Japan Tour API
    def japan_tour(self, body):
        """P_Max / P_Regu answered by web.JapanTour; P_Record also parsed into the live ranking."""
        b = body.decode("latin1", "replace").strip()
        if b.startswith("P_Record"):
            fields = dict(kv.partition("=")[::2] for kv in b.split("&"))
            rec = parse_record(fields.get("Record", ""))
            if rec:
                rec["t"] = time.strftime("%Y/%m/%d %H:%M", time.gmtime(time.time() + 9 * 3600))
                rec["raw"], rec["attest"] = fields.get("Record", ""), fields.get("Attest", "")
                with self.state.lock:
                    self.state.data["records"].append(rec)
                    self.state.save()
        return self.jt.answer(body)

    # ------------------------------------------------------------------ mgo.fcgi
    def url(self, req, tok, view=None, **kw):
        q = []
        if view:
            q.append(view)
        if tok:
            q.append("S=" + tok)
        q += ["%s=%s" % (k, urllib.parse.quote(str(v), encoding="cp932")) for k, v in kw.items()]
        # relative to /tour/ (the PS2 mangles '/'-rooted links); "/" itself links into /tour/
        base = "tour/mgo.fcgi" if req.path == "/" else "mgo.fcgi"
        return base + ("?" + "&".join(q) if q else "")

    def nav(self, req, tok):
        n = [("Top", self.url(req, tok, "P_Top")), ("News", self.url(req, tok, "P_News")),
             ("Tournaments", self.url(req, tok, "P_Tour")), ("Rankings", self.url(req, tok, "P_Rank")),
             ("Hall of Fame", self.url(req, tok, "P_Fame")), ("My Data", self.url(req, tok, "P_Mydata"))]
        if not req.ps2:
            n += [("BBS", "http://bbs.mgo.mingol.net:12105/dbbbs.fcgi"), ("Memorial (2005)", "/memorial/")]
        return n

    def mgo(self, req):
        p = req.params
        sess = self.sessions.get(p.get("S"))
        tok = p.get("S") if sess else None
        if "HANDLE" in p:                                   # PS2 GET ...&LOGIN=0, or the PC form
            acct = login(self.accounts, p.get("HANDLE"), p.get("PASS", ""))
            tok = self.sessions.new(acct["id"] if acct else None)
            sess = self.sessions.get(tok)
        handle = sess["handle"] if sess else None
        for name, fn in (("P_News", self.p_news), ("P_Tour", self.p_tour), ("P_Rank", self.p_rank),
                         ("P_Fame", self.p_fame), ("P_Mydata", self.p_mydata)):
            if name in p:
                title, body = fn(req, tok, handle)
                break
        else:
            title, body = self.p_top(req, tok, handle)
        return Response(page(req, title, body, self.nav(req, tok)))

    def login_box(self, req, tok, handle):
        if handle:
            return '<p class="note">Logged in as <b>%s</b>.</p>' % esc(handle)
        if req.ps2:
            return '<p class="note">Not logged in.</p>'
        return ('<form class="box" method="post" action="%s" accept-charset="Shift_JIS">'
                'Online ID: <input name="HANDLE" size="14"> Password: '
                '<input name="PASS" type="password" size="10"> <input type="submit" value="Log in"></form>'
                % self.url(req, None))

    def p_top(self, req, tok, handle):
        news = "".join("<p><b>%s %s</b><br>%s</p>" % (esc(d), esc(t), esc(b)) for d, t, b in NEWS[:2])
        body = self.login_box(req, tok, handle) + news
        if not req.ps2 and self.archive.official:
            body += ('<p class="note">Original pages: <a href="/tour/pc/w-online/index.php">What is MGO</a> | '
                     '<a href="/tour/pc/faq/index.php">FAQ</a> | <a href="/tour/pc/doc/index.php">Glossary</a> | '
                     '<a href="/tour/pc/dev/index_01.php">Dev diary</a> | <a href="/memorial/">Memorial site</a></p>')
        return "Everybody's Golf Online", body

    def p_news(self, req, tok, handle):
        return "News", "".join("<p><b>%s %s</b><br>%s</p>" % (esc(d), esc(t), esc(b)) for d, t, b in NEWS)

    def p_tour(self, req, tok, handle):
        jt = tours.japan_tour_record()
        name = jt[0x90:0xB8].split(b"\0")[0].decode("cp932")
        body = ("<p><b>Now open (New Nationals):</b> %s. Enter from the Clubhouse, New Nationals tab. "
                "Scores appear in the Rankings.</p>" % esc(name))
        body += ("<p><b>Real tournaments:</b> a 9-hole tour starts every 15 minutes (Japan time). "
                 "Enter from a lobby menu up to 30 seconds before the start.</p>")
        rows = "".join("<tr><td>%s</td><td>%s</td><td>%s</td></tr>"
                       % (esc(t["name"]), esc(t.get("period", "")), esc(t.get("course", "")))
                       for t in self.archived_tours["listed"])
        if rows:
            body += ('<h3>Past national tournaments (2003-2005, archived)</h3>'
                     '<table class="list"><tr><th>Tournament</th><th>Period</th><th>Course</th></tr>%s</table>'
                     % rows)
        return "Tournaments", body

    def live_ranking(self):
        best = {}
        for r in self.state.data["records"]:
            if r["handle"] and (r["handle"] not in best or r["total"] < best[r["handle"]]["total"]):
                best[r["handle"]] = r
        return sorted(best.values(), key=lambda r: (r["total"], r["t"]))[:100]

    def p_rank(self, req, tok, handle):
        p = req.params
        try:
            idx = int(p.get("tour", -1))
        except ValueError:
            idx = -1
        snaps = self.archived["rankings"]
        links = ['<a href="%s">New Nationals (live)</a>' % esc(self.url(req, tok, "P_Rank"))]
        links += ['<a href="%s">%s</a>' % (esc(self.url(req, tok, "P_Rank", tour=i)), esc(s["tour_name"]))
                  for i, s in enumerate(snaps)]
        if 0 <= idx < len(snaps):
            s = snaps[idx]
            rows = "".join("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                           % tuple(esc(r.get(k) or "") for k in ("rank", "player", "gross", "point", "comment"))
                           for r in s["rows"])
            table = ('<p class="note">Archived ranking (as last captured).</p><table class="list"><tr><th>Rank</th>'
                     '<th>Player</th><th>Gross</th><th>Point</th><th>Comment</th></tr>%s</table>' % rows)
            title = s["tour_name"]
        else:
            rows = "".join("<tr><td>%d</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                           % (i + 1, esc(r["handle"]), r["total"], esc(r["t"]), esc(r["comment"]))
                           for i, r in enumerate(self.live_ranking()))
            table = ('<table class="list"><tr><th>Rank</th><th>Player</th><th>Total</th><th>Submitted</th>'
                     '<th>Comment</th></tr>%s</table>' % (rows or '<tr><td colspan="5">No scores yet.</td></tr>'))
            title = "New Nationals ranking"
        return "Rankings", "<h3>%s</h3>%s<p>%s</p>" % (esc(title), table, " | ".join(links))

    def p_fame(self, req, tok, handle):
        try:
            pg = max(0, int(req.params.get("pg", 0)))
        except ValueError:
            pg = 0
        fame = self.archived["hall_of_fame"]
        chunk = fame[pg * 20:(pg + 1) * 20]
        rows = "".join("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                       % tuple(esc(f.get(k) or "") for k in ("nickname", "gross", "point", "date", "tour",
                                                              "comment")) for f in chunk)
        nav = []
        if pg:
            nav.append('<a href="%s">Previous</a>' % esc(self.url(req, tok, "P_Fame", pg=pg - 1)))
        if len(fame) > (pg + 1) * 20:
            nav.append('<a href="%s">Next</a>' % esc(self.url(req, tok, "P_Fame", pg=pg + 1)))
        body = ('<p class="note">Hall of fame from the original service (2003-2005).</p>'
                '<table class="list"><tr><th>Player</th><th>Gross</th><th>Point</th><th>Date</th>'
                '<th>Tournament</th><th>Comment</th></tr>%s</table><p>%s</p>' % (rows, " | ".join(nav)))
        return "Hall of Fame", body

    def p_mydata(self, req, tok, handle):
        if not handle:
            return "My Data", self.login_box(req, tok, handle) + "<p>Log in to see your results.</p>"
        mine = [r for r in self.state.data["records"] if r["handle"] == handle][-5:]
        rows = "".join("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                       % (esc(r["t"]), esc(r["tour"]), r["total"], esc(r["comment"])) for r in reversed(mine))
        return "My Data", ("<p><b>%s</b>: latest results (up to 5)</p><table class=\"list\"><tr><th>Date</th>"
                           "<th>Tournament</th><th>Total</th><th>Comment</th></tr>%s</table>"
                           % (esc(handle), rows or '<tr><td colspan="4">No results yet.</td></tr>'))
