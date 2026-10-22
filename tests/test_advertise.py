import unittest

from mingol.net import Service

MAP = "192.168.0.0/16=192.168.0.10,100.64.0.0/10=100.64.0.20,0.0.0.0/0=203.0.113.7"


class AdvertiseTest(unittest.TestCase):
    def test_first_matching_cidr_wins(self):
        s = Service("10.0.0.1", MAP)
        self.assertEqual(s.advertise("192.168.0.117"), "192.168.0.10")
        self.assertEqual(s.advertise("100.70.1.2"), "100.64.0.20")
        self.assertEqual(s.advertise("203.0.113.5"), "203.0.113.7")

    def test_no_map_or_bad_peer_uses_public_ip(self):
        self.assertEqual(Service("10.0.0.1").advertise("203.0.113.5"), "10.0.0.1")
        self.assertEqual(Service("10.0.0.1", MAP).advertise("?"), "10.0.0.1")


if __name__ == "__main__":
    unittest.main()
