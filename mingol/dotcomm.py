"""dotcomm: the Dwango transport every Everybody's Golf Online server connection uses.

Static RE of SYSTEM.BIN (VA = file offset + 0x162c80):

  frame = header(12) + payload
    +0  u8   header length, always 0x0c                 (0x18fa0c)
    +1  u8   type (low 5 bits) | 0x20 = payload is Blowfish-encrypted
    +2  u16  payload length on the wire (LE)              (0x18fa10)
    +4  u32  sender's own IPv4 as stored by the client (c0 00 02 0a = 192.0.2.10)
    +8  u16  CRC-16/MODBUS of the wire payload, 0 when the payload is empty (0x18fa30)
    +10 u16  0
  Receive (0x18fc70): reads the 12-byte header, then hdr[0]-12+len more bytes; checks the
  CRC only when len != 0 ("CRC check Error" -> frame dropped); if type & 0x20 the payload is
  deciphered and the length rewritten, then the flag is cleared.

  encrypted payload (0x18bfb0 / 0x18c250) = u32le plain_len + Blowfish-ECB blocks over the
  plaintext zero-padded to 8. Wire length = 4 + roundup8(plain_len) (0x18bf60). Blowfish is
  standard (P/S init at 0x291e00/0x291e50, key schedule 0x18bc80) but each 8-byte block is
  two NATIVE little-endian u32 halves, so bytes are swapped per 4 relative to the textbook
  big-endian Blowfish. Key = 16 bytes at 0x292e60.

  Transport types the receive switch knows (log strings at 0x2c0d10..0x2c0dd0):
    1 DATA, 2 CHAT (<200 B printed as text), 3 ACK, 5 SYSTEM, 7 DATA_NOLOG,
    0x0a sent with no payload by the open path (0x1906d0),
    0x0c keepalive: on receipt the client answers with an empty 0x0c (0x18ff58),
    0x0d LOST, 0x0e NEWS.
  Any other type is handed up to the app layer unchanged. The app layer sends through
  DocSend (0x190740) with its own type numbers; the first regi frame is type 0x1d, empty.
"""
import struct

from .blowfish import Blowfish

HDR = 12
F_SECURE = 0x20
KEY = bytes.fromhex("6d3921fe90175a8c13cb73036730db42")

T_DATA, T_CHAT, T_ACK, T_SYSTEM, T_DATA_NOLOG = 1, 2, 3, 5, 7
T_OPEN, T_KEEPALIVE, T_LOST, T_NEWS = 0x0A, 0x0C, 0x0D, 0x0E


def _crc_table():
    t = []
    for i in range(256):
        c = i
        for _ in range(8):
            c = (c >> 1) ^ 0xA001 if c & 1 else c >> 1
        t.append(c)
    return t


_CRC = _crc_table()


def crc16(data, crc=0xFFFF):
    for b in data:
        crc = (crc >> 8) ^ _CRC[(crc ^ b) & 0xFF]
    return crc


_BF = Blowfish(KEY)


def encrypt(plain):
    n = len(plain)
    padded = plain + b"\0" * (-n % 8)
    return struct.pack("<I", n) + _BF.encrypt(padded)


def decrypt(wire):
    n = struct.unpack_from("<I", wire, 0)[0]
    body = wire[4:]
    if len(body) % 8 or not (4 <= len(wire) - n <= 12):
        raise ValueError("bad secure payload length %d for plain %d" % (len(wire), n))
    return _BF.decrypt(body)[:n]


def build(ftype, payload=b"", ip=b"\0\0\0\0", secure=False):
    wire = encrypt(payload) if secure else payload
    t = (ftype & 0x1F) | (F_SECURE if secure else 0)
    crc = crc16(wire) if wire else 0
    return struct.pack("<BBH4sHH", HDR, t, len(wire), ip, crc, 0) + wire


class Frame:
    __slots__ = ("type", "secure", "ip", "payload", "raw")

    def __repr__(self):
        return "Frame(type=0x%02x%s len=%d %s)" % (
            self.type, " secure" if self.secure else "", len(self.payload), self.payload[:32].hex())


def parse(buf):
    """Return (frame or None, bytes consumed). Raises ValueError on a bad frame."""
    if len(buf) < HDR:
        return None, 0
    hlen, t, n, ip, crc, _ = struct.unpack_from("<BBH4sHH", buf, 0)
    if hlen < HDR:
        raise ValueError("header length %d" % hlen)
    total = hlen + n
    if len(buf) < total:
        return None, 0
    wire = bytes(buf[hlen:total])
    if n and crc16(wire) != crc:
        raise ValueError("CRC mismatch")
    f = Frame()
    f.type = t & 0x1F
    f.secure = bool(t & F_SECURE)
    f.ip = ip
    f.payload = decrypt(wire) if f.secure else wire
    f.raw = bytes(buf[:total])
    return f, total
