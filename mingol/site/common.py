"""Shared pieces of the website replica: sessions, logins, JSON state, page layouts.

Sessions mirror the original's opaque `S=` token: 16 random bytes as upper-case hex + "00",
mapped server-side to {handle, agreed}. Logins check the in-game ID + password (the game's
password) against the game servers' own account store, the same pair the PS2 sends as hn/pw or
HANDLE/PASS.
"""
import json
import os
import re
import secrets
import threading
import time

from .httpd import esc

SESSION_TTL = 6 * 3600


class Sessions:
    def __init__(self):
        self.s = {}

    def new(self, handle=None):
        tok = secrets.token_hex(16).upper() + "00"
        self.s[tok] = {"handle": handle, "agreed": False, "t": time.time()}
        return tok

    def get(self, tok):
        now = time.time()
        for k in [k for k, v in self.s.items() if now - v["t"] > SESSION_TTL]:
            del self.s[k]
        sess = self.s.get(tok or "")
        if sess:
            sess["t"] = now
        return sess


def login(store, handle, password):
    """Account dict when the game ID + password match, else None."""
    if not handle:
        return None
    for a in store.data["accounts"]:
        if a["id"] == handle and (not store.check_passwords or a["pw"] == password):
            return a
    return None


class JsonState:
    """A JSON document under the data dir, created from `seed()` on first use."""

    def __init__(self, path, seed):
        self.path = path
        self.lock = threading.Lock()
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                self.data = json.load(f)
        else:
            self.data = seed()
            self.save()

    def save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)


def jst_now():
    return time.strftime("%y/%m/%d %H:%M", time.gmtime(time.time() + 9 * 3600))


PC_CSS = """body{background:#e9f3d6;color:#222;font-family:'MS PGothic',Meiryo,sans-serif;margin:0}
#wrap{width:760px;margin:0 auto;background:#fff;border:1px solid #9bbf5a}
#head{background:#5f9a24;color:#fff;padding:10px 16px;font-size:20px;font-weight:bold}
#head small{font-size:12px;font-weight:normal;margin-left:10px}
#nav{background:#cfe5a8;padding:6px 16px;font-size:13px}#nav a{margin-right:12px;color:#234}
#main{padding:12px 16px 20px}h2{border-left:6px solid #5f9a24;padding-left:8px;font-size:17px}
table.list{border-collapse:collapse;width:100%}table.list td,table.list th{border-bottom:1px solid #ddd;padding:4px 6px;font-size:13px}
.post{border:1px solid #cde0ad;margin:8px 0;padding:6px 10px}.post .h{font-size:12px;color:#555;margin-bottom:4px}
.note{font-size:12px;color:#666}.arch{color:#888;font-size:11px}form.box{background:#f4f9ea;padding:8px;border:1px solid #cde0ad}
#foot{font-size:11px;color:#777;padding:8px 16px;border-top:1px solid #ddd}"""


PS2_COLUMNS = 46            # the PS2 browser never wraps; the original hard-wrapped bodies too


def _width(ch):
    return len(ch.encode("cp932", "replace"))      # full-width = 2 columns, ASCII/half-width kana = 1


def ps2_wrap(markup, columns=PS2_COLUMNS):
    """Insert <br> into text runs so no line exceeds `columns` display columns. Line state resets
    at block tags. Breaks at the last space when there is one, else mid-word (Japanese)."""
    import html as _h
    out, col = [], 0
    for part in re.split(r"(<[^>]+>)", markup):
        if part.startswith("<"):
            if re.match(r"</?(br|p|hr|tr|table|div|h\d|li|ul|form|textarea)\b", part, re.I):
                col = 0
            out.append(part)
            continue
        text = _h.unescape(part)
        line, buf = [], []
        for ch in text:
            w = _width(ch)
            if col + w > columns:
                sp = "".join(buf).rfind(" ")
                if sp > 0 and len(buf) - sp < columns // 2:
                    head, tail = "".join(buf[:sp]), "".join(buf[sp + 1:])
                    line.append(_h.escape(head, quote=False) + "<br>")
                    buf = list(tail)
                    col = sum(_width(c) for c in buf)
                else:
                    line.append(_h.escape("".join(buf), quote=False) + "<br>")
                    buf, col = [], 0
            buf.append(ch)
            col += w
        line.append(_h.escape("".join(buf), quote=False))
        out.append("".join(line))
    return "".join(out)


def ps2_links(markup):
    """The PS2 browser does not decode '&amp;' inside href/action: emit raw '&' there."""
    return re.sub(r'((?:href|action)=")([^"]*)"', lambda m: m.group(1) + m.group(2).replace("&amp;", "&") + '"',
                  markup)


def page(req, title, body, nav=None, site="Everybody's Golf Online"):
    """PS2: bare, narrow, no CSS/JS/GIF. PC: a 760 px column."""
    nav = nav or []
    if req.ps2:
        links = " | ".join('<a href="%s">%s</a>' % (esc(u), esc(t)) for t, u in nav)
        # two blank lines first: the browser's button panel covers the top of the page
        inner = "<br><br><b>%s</b><br>%s<hr>%s<hr>%s" % (esc(title), links, body, esc(site))
        return ('<html><head><meta http-equiv="Content-Type" content="text/html; charset=Shift_JIS">'
                "<title>%s</title></head><body>%s</body></html>"
                % (esc(site).replace("&#x27;", "'"), ps2_links(ps2_wrap(inner))))
    links = "".join('<a href="%s">%s</a>' % (esc(u), esc(t)) for t, u in nav)
    return ('<!DOCTYPE html><html><head><meta charset="Shift_JIS"><title>%s - %s</title>'
            "<style>%s</style></head><body><div id=\"wrap\">"
            '<div id="head">%s<small>fan server</small></div>'
            '<div id="nav">%s</div><div id="main"><h2>%s</h2>%s</div>'
            '<div id="foot">A fan server, not affiliated with or endorsed by Sony Interactive Entertainment or Clap Hanz.</div>'
            "</div></body></html>"
            ) % (esc(title), esc(site), PC_CSS, esc(site), links, esc(title), body)


def text_html(s):
    """Plain text -> HTML with line breaks; quoted lines ('>') in gray italics like the original."""
    out = []
    for line in str(s).split("\n"):
        e = esc(line)
        out.append("<font color=\"gray\"><i>%s</i></font>" % e if line.startswith((">", "＞")) else e)
    return "<br>".join(out)
