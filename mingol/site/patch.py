"""patch.mgo.mingol.net: the update check the game makes before it signs in.

The client (IPatcher2) GETs http://patch.mgo.mingol.net/patch/ipatcher.inf on port 80 and goes on
to the register server only when it gets a manifest it accepts. A manifest is "KEY VALUE" text
lines XORed with the repeating 10-byte key "moe5963miz". The parser takes END as the end of the
manifest; a line with one token and no value is an error, so the manifest has no comment line.
No File lines means there is nothing to download. The answer must be status 200 exactly.
"""
from .httpd import Response

KEY = b"moe5963miz"
NO_UPDATE = b"END\r\n"


def xor(data):
    """Encode or decode an IPatcher2 manifest (XOR is its own inverse)."""
    return bytes(b ^ KEY[i % len(KEY)] for i, b in enumerate(data))


class Patch:
    def handle(self, req):
        if req.path.lower().endswith("/ipatcher.inf"):
            return Response(xor(NO_UPDATE), ctype="application/octet-stream")
        return Response(b"", 404, ctype="text/plain")
