import asyncio
import os
import struct
import tempfile
import time
import unittest

from mingol import tours
from mingol.accounts import Store
from mingol.play import Play
from mingol.web import JapanTour
from tests.test_play import Client, cmd, is_cmd
from tests.test_regi_gate import login_msg


class ScheduleTest(unittest.TestCase):
    def test_day_of_rolling_tours(self):
        now = 1_800_000_000
        sched = tours.schedule(now)
        self.assertEqual(len(sched), 96)
        d = tours.day_start(now)
        self.assertEqual((d + tours.JST) % 86400, 0)            # JST midnight
        self.assertTrue(d <= now < d + 86400)
        for tid, s in sched:
            self.assertEqual(tours.start_of(tid), s)

    def test_record_layout(self):
        tid, s = tours.schedule(1_800_000_000)[5]
        r = tours.tour_record(tid, 5, s)
        self.assertEqual(len(r), 0x80)
        self.assertEqual(struct.unpack_from("<IH", r, 0), (tid, 5))
        self.assertEqual(r[7], 8)                                # 9-hole course
        self.assertEqual(struct.unpack_from("<III", r, 0x28), (s - 900, s - 900, s))
        self.assertEqual((r[0x64], r[0x66], r[0x67], r[0x68]), (0x13, 0x80, 0x80, 0xFF))
        m = tours.map_records(tours.schedule(1_800_000_000))
        self.assertEqual(struct.unpack_from("<HHHHI", m, 5 * 12), (5, 0, 5, 5, tid))

    def test_japan_tour_answers(self):
        jt = JapanTour()
        self.assertEqual(jt.answer(b"P_Max"), b"1")
        line = jt.answer(b"P_Regu&tour_code=1,")
        self.assertEqual(len(line), 0x50C * 2 + 1)
        rec = bytes.fromhex(line.decode().strip())
        start, end = struct.unpack_from("<II", rec, 0x4C0)
        self.assertTrue(start <= time.time() < end)
        self.assertEqual(jt.answer(b"P_Regu&tour_code=2,"), b"")
        self.assertEqual(jt.answer(b"P_Record&Record=00&Attest=00"), b"0\n")


class TourRoomTest(unittest.TestCase):
    def test_enter_tour_room(self):
        asyncio.run(self.flow())

    async def flow(self):
        os.environ["MINGOL_LOG_DIR"] = tempfile.mkdtemp()
        now = time.time()
        open_tours = [t for t, s in tours.schedule(now) + tours.schedule(now + 900)
                      if s - 900 <= now < s - 30]
        if not open_tours:
            self.skipTest("inside the 30 s entry gap")
        tid = open_tours[0]
        store = Store(os.path.join(tempfile.mkdtemp(), "a.json"))
        a, _ = store.create("K1", "alice", "pw")
        srv = await Play("127.0.0.1", store).serve("127.0.0.1", 0)
        r, w = await asyncio.open_connection("127.0.0.1", srv.sockets[0].getsockname()[1])
        c = Client(r, w)
        c.send(5, login_msg(a["user_no"], a["key"], a["pw"]), secure=True)
        f = await c.until(is_cmd(0x2F3))
        self.assertEqual(struct.unpack_from("<I", f.payload, 8)[0], 96)
        enter = bytearray(0x74)
        struct.pack_into("<HHII", enter, 0, 9, 1, tid, 0)
        struct.pack_into("<H", enter, 0x1A, 1)
        c.send(5, bytes(enter))
        f = await c.until(is_cmd(9))
        self.assertEqual(struct.unpack_from("<I", f.payload, 0x0C)[0], tid)
        f = await c.until(lambda f: is_cmd(0x1B)(f) and struct.unpack_from("<I", f.payload, 0x18)[0] == a["user_no"])
        self.assertEqual(struct.unpack_from("<I", f.payload, 4)[0], 1)   # room member kind
        c.send(5, cmd(0x2FB, 0, 0, tid))
        f = await c.until(is_cmd(0x2FB))
        self.assertEqual((struct.unpack_from("<I", f.payload, 4)[0], len(f.payload)), (0x3200, 0x10 + 0x3200))
        w.close()
        srv.close()


if __name__ == "__main__":
    unittest.main()
