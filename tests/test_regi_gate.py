"""Plays the client's side of regi -> gate over real sockets."""
import asyncio
import os
import socket
import struct
import tempfile
import unittest

from mingol import dotcomm
from mingol.accounts import Store
from mingol.regi_gate import Gate, Regi


async def rpc(reader, writer, ftype, payload=b"", secure=False, want=1):
    writer.write(dotcomm.build(ftype, payload, b"\x7f\0\0\1", secure))
    await writer.drain()
    out, buf = [], bytearray()
    while len(out) < want:
        buf += await asyncio.wait_for(reader.read(65536), 5)
        while True:
            f, n = dotcomm.parse(buf)
            if not f:
                break
            del buf[:n]
            out.append(f)
    return out


def login_msg(user_no, key, pw, version=1):
    p = bytearray(0x6C)
    struct.pack_into("<HHIiII", p, 0, 1, 0, 0x55, -10, 0, user_no)
    p[0x2C:0x2C + len(key)] = key.encode()
    p[0x5C:0x5C + len(pw)] = pw.encode()
    struct.pack_into("<H", p, 0x68, version)
    return bytes(p)


class RegiGateTest(unittest.TestCase):
    def test_full_login(self):
        asyncio.run(self.flow())

    async def flow(self):
        os.environ["MINGOL_LOG_DIR"] = tempfile.mkdtemp()
        store = Store(os.path.join(tempfile.mkdtemp(), "accounts.json"))
        regi = await Regi("127.0.0.1", 0).serve("127.0.0.1", 0)
        gate = Gate("127.0.0.1", 12104, store)
        gsrv = await gate.serve("127.0.0.1", 0)
        rport = regi.sockets[0].getsockname()[1]
        gport = gsrv.sockets[0].getsockname()[1]

        # regi: 0x1d empty -> world info
        r, w = await asyncio.open_connection("127.0.0.1", rport)
        (f,) = await rpc(r, w, 0x1D)
        self.assertEqual(f.type, 0x1D)
        date, count = f.payload[:8], struct.unpack_from("<I", f.payload, 8)[0]
        self.assertEqual(count, 1)
        self.assertEqual(socket.inet_ntoa(f.payload[12:16]), "127.0.0.1")
        w.close()

        # gate
        r, w = await asyncio.open_connection("127.0.0.1", gport)
        (f,) = await rpc(r, w, 5, struct.pack("<H", 0x2C) + b"\0" * 14)
        self.assertEqual(f.payload[2:10], date)
        req = bytearray(0x129)
        struct.pack_into("<H", req, 0, 500)
        req[0x10:0x1B] = b"CONSOLEKEY1"
        (f,) = await rpc(r, w, 5, bytes(req), secure=True)
        self.assertEqual(struct.unpack_from("<HxxxxxxI", f.payload, 0), (500, 0))

        add = bytearray(0x139)
        struct.pack_into("<H", add, 0, 0x1F5)
        add[0x10:0x1B] = b"CONSOLEKEY1"
        add[0x40:0x45] = b"alice"
        add[0x50:0x54] = b"pass"
        (f,) = await rpc(r, w, 5, bytes(add), secure=True)
        self.assertEqual(struct.unpack_from("<HH", f.payload, 0), (0x1F5, 0))
        user_no = struct.unpack_from("<I", f.payload, 0x10)[0]
        self.assertEqual(f.payload[0x10 + 0x34:0x10 + 0x39], b"alice")

        frames = await rpc(r, w, 5, login_msg(user_no, "CONSOLEKEY1", "pass"), secure=True, want=4)
        msgs = [struct.unpack_from("<H", x.payload, 0)[0] for x in frames]
        self.assertEqual(msgs, [2, 0x1F6, 0x211, 3])
        self.assertEqual(struct.unpack_from("<i", frames[0].payload, 8)[0], 0)
        self.assertEqual(len(frames[3].payload), 0xAEC)
        self.assertEqual(frames[3].payload[0x64:0x66], struct.pack(">H", 12104))
        self.assertEqual(struct.unpack_from("<I", frames[3].payload, 0x68)[0], 5)   # lobby table
        self.assertEqual(frames[3].payload[0x6C], 1)
        tour = 0x6C + 4 * 0x28                                                   # 5th lobby
        self.assertEqual((frames[3].payload[tour], frames[3].payload[tour + 1]), (5, 1))
        self.assertEqual(struct.unpack_from("<H", frames[3].payload, 0xA6C + 8)[0], 2)

        (f,) = await rpc(r, w, 5, login_msg(user_no, "CONSOLEKEY1", "wrong"), secure=True)
        self.assertEqual(struct.unpack_from("<i", f.payload, 8)[0], -1)
        w.close()
        regi.close()
        gsrv.close()


if __name__ == "__main__":
    unittest.main()
