import unittest

from mingol import dotcomm


class DotcommTest(unittest.TestCase):
    def test_crc_modbus_check_value(self):
        self.assertEqual(dotcomm.crc16(b"123456789"), 0x4B37)

    def test_parse_regi_hello(self):
        # a regi hello frame
        f, n = dotcomm.parse(bytes.fromhex("0c1d0000c000020a00000000"))
        self.assertEqual((n, f.type, f.secure, f.payload, f.ip), (12, 0x1D, False, b"", bytes([192, 0, 2, 10])))

    def test_partial_frame(self):
        raw = dotcomm.build(1, b"abc")
        self.assertEqual(dotcomm.parse(raw[:-1]), (None, 0))

    def test_secure_round_trip_and_lengths(self):
        for n in (0, 1, 7, 8, 9, 100):
            p = bytes(range(n))
            raw = dotcomm.build(1, p, secure=True)
            self.assertEqual(raw[2] | raw[3] << 8, 4 + ((n + 7) // 8) * 8)
            f, used = dotcomm.parse(raw)
            self.assertEqual((f.payload, f.secure, used), (p, True, len(raw)))

    def test_blowfish_known_answer(self):
        # byte-swapped big-endian Blowfish
        self.assertEqual(dotcomm.encrypt(bytes(range(20))).hex(),
                         "140000007c9fda9fd3fe2592b1dffc96ac751b8d4495f8ebf2e26bef")

    def test_crc_rejects_corruption(self):
        raw = bytearray(dotcomm.build(1, b"hello"))
        raw[-1] ^= 1
        with self.assertRaises(ValueError):
            dotcomm.parse(raw)


if __name__ == "__main__":
    unittest.main()
