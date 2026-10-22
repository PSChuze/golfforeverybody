"""regi (register) + gate services.

regi: one request (type 0x1d, empty) -> world info (type 0x1d). Client then closes.
gate: world-date probe (msg 0x2c), char DB (msg 500), add user (0x1f5), login (msg 1) -> pushes
      msg 2 (auth), 0x1f6 (GAMEDB), 0x211 (USERDB), 3 (play-server list).
All gate messages ride transport type 5 (SYSTEM); the client sends secrets as 0x25 (secure).
"""
import socket
import struct

from . import accounts
from .net import Service

T_SYSTEM = 5
T_REGI = 0x1D
T_NEWS = 0x0E

WORLD_DATE = b"20031101"   # opaque to the client; only compared regi vs gate (0x2c)

ENTRY = 0xE0               # account record (char DB / add user / USERDB)


def cstr(s, n):
    b = s.encode("cp932", "replace") if isinstance(s, str) else s
    return b[:n - 1].ljust(n, b"\0")


def port_be(port):
    """Port slot: big-endian u16 in the first two bytes of a u32 (raw sin_port)."""
    return struct.pack(">H", port) + b"\0\0"


def entry(acct):
    rec = struct.pack("<I", acct["user_no"]) + cstr(acct["key"], 0x30) + cstr(acct["id"], 0x10)
    return rec.ljust(ENTRY, b"\0")


def u16(p, o):
    return struct.unpack_from("<H", p, o)[0]


def zstr(b):
    return b.split(b"\0", 1)[0].decode("cp932", "replace")


class Regi(Service):
    name = "regi"

    def __init__(self, public_ip, gate_port, world_name="Everybody's Golf", advertise_map=""):
        super().__init__(public_ip, advertise_map)
        self.gate_port = gate_port
        self.world_name = world_name
        self.on(T_REGI)(self.world_info)

    async def world_info(self, s, f):
        p = WORLD_DATE + struct.pack("<I", 1)
        p += socket.inet_aton(self.advertise(s.peer)) + port_be(self.gate_port) + cstr(self.world_name, 0x20)
        s.send(T_REGI, p)


class Gate(Service):
    name = "gate"

    def __init__(self, public_ip, play_port, store, news="", server_name="Lobby Server",
                 advertise_map=""):
        super().__init__(public_ip, advertise_map)
        self.play_port = play_port
        self.store = store
        self.news = news
        self.server_name = server_name
        self.on(T_NEWS)(self.on_news)
        self.on(T_SYSTEM)(self.on_system)

    async def on_news(self, s, f):
        if self.news:
            s.send(T_NEWS, self.news.encode("cp932", "replace") + b"\0")

    async def on_system(self, s, f):
        p = f.payload
        if len(p) < 2:
            return
        msg = u16(p, 0)
        h = {0x2C: self.world_date, 500: self.char_db, 0x1F5: self.add_user, 1: self.login}.get(msg)
        if h is None:
            s.log(event="unhandled-msg", msg=msg)
            return
        h(s, p)

    def world_date(self, s, p):
        s.send(T_SYSTEM, struct.pack("<H", 0x2C) + WORLD_DATE + b"\0" * 6)

    def char_db(self, s, p):
        key = zstr(p[0x10:0x40])
        s.state["key"] = key
        s.state["profile"] = p[0x4B:0x4B + 0xDE].hex()
        accts = self.store.by_key(key)[:5]
        s.log(event="char-db", key=key, count=len(accts))
        out = struct.pack("<H6sI4s", 500, b"", len(accts), b"") + b"".join(entry(a) for a in accts)
        s.send(T_SYSTEM, out)

    def add_user(self, s, p):
        key, uid, pw = zstr(p[0x10:0x40]), zstr(p[0x40:0x50]), zstr(p[0x50:0x5B])
        acct, result = self.store.create(key, uid, pw, p[0x5B:0x5B + 0xDE].hex())
        s.log(event="add-user", key=key, id=uid, result=result)
        rec = entry(acct) if acct else b"\0" * ENTRY
        s.send(T_SYSTEM, struct.pack("<HH12s", 0x1F5, result, b"") + rec)

    def login(self, s, p):
        result, acct = login_result(self.store, p)
        s.log(event="gate-login", user_no=struct.unpack_from("<I", p, 0x10)[0], result=result)
        s.send(T_SYSTEM, auth_reply(p, result, acct, s))
        if result < 0:
            return
        # GAMEDB: status 0, +0xc must be 0; an all-zero record bounces the menu silently
        s.send(T_SYSTEM, struct.pack("<HH8sI", 0x1F6, 0, b"", 0) + self.store.gamedb(acct))
        s.send(T_SYSTEM, struct.pack("<H14s", 0x211, b"") + entry(acct))
        s.send(T_SYSTEM, play_server(1, 1, self.advertise(s.peer), self.play_port, self.server_name))


def login_result(store, p):
    """Shared by gate and play: msg 1 = user_no +0x10, key +0x2c, password +0x5c, version +0x68."""
    if len(p) < 0x6C:
        return -10, None
    user_no = struct.unpack_from("<I", p, 0x10)[0]
    key, pw = zstr(p[0x2C:0x5C]), zstr(p[0x5C:0x67])
    acct = store.get(user_no)
    if not acct or acct["key"] != key:
        return -1, None
    if store.check_passwords and acct["pw"] != pw:
        return -1, None
    return 0, acct


def auth_reply(req, result, acct, s):
    """msg 2 mirrors the 0x6c-byte request; +4 session, +8 result, +0x10 user_no."""
    r = bytearray(req[:0x6C].ljust(0x6C, b"\0"))
    struct.pack_into("<H", r, 0, 2)
    struct.pack_into("<I", r, 4, id(s) & 0x7FFFFFFF)
    struct.pack_into("<i", r, 8, result)
    if acct:
        struct.pack_into("<I", r, 0x10, acct["user_no"])
    return bytes(r)


def play_server(index, total, ip, port, name, default=True):
    """msg 3: one frame per server, 0xaec bytes; index is 1-based."""
    p = bytearray(0xAEC)
    struct.pack_into("<HHHH", p, 0, 3, 0, index, total)
    p[0x0C:0x10] = socket.inet_aton(ip)
    struct.pack_into("<I", p, 0x10, 0x80000000 if default else 0)
    p[0x14:0x54] = cstr(name, 0x40)
    p[0x64:0x68] = port_be(port)
    # Lobby table: the Clubhouse lobby list is built from these records, not lobby.cfg. +0x68 u32
    # count; +0x6c 64 x 0x28 {u8 lobbyNo (0 hides), u8 category, u16 0, u32 cond (byte 0 = scene,
    # 10 = flagged players only), char name[0x20]}. The 16 x 8 category table at +0xa6c stays
    # zero: every lobby on the free-match tab. Category kind 2 puts a lobby on the Real
    # Tournament tab.
    struct.pack_into("<I", p, 0x68, len(LOBBIES))
    for i, (no, cat, scene, lname) in enumerate(LOBBIES):
        o = 0x6C + i * 0x28
        p[o], p[o + 1] = no, cat
        struct.pack_into("<I", p, o + 4, scene)
        p[o + 8:o + 0x28] = cstr(lname, 0x20)
    for cat, kind in CATEGORIES.items():
        struct.pack_into("<H", p, 0xA6C + cat * 8, kind)
    return bytes(p)


# (lobbyNo, category, cond scene byte, name). lobbyNo 5 is tours.TOUR_LOBBY, the venue the
# 0x2f4 tour map points at.
LOBBIES = [
    (1, 0, 0, "Lobby 1"),
    (2, 0, 1, "Lobby 2"),
    (3, 0, 2, "Lobby 3"),
    (4, 0, 4, "Lobby 4"),
    (5, 1, 3, "Tournament Lobby"),
]
CATEGORIES = {0: 0, 1: 2}
