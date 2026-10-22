"""Two simulated clients: play auth -> lobby -> create/join room -> accept -> round start -> relay.
Follows."""
import asyncio
import os
import struct
import tempfile
import unittest

from mingol import dotcomm
from mingol.accounts import Store
from mingol.play import Play
from tests.test_regi_gate import login_msg


class Client:
    def __init__(self, r, w):
        self.r, self.w, self.buf, self.frames = r, w, bytearray(), []

    def send(self, ftype, payload, secure=False):
        self.w.write(dotcomm.build(ftype, payload, b"\x7f\0\0\1", secure))

    async def until(self, pred, timeout=3):
        async def loop():
            while True:
                for i, f in enumerate(self.frames):
                    if pred(f):
                        del self.frames[:i + 1]
                        return f
                self.buf += await self.r.read(65536)
                while True:
                    f, n = dotcomm.parse(self.buf)
                    if not f:
                        break
                    del self.buf[:n]
                    self.frames.append(f)
        return await asyncio.wait_for(loop(), timeout)


def is_cmd(c):
    return lambda f: f.type == 5 and struct.unpack_from("<H", f.payload, 0)[0] == c


def cmd(c, sub=0, a=0, b=0, cc=0, body=b""):
    return struct.pack("<HHIII", c, sub, a, b, cc) + body


class PlayTest(unittest.TestCase):
    def test_lobby_room_round(self):
        asyncio.run(self.flow())

    async def login(self, port, acct):
        r, w = await asyncio.open_connection("127.0.0.1", port)
        c = Client(r, w)
        c.send(5, login_msg(acct["user_no"], acct["key"], acct["pw"]), secure=True)
        f = await c.until(is_cmd(2))
        self.assertEqual(struct.unpack_from("<i", f.payload, 8)[0], 0)
        f = await c.until(is_cmd(9))
        self.assertEqual(struct.unpack_from("<II", f.payload, 8), (0, 0))   # on the server, no lobby
        await c.until(is_cmd(0x2FC))
        c.send(5, cmd(9, body=b"\0" * 0x64))                                # m_lobsel "server, no lobby"
        f = await c.until(is_cmd(9))
        self.assertEqual(struct.unpack_from("<II", f.payload, 8), (0, 0))
        enter = bytearray(0x74)
        struct.pack_into("<HHII", enter, 0, 9, 0, 0, 1)                     # m_lobby enters lobby 1
        c.send(5, bytes(enter))
        f = await c.until(is_cmd(9))
        self.assertEqual(struct.unpack_from("<II", f.payload, 8), (1, 0))
        f = await c.until(lambda f: is_cmd(0x1B)(f) and struct.unpack_from("<I", f.payload, 0x18)[0] == acct["user_no"])
        return c

    async def flow(self):
        os.environ["MINGOL_LOG_DIR"] = tempfile.mkdtemp()
        store = Store(os.path.join(tempfile.mkdtemp(), "a.json"))
        a, _ = store.create("K1", "alice", "pw")
        b, _ = store.create("K2", "bob", "pw")
        srv = await Play("127.0.0.1", store).serve("127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        ca = await self.login(port, a)
        cb = await self.login(port, b)

        # common memory echo to sender
        ca.send(5, cmd(0x262, 0, 4, 0, body=struct.pack("<I", a["user_no"])))
        await ca.until(is_cmd(0x262))
        await cb.until(is_cmd(0x262))

        # alice creates a 2-player room
        create = bytearray(0x74)
        struct.pack_into("<HHII", create, 0, 9, 1, 0, 0)
        struct.pack_into("<H", create, 0x10, 2)
        struct.pack_into("<H", create, 0x16, 1)
        ca.send(5, bytes(create))
        f = await ca.until(is_cmd(9))
        room = struct.unpack_from("<I", f.payload, 0x0C)[0]
        self.assertGreater(room, 0)
        f = await ca.until(is_cmd(600))
        self.assertEqual(struct.unpack_from("<I", f.payload, 0x18)[0], a["user_no"])

        # bob finds and joins it
        cb.send(5, cmd(0x387, 1))
        f = await cb.until(is_cmd(0x387))
        self.assertEqual(struct.unpack_from("<I", f.payload, 8)[0], 1)
        self.assertEqual(struct.unpack_from("<H", f.payload, 0x10)[0], room)
        join = bytearray(0x74)
        struct.pack_into("<HHII", join, 0, 9, 1, room, 0)
        cb.send(5, bytes(join))
        f = await cb.until(is_cmd(9))
        self.assertEqual(struct.unpack_from("<I", f.payload, 0x0C)[0], room)
        await cb.until(lambda f: is_cmd(0x1B)(f) and struct.unpack_from("<I", f.payload, 0x18)[0] == b["user_no"])

        # both accept -> start info + member states
        ca.send(5, cmd(0x20))
        cb.send(5, cmd(0x20))
        for c, me in ((ca, a), (cb, b)):
            f = await c.until(is_cmd(0x0B))
            self.assertEqual(struct.unpack_from("<H", f.payload, 0x16)[0], 2)
            states = []
            for _ in range(2):
                f = await c.until(is_cmd(0x26))
                states.append(struct.unpack_from("<i", f.payload, 0x0C)[0])
            self.assertEqual(sorted(states), [0, 2])
            await c.until(is_cmd(0x27))

        # in-round relay: type 7 in, type 1 out, other member only
        shot = struct.pack("<HBBI", 8, 3, 0, 42)
        ca.send(7, shot)
        f = await cb.until(lambda f: f.type == 1)
        self.assertEqual(f.payload, shot)
        ca.send(5, cmd(0x34, 0, 77))
        f = await ca.until(is_cmd(0x34))      # next frame for alice is the time reply, not an echo
        self.assertEqual(struct.unpack_from("<I", f.payload, 4)[0], 77)
        self.assertFalse(any(x.type == 1 for x in ca.frames))

        # logout
        cb.send(5, cmd(0x0C))
        await cb.until(is_cmd(0x0C))
        await ca.until(is_cmd(0x1C))
        for c in (ca, cb):
            c.w.close()
        srv.close()


if __name__ == "__main__":
    unittest.main()


class IdlePingTest(unittest.TestCase):
    """The client drops a play socket after 60 s without a frame; its 30 s
    idle ping (empty 0x0a) gets an empty 0x0a back to reset that timer."""

    def test_idle_ping_answered(self):
        asyncio.run(self.flow())

    async def flow(self):
        store = Store(os.path.join(tempfile.mkdtemp(), "a.json"))
        srv = await Play("127.0.0.1", store).serve("127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        r, w = await asyncio.open_connection("127.0.0.1", port)
        c = Client(r, w)
        c.send(dotcomm.T_OPEN, b"")
        f = await c.until(lambda f: f.type == dotcomm.T_OPEN)
        self.assertEqual(f.payload, b"")
        w.close()
        srv.close()
        await srv.wait_closed()
