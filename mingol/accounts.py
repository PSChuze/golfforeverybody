"""Account store (JSON file). An account = the 0xe0 char-DB record the client sees:
user_no, the opaque per-console account key (0x30, from EDAUTH), login id, password.
The client allows at most 5 accounts per key (error (0x1c,0) beyond that)."""
import json
import os
import threading

MAX_PER_KEY = 5
GAMEDB_LEN = 0xA0


def default_gamedb(user_no):
    """0xa0 GAMEDB record a new player needs: +0 u32 user_no (0 = "no player data"
    -> silent bounce to mode select), +8 u32 year<<16|month<<8|day (0 -> sent to
    registration), +0x12 u8 1, rest 0."""
    import struct
    rec = bytearray(GAMEDB_LEN)
    struct.pack_into("<II", rec, 0, user_no, 0)
    struct.pack_into("<I", rec, 8, 0x07C60101)
    rec[0x12] = 1
    return bytes(rec)


class Store:
    def __init__(self, path, check_passwords=True):
        self.path = path
        self.check_passwords = check_passwords
        self.lock = threading.Lock()
        self.data = {"next_user_no": 1, "accounts": []}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                self.data = json.load(f)

    def _save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=1)
        os.replace(tmp, self.path)

    def gamedb(self, acct):
        hexrec = acct.get("gamedb")
        return bytes.fromhex(hexrec) if hexrec else default_gamedb(acct["user_no"])

    def set_gamedb(self, user_no, rec):
        with self.lock:
            acct = self.get(user_no)
            if acct:
                acct["gamedb"] = rec[:GAMEDB_LEN].ljust(GAMEDB_LEN, b"\0").hex()
                self._save()

    def by_key(self, key):
        return [a for a in self.data["accounts"] if a["key"] == key]

    def get(self, user_no):
        return next((a for a in self.data["accounts"] if a["user_no"] == user_no), None)

    def create(self, key, uid, pw, profile_hex=""):
        """Returns (account, result). Result codes are the client's add-user codes:
        0 ok; 1..6 errors (exact meanings unknown)."""
        with self.lock:
            for a in self.by_key(key):
                if a["id"] == uid:
                    return a, 0
            if not uid or len(self.by_key(key)) >= MAX_PER_KEY:
                return None, 1
            if any(a["id"] == uid for a in self.data["accounts"]):
                return None, 2
            a = {"user_no": self.data["next_user_no"], "key": key, "id": uid, "pw": pw,
                 "profile": profile_hex}
            self.data["next_user_no"] += 1
            self.data["accounts"].append(a)
            self._save()
            return a, 0
