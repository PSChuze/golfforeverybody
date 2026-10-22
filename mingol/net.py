"""Shared asyncio plumbing for the dotcomm servers (regi, gate, play).

A Service subclass maps app frame types to handler coroutines. Every frame in both
directions is logged to logs/<service>.jsonl with its decoded payload, so a live run doubles
as a capture. Unknown types are logged and ignored (never answered with guesses).
"""
import asyncio
import ipaddress
import json
import os
import socket
import time

from . import dotcomm

DEFAULT_LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")


def _log(service, rec):
    log_dir = os.environ.get("MINGOL_LOG_DIR") or DEFAULT_LOG_DIR
    os.makedirs(log_dir, exist_ok=True)
    rec["t"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    line = json.dumps(rec)
    with open(os.path.join(log_dir, service + ".jsonl"), "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(service, line, flush=True)


def parse_advertise_map(spec):
    """"192.168.0.0/16=192.168.0.10,0.0.0.0/0=203.0.113.7" -> [(network, ip), ...]"""
    out = []
    for item in filter(None, (x.strip() for x in (spec or "").split(","))):
        cidr, addr = item.split("=", 1)
        out.append((ipaddress.ip_network(cidr.strip()), addr.strip()))
    return out


class Session:
    def __init__(self, service, reader, writer):
        self.service = service
        self.reader = reader
        self.writer = writer
        peer = writer.get_extra_info("peername") or ("?", 0)
        self.peer = peer[0]
        self.server_ip = socket.inet_aton(service.advertise(self.peer))
        self.state = {}

    def log(self, **rec):
        rec.setdefault("peer", self.peer)
        _log(self.service.name, rec)

    def send(self, ftype, payload=b"", secure=False):
        raw = dotcomm.build(ftype, payload, self.server_ip, secure)
        self.log(dir="S>C", type=ftype, secure=secure, len=len(payload), hex=payload.hex())
        self.writer.write(raw)

    async def run(self):
        self.log(event="connect")
        buf = bytearray()
        try:
            while True:
                data = await asyncio.wait_for(self.reader.read(65536), self.service.idle_timeout)
                if not data:
                    break
                buf += data
                while True:
                    try:
                        f, n = dotcomm.parse(buf)
                    except ValueError as e:
                        self.log(event="bad-frame", err=str(e), hex=bytes(buf[:64]).hex())
                        return
                    if not f:
                        break
                    del buf[:n]
                    self.log(dir="C>S", type=f.type, secure=f.secure, len=len(f.payload),
                             ip=socket.inet_ntoa(f.ip), hex=f.payload.hex())
                    await self.dispatch(f)
                await self.writer.drain()
        except (asyncio.TimeoutError, ConnectionError) as e:
            self.log(event="error", err=repr(e))
        finally:
            await self.service.on_close(self)
            self.log(event="close")
            self.writer.close()

    async def dispatch(self, f):
        if f.type == dotcomm.T_KEEPALIVE:
            self.send(dotcomm.T_KEEPALIVE)
            return
        # The client sends an empty 0x0a after 30 s without a frame and drops a
        # lobby/play socket after 60 s. A solo round (the
        # Japan Tour) sends nothing for minutes, so answer with an empty 0x0a:
        # the client discards it but it resets the timer. Not 0x0c, which the
        # client echoes and we echo back.
        if f.type == dotcomm.T_OPEN:
            self.send(dotcomm.T_OPEN)
            return
        h = self.service.handlers.get(f.type)
        if h is None:
            self.log(event="unhandled", type=f.type)
            return
        await h(self, f)


class Service:
    name = "service"
    idle_timeout = 600

    def __init__(self, public_ip, advertise_map=""):
        self.public_ip = public_ip
        self.advertise_map = parse_advertise_map(advertise_map)
        self.handlers = {}

    def advertise(self, peer):
        """The address to hand THIS client for our next server: first CIDR in the map that
        contains the client's source address wins, else public_ip. This needs the client's real
        source address: forward the ports without SNAT (Docker Desktop hides it)."""
        try:
            ip = ipaddress.ip_address(peer)
        except ValueError:
            return self.public_ip
        for net, addr in self.advertise_map:
            if ip in net:
                return addr
        return self.public_ip

    def on(self, ftype):
        def deco(fn):
            self.handlers[ftype] = fn
            return fn
        return deco

    async def on_close(self, session):
        pass

    async def serve(self, host, port):
        async def accept(r, w):
            await Session(self, r, w).run()
        srv = await asyncio.start_server(accept, host, port)
        _log(self.name, {"event": "listen", "addr": "%s:%d" % (host, port)})
        return srv
