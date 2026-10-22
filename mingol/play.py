"""play server: lobbies, rooms, round start, in-round relay.

Commands ride transport type 5 with a 16-byte header {u16 cmd; u16 sub; u32 a; u32 b; u32 c}.
Bytes the client does not explicitly fill are stack garbage: never trust them.
"""
import asyncio
import socket
import struct
import time

from . import tours
from .accounts import Store
from .net import Service
from .regi_gate import auth_reply, cstr, login_result, zstr

T_DATA, T_CHAT, T_SYSTEM, T_SLASH, T_DATA_NOLOG = 1, 2, 5, 6, 7

LOBBY_SLOTS, LOBBY_SLOT_SIZE = 30, 0x20
ROOM_SLOTS, ROOM_SLOT_SIZE = 4, 0x20
DEFAULT_LOBBY = 1          # where refusals/kicks land a player that never picked a lobby
POP_CAP = 29               # the pick check refuses a lobby reported at 30+
MEMBER_REC = 0x88          # cmd 0x0b member record; +0x6c playerId
START_HDR = 0x84


def cmd(c, sub=0, a=0, b=0, cc=0, body=b""):
    return struct.pack("<HHIII", c, sub & 0xFFFF, a & 0xFFFFFFFF, b & 0xFFFFFFFF, cc & 0xFFFFFFFF) + body


def u32(p, o):
    return struct.unpack_from("<I", p, o)[0] if len(p) >= o + 4 else 0


def u16(p, o):
    return struct.unpack_from("<H", p, o)[0] if len(p) >= o + 2 else 0


class Player:
    def __init__(self, s, acct):
        self.s = s
        self.id = acct["user_no"]
        self.name = acct["id"]
        self.where = None       # Lobby or Room
        self.slot = -1
        self.lobby_no = DEFAULT_LOBBY
        self.accepted = False


class Container:
    """Shared shape of a lobby and a room: slots + common memory echoed to every member."""
    def __init__(self, nslots, slot_size):
        self.slots = [None] * nslots
        self.mem = bytearray(nslots * slot_size)
        self.slot_size = slot_size

    def members(self):
        return [p for p in self.slots if p]

    def free_slot(self):
        return next((i for i, p in enumerate(self.slots) if p is None), -1)


class Lobby(Container):
    kind = 0

    def __init__(self, no):
        super().__init__(LOBBY_SLOTS, LOBBY_SLOT_SIZE)
        self.no = no


class Room(Container):
    kind = 1

    def __init__(self, rid, lobby_no, max_players):
        super().__init__(ROOM_SLOTS, ROOM_SLOT_SIZE)
        self.id = rid
        self.lobby_no = lobby_no
        self.max = max(1, min(4, max_players))
        self.cond = bytearray(0x40)
        self.owner = None
        self.caption = ""
        self.open = True
        self.started = False
        self.start_msg = b""


class TourRoom(Container):
    """Real-tournament room: 100 slots x 0x80 of common memory (live scores), keyed by tourId."""
    kind = 1

    def __init__(self, tour_id):
        super().__init__(100, 0x80)
        self.id = tour_id
        self.lobby_no = tours.TOUR_LOBBY
        self.holes_sent = 0


class Play(Service):
    name = "play"

    def __init__(self, public_ip, store: Store, advertise_map=""):
        super().__init__(public_ip, advertise_map)
        self.store = store
        self.players = {}       # id -> Player
        self.lobbies = {}
        self.rooms = {}
        self.tour_rooms = {}    # tourId -> TourRoom
        self.reserved = {}      # tourId -> set of player ids
        self.next_room = 1
        self.on(T_SYSTEM)(self.on_system)
        self.on(T_CHAT)(self.on_chat)
        self.on(T_SLASH)(self.on_slash)
        self.on(T_DATA)(self.on_data)
        self.on(T_DATA_NOLOG)(self.on_data)
        self.cmds = {
            1: self.c_auth, 9: self.c_enter, 0x0C: self.c_leave, 0x262: self.c_common,
            0x1F: self.c_rule, 0x20: self.c_accept, 0x0F: self.c_game_end, 0x34: self.c_time,
            0x2FA: self.c_player_num, 0x385: self.c_search_players, 0x387: self.c_search_rooms,
            0x1F6: self.c_player_record, 0x2F1: self.c_send_gamedb, 0x201: self.c_lookup,
            1000: self.c_record_board, 0x320: self.c_friend_add, 0x321: self.c_friend_reply,
            0x324: self.c_mail, 0x29: self.c_dm, 0x2F5: self.c_tour_reserve, 0x2FB: self.c_tour_data,
            0x2F0: self.c_kick, 0x14: self.c_knock, 0x16: self.c_knock_answer,
        }
        # acknowledged-by-silence: the client ignores any reply
        self.silent = {0x384, 0x325, 0x200, 0x23, 0x24}

    # ---------------------------------------------------------------- plumbing
    def lobby(self, no):
        if no not in self.lobbies:
            self.lobbies[no] = Lobby(no)
        return self.lobbies[no]

    def me(self, s):
        return s.state.get("player")

    async def on_system(self, s, f):
        p = f.payload
        c = u16(p, 0)
        if c in self.silent:
            return
        h = self.cmds.get(c)
        if h is None:
            s.log(event="unhandled-cmd", cmd=c)
            return
        if c != 1 and not self.me(s):
            s.log(event="cmd-before-auth", cmd=c)
            return
        h(s, p)

    async def on_close(self, s):
        p = self.me(s)
        if p and self.players.get(p.id) is p:
            self.detach(p)
            del self.players[p.id]

    # ---------------------------------------------------------------- auth
    def c_auth(self, s, p):
        result, acct = login_result(self.store, p)
        if acct and acct["user_no"] in self.players:
            old = self.players[acct["user_no"]]
            if old.s is not s:
                result = -2        # double login
        s.log(event="play-auth", result=result, user_no=u32(p, 0x10))
        reply = bytearray(auth_reply(p, result, acct, s))
        if acct:
            reply[0x14:0x24] = cstr(acct["id"], 0x10)   # handle used in 0x29/0x201
        s.send(T_SYSTEM, bytes(reply))
        if result < 0:
            return
        pl = Player(s, acct)
        s.state["player"] = pl
        self.players[pl.id] = pl
        self.send_time(pl)
        self.to_server(pl)
        self.send_tours(pl)

    def send_time(self, pl):
        t = time.gmtime(time.time() + tours.JST)   # JST wall time: no +9 h on the client side
        tm = struct.pack("<9i", t.tm_sec, t.tm_min, t.tm_hour, t.tm_mday, t.tm_mon - 1,
                         t.tm_year - 1900, (t.tm_wday + 1) % 7, t.tm_yday - 1, 0)
        pl.s.send(T_SYSTEM, cmd(0x33, body=tm))

    def c_time(self, s, p):
        s.send(T_SYSTEM, cmd(0x34, 0, u32(p, 4), int(time.time())))

    # ---------------------------------------------------------------- containers
    def detach(self, pl):
        where = pl.where
        if where is None:
            return
        slot = pl.slot
        where.slots[slot] = None
        where.mem[slot * where.slot_size:(slot + 1) * where.slot_size] = bytes(where.slot_size)
        pl.where, pl.slot, pl.accepted = None, -1, False
        for o in where.members():
            o.s.send(T_SYSTEM, cmd(0x1C, 0, where.kind, slot, body=b"\0" * 8 + struct.pack("<I", pl.id)))
        if isinstance(where, Room):
            self.room_left(where, pl, slot)
        elif isinstance(where, TourRoom):
            self.broadcast_vacancies([where.id])
        else:
            self.broadcast_populations()

    def attach(self, pl, where, slot):
        pl.where, pl.slot = where, slot
        where.slots[slot] = pl
        # newcomer learns the existing members and their common memory
        for o in where.members():
            if o is not pl:
                pl.s.send(T_SYSTEM, self.joined(where, o))
                seg = where.mem[o.slot * where.slot_size:(o.slot + 1) * where.slot_size]
                if any(seg):
                    pl.s.send(T_SYSTEM, cmd(0x262, 0, len(seg), o.slot * where.slot_size, body=bytes(seg)))
        # everybody (newcomer included, it needs its own 0x1b) learns the newcomer
        for o in where.members():
            o.s.send(T_SYSTEM, self.joined(where, pl))

    def joined(self, where, pl):
        body = b"\0" * 8 + struct.pack("<I", pl.id)
        if where.kind == 1:
            body += cstr(pl.name, 0x10)
        return cmd(0x1B, 0, where.kind, pl.slot, body=body)

    def to_server(self, pl):
        """cmd 9 lobbyNo 0 = on the server, in no lobby: what m_lobsel asks for while it shows
        the lobby list. No own 0x1b in this case."""
        self.detach(pl)
        pl.s.send(T_SYSTEM, cmd(9, 0, 0, 0, 0))
        self.broadcast_populations()

    def enter_lobby(self, pl, no):
        if not no:
            self.to_server(pl)
            return
        lob = self.lobby(no)
        slot = lob.free_slot()
        if slot < 0 and pl.where is not lob:
            lob = next((l for l in self.lobbies.values() if l.free_slot() >= 0), None) or self.lobby(len(self.lobbies) + 1)
            slot = lob.free_slot()
        self.detach(pl)
        pl.lobby_no = lob.no
        pl.s.send(T_SYSTEM, cmd(9, 0, 0, lob.no, 0))
        self.attach(pl, lob, slot)
        self.broadcast_populations()

    def broadcast_populations(self):
        """cmd 0x2fc: one 0x80 record per play server (we are one), u16 pop[lobbyNo]."""
        pop = [0] * 64
        for no, lob in self.lobbies.items():
            if no < 64:
                pop[no] = min(len(lob.members()), POP_CAP)
        msg = cmd(0x2FC, 0, 0, 1, body=struct.pack("<64H", *pop))
        for o in self.players.values():
            o.s.send(T_SYSTEM, msg)

    def c_enter(self, s, p):
        pl = self.me(s)
        is_room, room_id, lobby_no = u16(p, 2), u32(p, 4), u32(p, 8)
        if not is_room:
            self.enter_lobby(pl, lobby_no)
            return
        if u16(p, 0x1A):                                   # EnterTourRoom (tourRoom=1)
            self.join_tour(pl, room_id)
            return
        if u16(p, 0x16):                                   # create
            r = Room(self.next_room, pl.lobby_no, u16(p, 0x10))
            self.next_room += 1
            r.cond[:] = p[0x24:0x64].ljust(0x40, b"\0")[:0x40]
            r.owner = pl
            self.rooms[r.id] = r
            self.join_room(pl, r)
            return
        r = self.rooms.get(room_id)
        if not r or r.started or len(r.members()) >= r.max or r.free_slot() < 0:
            s.log(event="room-refused", room=room_id)
            self.enter_lobby(pl, pl.lobby_no)               # refusal = back to the lobby
            return
        if not r.open and r.owner:
            s.state["knock_room"] = r.id
            s.send(T_SYSTEM, cmd(0x13))
            return
        self.join_room(pl, r)

    def join_room(self, pl, r):
        self.detach(pl)
        pl.s.send(T_SYSTEM, cmd(9, 0, 0, 0, r.id))
        self.attach(pl, r, r.free_slot())
        pl.s.send(T_SYSTEM, struct.pack("<HH", 0x1D, 0) + bytes(r.cond))   # all rules at +4
        for o in r.members():
            o.s.send(T_SYSTEM, cmd(600, body=b"\0" * 8 + struct.pack("<I", r.owner.id)))

    def room_left(self, r, pl, slot):
        if not r.members():
            self.rooms.pop(r.id, None)
            return
        if r.owner is pl:
            r.owner = min(r.members(), key=lambda m: m.slot)
            for o in r.members():
                o.s.send(T_SYSTEM, cmd(600, body=b"\0" * 8 + struct.pack("<I", r.owner.id)))
        if r.started:
            self.send_member_states(r, dropped_slot=slot)

    def c_leave(self, s, p):
        pl = self.me(s)
        s.send(T_SYSTEM, cmd(0x0C))
        self.detach(pl)
        self.players.pop(pl.id, None)
        s.state.pop("player", None)

    # ---------------------------------------------------------------- shared memory, chat, data
    def c_common(self, s, p):
        pl = self.me(s)
        where = pl.where
        if not where:
            return
        n, off = u32(p, 4), u32(p, 8)
        data = p[0x10:0x10 + n]
        if off + len(data) <= len(where.mem):
            where.mem[off:off + len(data)] = data
        for o in where.members():                # echo is mandatory, sender included
            o.s.send(T_SYSTEM, p)

    async def on_chat(self, s, f):
        pl = self.me(s)
        if pl and pl.where:
            for o in pl.where.members():
                o.s.send(T_CHAT, f.payload)

    async def on_slash(self, s, f):
        pl = self.me(s)
        text = zstr(f.payload)
        r = pl.where if pl else None
        if not isinstance(r, Room):
            return
        if text.startswith("/caption "):
            r.caption = text[9:]
        elif text == "/open":
            r.open = True
        elif text == "/close":
            r.open = False

    async def on_data(self, s, f):
        """In-round / peer data: relay to every other member as type 1, never echo."""
        pl = self.me(s)
        if not pl or not pl.where or not f.payload:
            return
        for o in pl.where.members():
            if o is not pl:
                o.s.send(T_DATA, f.payload)

    # ---------------------------------------------------------------- room rules and round start
    def c_rule(self, s, p):
        pl = self.me(s)
        r = pl.where
        if not isinstance(r, Room) or r.owner is not pl:
            return
        value, index = u32(p, 8) & 0xFFFF, u32(p, 0xC)
        if 1 <= index < 32:
            struct.pack_into("<H", r.cond, index * 2, value)
        for o in r.members():
            o.accepted = False
            o.s.send(T_SYSTEM, cmd(0x1F, 0, 0, value, index))

    def c_accept(self, s, p):
        pl = self.me(s)
        r = pl.where
        if not isinstance(r, Room):
            return
        pl.accepted = True
        for o in r.members():
            o.s.send(T_SYSTEM, cmd(0x21, 0, 1, body=b"\0" * 8 + struct.pack("<I", pl.id)))
        if not r.started and all(m.accepted for m in r.members()):
            self.start_round(r)

    def start_round(self, r):
        members = sorted(r.members(), key=lambda m: m.slot)
        hdr = bytearray(START_HDR)
        struct.pack_into("<H", hdr, 0, 0x0B)
        struct.pack_into("<H", hdr, 0x16, len(members))
        hdr[0x1C:0x5C] = r.cond
        recs = b""
        for m in members:
            rec = bytearray(MEMBER_REC)
            struct.pack_into("<I", rec, 0x6C, m.id)
            rec[0:0x10] = cstr(m.name, 0x10)
            recs += rec
        r.start_msg = bytes(hdr) + recs
        r.started = True
        r.order = members
        for m in members:
            m.s.send(T_SYSTEM, r.start_msg)
        self.send_member_states(r)

    def send_member_states(self, r, dropped_slot=None):
        """cmd 0x26 per position (recipient-relative: 0 own, 1 proxy, 2 other, -1 none) + 0x27.
        On a drop, the owner proxies the dropped position (inferred)."""
        order = getattr(r, "order", [])
        for m in r.members():
            for pos, who in enumerate(order):
                if who is m:
                    st = 0
                elif who.where is r:
                    st = 2
                else:
                    st = 1 if m is r.owner else 2
                m.s.send(T_SYSTEM, cmd(0x26, 0, pos, 0, st))
            m.s.send(T_SYSTEM, cmd(0x27))

    def c_game_end(self, s, p):
        pl = self.me(s)
        r = pl.where
        if not isinstance(r, Room):
            return
        r.started = False
        for m in r.members():
            m.accepted = False
            m.s.send(T_SYSTEM, p)

    def c_player_num(self, s, p):
        pl = self.me(s)
        r = pl.where
        ok = isinstance(r, Room) and r.owner is pl and len(r.members()) <= u32(p, 8) <= 4
        if ok:
            r.max = u32(p, 8)
        s.send(T_SYSTEM, cmd(0x2FA, 0, 0 if ok else 1))

    def c_kick(self, s, p):
        pl = self.me(s)
        r = pl.where
        target = self.players.get(u32(p, 8))
        if isinstance(r, Room) and r.owner is pl and target and target.where is r:
            self.enter_lobby(target, target.lobby_no)

    # ---------------------------------------------------------------- knock (closed rooms)
    def c_knock(self, s, p):
        r = self.rooms.get(s.state.get("knock_room"))
        if r and r.owner:
            r.owner.s.send(T_SYSTEM, cmd(0x15, body=p[0x10:0x34]))

    def c_knock_answer(self, s, p):
        s.send(T_SYSTEM, cmd(0x16))
        knocker = self.players.get(u32(p, 0x10 + 8))
        r = self.me(s).where
        if not knocker or not isinstance(r, Room) or knocker.s.state.pop("knock_room", None) != r.id:
            return
        if u32(p, 8) and r.free_slot() >= 0 and not r.started:
            self.join_room(knocker, r)
        else:
            self.enter_lobby(knocker, knocker.lobby_no)

    # ---------------------------------------------------------------- searches, info
    def c_search_players(self, s, p):
        s.send(T_SYSTEM, cmd(0x385, 0, 0))

    def c_search_rooms(self, s, p):
        pl = self.me(s)
        recs = b""
        n = 0
        for r in self.rooms.values():
            if r.lobby_no == pl.lobby_no and r.open and not r.started and len(r.members()) < r.max:
                rec = bytearray(0x80)
                struct.pack_into("<H", rec, 0, r.id)
                recs += rec
                n += 1
        s.send(T_SYSTEM, cmd(0x387, 0, 0, n, body=recs))

    def c_player_record(self, s, p):
        target = u32(p, 0xC)
        acct = self.store.get(target)
        rec = self.store.gamedb(acct) if acct else b"\0" * 0xA0
        s.send(T_SYSTEM, cmd(0x1F6, 0 if acct else 1, 0, 0, target or 1, body=rec))

    def c_send_gamedb(self, s, p):
        self.store.set_gamedb(self.me(s).id, p[0x10:0xB0])
        s.send(T_SYSTEM, cmd(0x2F1, 0))

    def c_lookup(self, s, p):
        if u16(p, 0x28):
            o = self.players.get(u32(p, 0x2C))
        else:
            name = zstr(p[0x30:0x40])
            o = next((x for x in self.players.values() if x.name == name), None)
        if not o:
            s.send(T_SYSTEM, cmd(0x201, 1))
            return
        summary = struct.pack("<I", o.id) + cstr(o.name, 0x10)
        s.send(T_SYSTEM, cmd(0x201, 0, body=summary.ljust(0x24, b"\0")))

    def c_record_board(self, s, p):
        s.send(T_SYSTEM, cmd(1000, body=b"\0" * 0xB40))

    # ---------------------------------------------------------------- social (minimal)
    def c_friend_add(self, s, p):
        s.send(T_SYSTEM, cmd(0x320, 4))           # not supported yet -> client error text

    def c_friend_reply(self, s, p):
        s.send(T_SYSTEM, cmd(0x321, 4))

    def c_mail(self, s, p):
        s.send(T_SYSTEM, cmd(0x324, 3 if len(p) == 0x10 else 6))

    def c_dm(self, s, p):
        pl = self.me(s)
        target = self.players.get(u32(p, 0x2C))
        st = 0 if target else 1
        if target:
            text = p[0x40:]
            body = bytearray(0x40) + text
            struct.pack_into("<I", body, 0x14 - 0x10, 0)
            body[0x18 - 0x10:0x28 - 0x10] = cstr(pl.name, 0x10)
            target.s.send(T_SYSTEM, cmd(0x29, 0, pl.id, body=bytes(body)))
        ack = bytearray(cmd(0x29, 0, pl.id)) + bytearray(0x20)
        struct.pack_into("<H", ack, 0x2A, st)
        s.send(T_SYSTEM, bytes(ack))

    # ---------------------------------------------------------------- real tournaments
    def send_tours(self, pl):
        """0x2f4 map + 0x2f3 list + 0x301 vacancies for today's rolling schedule (sent once at
        auth: m_realtour keeps raw pointers into the list, so it is never resent mid-screen)."""
        sched = tours.schedule()
        pl.s.send(T_SYSTEM, cmd(0x2F4, 0, 0, len(sched), body=tours.map_records(sched)))
        pl.s.send(T_SYSTEM, cmd(0x2F3, 0, 0, len(sched), body=tours.tour_list(sched)))
        pl.s.send(T_SYSTEM, self.vacancies([tid for tid, _s in sched]))

    def vacancies(self, ids):
        body = b""
        for tid in ids:
            room = self.tour_rooms.get(tid)
            used = len(room.members()) if room else 0
            used += len(self.reserved.get(tid, ()))
            body += struct.pack("<IHH", tid, max(0, tours.CAPACITY - used), tours.CAPACITY)
        return cmd(0x301, 0, 0, len(ids), body=body)

    def broadcast_vacancies(self, ids):
        msg = self.vacancies(ids)
        for o in self.players.values():
            o.s.send(T_SYSTEM, msg)

    def join_tour(self, pl, tour_id):
        start = tours.start_of(tour_id)
        now = time.time()
        room = self.tour_rooms.get(tour_id)
        if not (start - tours.SPACING <= now < start - 30) or (room and room.free_slot() < 0):
            pl.s.log(event="tour-refused", tour=tour_id)
            self.enter_lobby(pl, pl.lobby_no)          # client shows "full, could not enter"
            return
        if room is None:
            room = self.tour_rooms[tour_id] = TourRoom(tour_id)
        self.detach(pl)
        for ids in self.reserved.values():
            ids.discard(pl.id)
        pl.s.send(T_SYSTEM, cmd(9, 0, 0, 0, tour_id))
        self.attach(pl, room, room.free_slot())
        self.broadcast_vacancies([tour_id])

    def c_tour_reserve(self, s, p):
        pl = self.me(s)
        tid, cancel = u32(p, 8), u32(p, 0xC) & 1
        if cancel:
            self.reserved.get(tid, set()).discard(pl.id)
            st = 0
        elif time.time() >= tours.start_of(tid) - 30:
            st = 1                                      # already started
        elif any(pl.id in ids for t, ids in self.reserved.items() if t != tid):
            st = 4                                      # reserved another tour already
        else:
            self.reserved.setdefault(tid, set()).add(pl.id)
            st = 0
        s.send(T_SYSTEM, cmd(0x2F5, 0, st, 0, cancel))
        self.broadcast_vacancies([tid])

    def c_tour_data(self, s, p):
        """Watch / results: the tour room's 100 x 0x80 common memory."""
        room_id, tid = u32(p, 8), u32(p, 0xC)
        room = self.tour_rooms.get(room_id or tid)
        mem = bytes(room.mem) if room else bytes(0x3200)
        s.send(T_SYSTEM, cmd(0x2FB, 0, len(mem), room_id, tid, body=mem))

    async def pace_tours(self):
        """Push cmd 0x2f6 {+8 = hole index} to a tour room at each hole's start; without it the
        mode-7 round stops after hole 1."""
        while True:
            now = time.time()
            for tid, room in list(self.tour_rooms.items()):
                starts = tours.hole_starts(tours.start_of(tid))
                while room.holes_sent < len(starts) and now >= starts[room.holes_sent]:
                    for m in room.members():
                        m.s.send(T_SYSTEM, cmd(0x2F6, 0, 0, room.holes_sent))
                        await m.s.writer.drain()
                    room.holes_sent += 1
                if now > tours.end_of(tours.start_of(tid)) + 600 and not room.members():
                    del self.tour_rooms[tid]
                    self.reserved.pop(tid, None)
            await asyncio.sleep(1)

    async def serve(self, host, port):
        srv = await super().serve(host, port)
        self._pacer = asyncio.ensure_future(self.pace_tours())
        return srv
