"""Japan Tour HTTP endpoint: POST /tour/mgzantei.fcgi.

The three requests share one URL and differ only in the urlencoded body:
  P_Max                          -> number of tours ("1")
  P_Regu&tour_code=<k>,          -> one hex line per 0x50c record, '\n' terminated
  P_Record&Record=..&Attest=..   -> "0\n" (code 0 = accepted)
The client accepts only status 200. An install whose ADDRESS.XB points JAPANTOUR_SERVER at port
12105 reaches this directly; one that keeps port 80 gets here through the web server in front.
"""
import asyncio
import json
import os
import time

from . import tours
from .net import _log


class JapanTour:
    name = "web"

    def __init__(self, records_path=None):
        self.records_path = records_path

    def answer(self, body):
        b = body.decode("latin1", "replace").strip()
        if b.startswith("P_Max"):
            return b"1"
        if b.startswith("P_Regu"):
            codes = b.partition("tour_code=")[2].split(",")
            if str(tours.JAPAN_TOUR_CODE) in codes:
                return tours.japan_tour_record().hex().upper().encode() + b"\n"
            return b""
        if b.startswith("P_Record"):
            self.store_record(b)
            return b"0\n"
        return b""

    def store_record(self, body):
        if not self.records_path:
            return
        os.makedirs(os.path.dirname(self.records_path) or ".", exist_ok=True)
        rec = {"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "body": body}
        with open(self.records_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")

    async def handle(self, reader, writer):
        peer = (writer.get_extra_info("peername") or ("?",))[0]
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 30)
            lines = head.decode("latin1").split("\r\n")
            method, path = (lines[0].split(" ") + ["", ""])[:2]
            headers = {k.strip().lower(): v.strip() for k, _, v in
                       (ln.partition(":") for ln in lines[1:] if ln)}
            n = int(headers.get("content-length", "0") or 0)
            body = await asyncio.wait_for(reader.readexactly(n), 30) if n else b""
            out = self.answer(body) if method == "POST" else b""
            _log(self.name, {"peer": peer, "method": method, "path": path,
                             "body": body[:200].decode("latin1"), "reply_len": len(out)})
            writer.write(b"HTTP/1.0 200 OK\r\nContent-Type: text/plain\r\n"
                         b"Content-Length: %d\r\nConnection: close\r\n\r\n" % len(out) + out)
            await writer.drain()
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError, ValueError) as e:
            _log(self.name, {"peer": peer, "event": "error", "err": repr(e)})
        finally:
            writer.close()

    async def serve(self, host, port):
        srv = await asyncio.start_server(self.handle, host, port)
        _log(self.name, {"event": "listen", "addr": "%s:%d" % (host, port)})
        return srv
