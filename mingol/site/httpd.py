"""Tiny asyncio HTTP/1.0-1.1 server for the MGO website replica (www + bbs + Japan Tour API).

Everything the original served was Shift_JIS, and the PS2 browser (User-Agent CLAPHANZ/100)
percent-encodes form data in Shift_JIS too, so query strings and form bodies are decoded as
cp932 and every HTML page is encoded as cp932. Requests are routed by Host header:
bbs.* -> the BBS app, anything else -> the www app.
"""
import asyncio
import html
import re
import time
import urllib.parse

from ..net import _log

ENC = "cp932"
MAX_BODY = 256 * 1024


def esc(s):
    return html.escape(str(s), quote=True)


def parse_qs(raw):
    """Shift_JIS-aware query/body parser. Keys without '=' (P_Top, p_res) map to ''."""
    out = {}
    for part in raw.split("&"):
        if not part:
            continue
        k, _, v = part.partition("=")
        k = urllib.parse.unquote_plus(k, encoding=ENC, errors="replace")
        if k.startswith("amp;"):                 # a browser that kept '&amp;' from an href
            k = k[4:]
        v = urllib.parse.unquote_plus(v, encoding=ENC, errors="replace")
        out.setdefault(k, v)
    return out


class Request:
    def __init__(self, method, target, headers, body, peer):
        self.method = method
        self.path, _, self.query_string = target.partition("?")
        # The PS2 browser resolves '/x/y' links relative to the current folder ('/ps2//ps2/y'):
        # collapse doubled slashes and repeated leading folders.
        self.path = re.sub(r"/+", "/", self.path)
        self.path = re.sub(r"^((?:/[^/]+)+?)\1/", r"\1/", self.path)
        self.headers = headers
        self.body = body
        self.peer = peer
        self.host = headers.get("host", "").split(":")[0].lower()
        self.args = parse_qs(self.query_string)
        ctype = headers.get("content-type", "")
        self.form = {}
        if method == "POST" and "multipart/form-data" in ctype:
            self.form = parse_multipart(body, ctype)
        elif method == "POST":
            self.form = parse_qs(body.decode("latin1"))
        self.params = dict(self.args)
        self.params.update(self.form)

    @property
    def ps2(self):
        return "CLAPHANZ" in self.headers.get("user-agent", "") or self.path.startswith("/ps2/")


def parse_multipart(body, ctype):
    boundary = ctype.partition("boundary=")[2].strip().strip('"').encode("latin1")
    out = {}
    if not boundary:
        return out
    for part in body.split(b"--" + boundary):
        head, sep, data = part.partition(b"\r\n\r\n")
        if not sep:
            continue
        name = head.decode("latin1").partition('name="')[2].partition('"')[0]
        if name:
            out.setdefault(name, data.rstrip(b"\r\n-").decode(ENC, "replace"))
    return out


class Response:
    def __init__(self, body=b"", status=200, ctype="text/html; charset=Shift_JIS", headers=None):
        if isinstance(body, str):
            body = body.encode(ENC, "xmlcharrefreplace")
        self.body, self.status, self.ctype = body, status, ctype
        self.headers = headers or {}

    @classmethod
    def redirect(cls, location):
        return cls(b"", 302, headers={"Location": location})

    def bytes(self):
        reason = {200: "OK", 302: "Found", 400: "Bad Request", 404: "Not Found"}.get(self.status, "OK")
        h = ["HTTP/1.0 %d %s" % (self.status, reason), "Content-Type: " + self.ctype,
             "Content-Length: %d" % len(self.body), "Connection: close", "Cache-Control: no-cache"]
        h += ["%s: %s" % kv for kv in self.headers.items()]
        return ("\r\n".join(h) + "\r\n\r\n").encode("latin1") + self.body


class Site:
    """Host-routed HTTP front. `apps` = [(host predicate, app)]; app.handle(req) -> Response."""
    name = "web"

    def __init__(self, apps):
        self.apps = apps

    def route(self, req):
        for match, app in self.apps:
            if match(req.host):
                return app
        return self.apps[-1][1]

    async def handle(self, reader, writer):
        peer = (writer.get_extra_info("peername") or ("?",))[0]
        t0 = time.time()
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 30)
            lines = head.decode("latin1").split("\r\n")
            parts = lines[0].split(" ")
            method, target = parts[0], (parts[1] if len(parts) > 1 else "/")
            headers = {}
            for ln in lines[1:]:
                k, sep, v = ln.partition(":")
                if sep:
                    headers[k.strip().lower()] = v.strip()
            n = min(int(headers.get("content-length", "0") or 0), MAX_BODY)
            body = await asyncio.wait_for(reader.readexactly(n), 30) if n else b""
            req = Request(method, target, headers, body, peer)
            try:
                resp = self.route(req).handle(req)
            except Exception as e:                       # never let one page kill the listener
                _log(self.name, {"peer": peer, "event": "app-error", "err": repr(e), "path": req.path})
                resp = Response("<html><body>Server error.</body></html>", 200)
            writer.write(resp.bytes())
            await writer.drain()
            _log(self.name, {"peer": peer, "host": req.host, "method": method, "path": req.path,
                             "q": req.query_string[:120], "body": body[:200].decode("latin1"),
                             "status": resp.status, "reply_len": len(resp.body),
                             "ua": headers.get("user-agent", "")[:40], "ms": int((time.time() - t0) * 1000)})
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError,
                ConnectionError, ValueError) as e:
            _log(self.name, {"peer": peer, "event": "error", "err": repr(e)})
        finally:
            writer.close()

    async def serve(self, host, port):
        srv = await asyncio.start_server(self.handle, host, port)
        _log(self.name, {"event": "listen", "addr": "%s:%d" % (host, port)})
        return srv
