"""Standard Blowfish, ECB, stdlib only.

The P-array and S-boxes are the hex digits of pi (SYSTEM.BIN's tables at 0x291e00/0x291e50
are the standard ones), so they are computed rather than shipped. The game's dotcomm feeds each
8-byte block to the cipher as two NATIVE little-endian u32 halves (0x18b2d0), so block I/O
here is little-endian, unlike the textbook big-endian Blowfish.
"""
import struct

_N_WORDS = 18 + 4 * 256
_M32 = 0xFFFFFFFF


def _pi_words(n):
    """The first n 32-bit words of the fractional hex digits of pi (Machin's formula)."""
    bits = n * 32
    guard = 64
    one = 1 << (bits + guard)

    def arctan_inv(x):
        x2 = x * x
        term = one // x
        total, k, sign = term, 1, -1
        while term:
            term //= x2
            k += 2
            total += sign * (term // k)
            sign = -sign
        return total

    pi = 4 * (4 * arctan_inv(5) - arctan_inv(239))
    frac = (pi - 3 * one) >> guard
    return [(frac >> (bits - 32 * (i + 1))) & _M32 for i in range(n)]


class Blowfish:
    def __init__(self, key):
        words = _pi_words(_N_WORDS)
        self.P = words[:18]
        self.S = [words[18 + 256 * i:18 + 256 * (i + 1)] for i in range(4)]
        k = len(key)
        j = 0
        for i in range(18):
            d = 0
            for _ in range(4):
                d = (d << 8) | key[j]
                j = (j + 1) % k
            self.P[i] ^= d
        left = right = 0
        for i in range(0, 18, 2):
            left, right = self._enc(left, right)
            self.P[i], self.P[i + 1] = left, right
        for box in self.S:
            for i in range(0, 256, 2):
                left, right = self._enc(left, right)
                box[i], box[i + 1] = left, right

    def _f(self, x):
        s = self.S
        h = (s[0][x >> 24] + s[1][(x >> 16) & 0xFF]) & _M32
        return ((h ^ s[2][(x >> 8) & 0xFF]) + s[3][x & 0xFF]) & _M32

    def _enc(self, left, right):
        p = self.P
        for i in range(16):
            left ^= p[i]
            right ^= self._f(left)
            left, right = right, left
        left, right = right, left
        return left ^ p[17], right ^ p[16]

    def _dec(self, left, right):
        p = self.P
        for i in range(17, 1, -1):
            left ^= p[i]
            right ^= self._f(left)
            left, right = right, left
        left, right = right, left
        return left ^ p[0], right ^ p[1]

    def _run(self, data, fn):
        if len(data) % 8:
            raise ValueError("Blowfish-ECB needs a multiple of 8 bytes, got %d" % len(data))
        out = bytearray()
        for i in range(0, len(data), 8):
            left, right = struct.unpack_from("<II", data, i)
            out += struct.pack("<II", *fn(left, right))
        return bytes(out)

    def encrypt(self, data):
        return self._run(data, self._enc)

    def decrypt(self, data):
        return self._run(data, self._dec)
