"""Clubhouse BBS (bbs.mgo.mingol.net) replica.

Original shape: a rules page with an agree step, two boards, threads viewed with `of=<thread
id>` and paged replies, a reply form (`p_res`). The PS2 enters at
/ps2/dbbbs.fcgi?hn=<handle>&pw=<password> (in-game ID + MinGOL pass) and is logged in
directly; PC visitors log in with the same pair. Threads can be seeded from seed/bbs.json
(none ship); new replies continue each seeded thread's numbering.
"""
import json
import os
import urllib.parse

from .common import JsonState, jst_now, login, page, text_html
from .httpd import Response, esc

SEED = os.path.join(os.path.dirname(__file__), "seed", "bbs.json")
BOARD_NAMES = {1: "Clubhouse 1", 2: "Clubhouse 2"}
THREADS_PER_PAGE = 20
MAX_BODY_CHARS = 2000
MAX_TITLE_CHARS = 40
ENTRY_PATHS = ("/", "/dbbbs.fcgi", "/ps2/dbbbs.fcgi", "/dbbbs/dbbbs.fcgi")


def seed_state():
    try:
        with open(SEED, encoding="utf-8") as f:
            src = json.load(f)
    except FileNotFoundError:
        src = {"boards": [{"board": n, "threads": []} for n in sorted(BOARD_NAMES)]}
    st = {"boards": [], "threads": [], "posts": {}, "next_thread_id": 1}
    for b in src["boards"]:
        st["boards"].append({"id": b["board"], "name": BOARD_NAMES.get(b["board"], "Board %d" % b["board"])})
        for t in b["threads"]:
            posts = sorted(t["posts"], key=lambda p: p["no"])
            st["threads"].append({
                "id": t["thread_id"], "board": b["board"], "title": t["title"],
                "author": posts[0]["author"], "created": posts[0]["date"], "last": posts[-1]["date"],
                "next_no": t["highest_no_seen"] + 1, "source": "wayback:20051026"})
            st["posts"][str(t["thread_id"])] = [
                {"no": p["no"], "author": p["author"], "date": p["date"], "icon": p.get("icon", ""),
                 "body": p["body"], "source": "wayback"} for p in posts]
    st["next_thread_id"] = max((t["id"] for t in st["threads"]), default=0) + 1
    return st


class BBS:
    def __init__(self, store, sessions, data_dir):
        self.accounts = store
        self.sessions = sessions
        self.state = JsonState(os.path.join(data_dir, "bbs.json"), seed_state)

    # ------------------------------------------------------------------ helpers
    def url(self, req, tok, **kw):
        q = "S=" + tok + "".join("&%s=%s" % (k, urllib.parse.quote(str(v), encoding="cp932"))
                                 for k, v in kw.items() if v is not None)
        return "dbbbs.fcgi?" + q              # relative: the PS2 mangles '/'-rooted links

    def nav(self, req, tok, sess):
        n = [("Boards", self.url(req, tok))]
        if not (sess and sess["handle"]):
            n.append(("Log in", self.url(req, tok, p_login="")))
        return n

    def thread(self, tid):
        return next((t for t in self.state.data["threads"] if t["id"] == tid), None)

    def per_page(self, req):
        return 10 if req.ps2 else 50

    # ------------------------------------------------------------------ dispatch
    def handle(self, req):
        if not (req.path.endswith("dbbbs.fcgi") or req.path == "/"):
            return Response("<html><body>Not found.</body></html>", 404)
        p = req.params
        if "hn" in p:                                        # PS2 entry: auto login
            acct = login(self.accounts, p.get("hn"), p.get("pw", ""))
            tok = self.sessions.new(acct["id"] if acct else None)
            return self.rules(req, tok, failed=not acct)
        sess = self.sessions.get(p.get("S"))
        if not sess:
            return self.rules(req, self.sessions.new())
        tok = p["S"]
        if "agree" in p:
            sess["agreed"] = True
        if not sess["agreed"]:
            return self.rules(req, tok)
        if "p_login" in p:
            return self.do_login(req, tok, sess)
        if "p_res" in p or "p_new" in p:
            return self.post(req, tok, sess)
        if "of" in p:
            return self.view_thread(req, tok, sess)
        if "b" in p:
            return self.board(req, tok, sess)
        return self.index(req, tok, sess)

    # ------------------------------------------------------------------ pages
    def rules(self, req, tok, failed=False):
        body = ("<p>Welcome to the Clubhouse BBS.</p>"
                "<p>Rules: be kind to other golfers, no personal information, no advertising. "
                "Posts that break the rules may be removed.</p>")
        if failed:
            body += "<p><b>Your ID or password did not match, so you can read but not post.</b></p>"
        body += '<p><a href="%s">I agree</a></p>' % esc(self.url(req, tok, agree=""))
        return Response(page(req, "Clubhouse BBS rules", body))

    def index(self, req, tok, sess):
        rows = []
        for b in self.state.data["boards"]:
            ts = [t for t in self.state.data["threads"] if t["board"] == b["id"]]
            rows.append('<tr><td><a href="%s">%s</a></td><td>%d threads</td></tr>'
                        % (esc(self.url(req, tok, b=b["id"])), esc(b["name"]), len(ts)))
        who = ("Logged in as <b>%s</b>." % esc(sess["handle"])) if sess["handle"] else "Not logged in (read only)."
        body = '<p class="note">%s</p><table class="list">%s</table>' % (who, "".join(rows))
        return Response(page(req, "Clubhouse BBS", body, self.nav(req, tok, sess)))

    def board(self, req, tok, sess):
        try:
            bid, pg = int(req.params["b"]), max(0, int(req.params.get("pg", 0)))
        except ValueError:
            return self.index(req, tok, sess)
        board = next((b for b in self.state.data["boards"] if b["id"] == bid), None)
        if not board:
            return self.index(req, tok, sess)
        ts = sorted((t for t in self.state.data["threads"] if t["board"] == bid),
                    key=lambda t: t.get("last_sort", t["last"]), reverse=True)
        chunk = ts[pg * THREADS_PER_PAGE:(pg + 1) * THREADS_PER_PAGE]
        rows = "".join('<tr><td><a href="%s">%s</a></td><td>%s</td><td>%d</td><td>%s</td></tr>'
                       % (esc(self.url(req, tok, of=t["id"])), esc(t["title"]), esc(t["author"]),
                          len(self.state.data["posts"].get(str(t["id"]), [])), esc(t["last"]))
                       for t in chunk)
        body = ('<table class="list"><tr><th>Thread</th><th>Started by</th><th>Posts</th><th>Last post</th></tr>%s</table>'
                % rows)
        more = []
        if pg:
            more.append('<a href="%s">Newer threads</a>' % esc(self.url(req, tok, b=bid, pg=pg - 1)))
        if len(ts) > (pg + 1) * THREADS_PER_PAGE:
            more.append('<a href="%s">Older threads</a>' % esc(self.url(req, tok, b=bid, pg=pg + 1)))
        if sess["handle"]:
            more.append('<a href="%s">Start a new thread</a>' % esc(self.url(req, tok, p_new="", b=bid)))
        body += "<p>%s</p>" % " | ".join(more)
        return Response(page(req, board["name"], body, self.nav(req, tok, sess)))

    def view_thread(self, req, tok, sess):
        try:
            tid = int(req.params["of"])
        except ValueError:
            return self.index(req, tok, sess)
        t = self.thread(tid)
        if not t:
            return self.index(req, tok, sess)
        posts = self.state.data["posts"].get(str(tid), [])
        starter, replies = posts[:1], posts[1:]
        n = self.per_page(req)
        last_pg = max(0, (len(replies) - 1) // n)
        try:
            pg = min(max(0, int(req.params.get("pg", last_pg))), last_pg)
        except ValueError:
            pg = last_pg
        shown = replies[pg * n:(pg + 1) * n]
        out = []
        for p in starter + shown:
            label = "" if p["no"] == 0 else "NO.%d " % p["no"]
            arch = ' <span class="arch">[archived]</span>' if p.get("source") == "wayback" else ""
            out.append('<div class="post"><div class="h">%sNickname: <b>%s</b> Posted: %s%s</div>%s</div>'
                       % (label, esc(p["author"]), esc(p["date"]), arch, text_html(p["body"])))
            if req.ps2:
                out.append("<hr>")
        if t.get("source", "").startswith("wayback"):
            out.insert(1, '<p class="note">Only part of this thread survives in the Internet Archive '
                          '(replies up to NO.%d existed).</p>' % (t["next_no"] - 1))
        links = []
        if pg < last_pg:
            links.append('<a href="%s">Newest</a>' % esc(self.url(req, tok, of=tid)))
            links.append('<a href="%s">Newer</a>' % esc(self.url(req, tok, of=tid, pg=pg + 1)))
        if pg > 0:
            links.append('<a href="%s">Older</a>' % esc(self.url(req, tok, of=tid, pg=pg - 1)))
            links.append('<a href="%s">First</a>' % esc(self.url(req, tok, of=tid, pg=0)))
        if sess["handle"]:
            links.append('<a href="%s">Reply</a>' % esc(self.url(req, tok, p_res="", of=tid)))
        links.append('<a href="%s">Back to board</a>' % esc(self.url(req, tok, b=t["board"])))
        body = "".join(out) + "<p>%s</p>" % " | ".join(links)
        return Response(page(req, t["title"], body, self.nav(req, tok, sess)))

    def do_login(self, req, tok, sess):
        p = req.params
        if req.method == "POST":
            acct = login(self.accounts, p.get("HANDLE", ""), p.get("PASS", ""))
            if acct:
                sess["handle"] = acct["id"]
                return Response.redirect(self.url(req, tok))
            msg = "<p><b>ID or password did not match.</b></p>"
        else:
            msg = ""
        body = (msg + '<form class="box" method="post" action="%s" accept-charset="Shift_JIS">'
                '<input type="hidden" name="S" value="%s"><input type="hidden" name="p_login" value="">'
                'Online ID (in-game name): <input name="HANDLE" size="16"><br>'
                'Password: <input name="PASS" type="password" size="12"><br>'
                '<input type="submit" value="Log in"></form>'
                '<p class="note">Use the same ID and password you log in with in the game.</p>'
                ) % ("dbbbs.fcgi", esc(tok))
        return Response(page(req, "Log in", body, self.nav(req, tok, sess)))

    def post(self, req, tok, sess):
        p = req.params
        new = "p_new" in p
        if not sess["handle"]:
            return self.do_login(req, tok, sess)
        if req.method == "POST":
            text = p.get("body", "").replace("\r\n", "\n").strip()[:MAX_BODY_CHARS]
            title = p.get("title", "").strip()[:MAX_TITLE_CHARS]
            if text and (title or not new):
                tid = self.add(sess["handle"], text, int(p.get("b", 1) or 1) if new else None,
                               title if new else None, None if new else int(p.get("of", 0) or 0))
                if tid:
                    return Response.redirect(self.url(req, tok, of=tid))
        hidden = "".join('<input type="hidden" name="%s" value="%s">' % (k, esc(v)) for k, v in
                         [("S", tok), ("p_new" if new else "p_res", ""),
                          ("b" if new else "of", p.get("b", "1") if new else p.get("of", ""))])
        title_in = 'Title: <input name="title" size="30" maxlength="40"><br>' if new else ""
        body = ('<form class="box" method="post" action="%s" accept-charset="Shift_JIS">%s%s'
                'Message:<br><textarea name="body" rows="8" cols="40"></textarea><br>'
                '<input type="submit" value="Post"></form>'
                ) % ("dbbbs.fcgi", hidden, title_in)
        return Response(page(req, "New thread" if new else "Reply", body, self.nav(req, tok, sess)))

    def add(self, handle, text, board=None, title=None, thread_id=None):
        """Append a reply (thread_id) or start a thread (board + title). Returns the thread id."""
        st = self.state
        with st.lock:
            now = jst_now()
            if thread_id is None:
                if not any(b["id"] == board for b in st.data["boards"]):
                    return None
                tid = st.data["next_thread_id"]
                st.data["next_thread_id"] += 1
                st.data["threads"].append({"id": tid, "board": board, "title": title, "author": handle,
                                           "created": now, "last": now, "next_no": 1, "source": "live"})
                st.data["posts"][str(tid)] = [{"no": 0, "author": handle, "date": now, "icon": "",
                                               "body": text, "source": "live"}]
            else:
                t = next((t for t in st.data["threads"] if t["id"] == thread_id), None)
                if not t:
                    return None
                tid = thread_id
                st.data["posts"].setdefault(str(tid), []).append(
                    {"no": t["next_no"], "author": handle, "date": now, "icon": "", "body": text,
                     "source": "live"})
                t["next_no"] += 1
                t["last"] = now
            st.save()
            return tid
