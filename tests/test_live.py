"""The boards' snapshot (mingol/live.py): python -m unittest tests.test_live"""
import json
import os
import tempfile
import unittest

from mingol import live, tours
from mingol.accounts import Store
from mingol.play import Lobby, Play, Player, Room, TourRoom
from mingol.site.common import Sessions
from mingol.site.www import WWW


class FakeSession:
    def send(self, *a):
        pass


NOW = 1_800_000_100            # 100 s into a 900 s slot


class LiveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "accounts.json"))
        self.play = Play("127.0.0.1", self.store)

    def tearDown(self):
        self.tmp.cleanup()

    def player(self, no, name, where=None, slot=0):
        acct, _ = self.store.create("k%d" % no, name, "secret%d" % no)
        pl = Player(FakeSession(), acct)
        self.play.players[pl.id] = pl
        if where is not None:
            pl.where, pl.slot = where, slot
            where.slots[slot] = pl
        return pl

    def test_empty(self):
        snap = live.snapshot(self.play, now=NOW)
        self.assertEqual((snap["online"], snap["players"], snap["rooms"], snap["lobbies"]), (0, [], [], []))
        t = snap["tours"]
        self.assertEqual(len(t["open"]), 1)
        self.assertEqual(t["open"][0]["start"], (NOW // tours.SPACING + 1) * tours.SPACING)
        self.assertEqual(t["next"]["start"], t["open"][0]["start"] + tours.SPACING)
        self.assertEqual(t["playing"], [])
        json.dumps(snap)

    def test_entry_closes_30s_before_the_start(self):
        late = (NOW // tours.SPACING + 1) * tours.SPACING - 10
        self.assertEqual(live.snapshot(self.play, now=late)["tours"]["open"], [])

    def test_players_rooms_and_a_playing_tour(self):
        lobby = self.play.lobbies[1] = Lobby(1)
        room = self.play.rooms[1] = Room(1, 1, 4)
        room.caption, room.started = "Sunday round", True
        start = (NOW // tours.SPACING) * tours.SPACING          # started 100 s ago
        tr = self.play.tour_rooms[start // tours.SPACING] = TourRoom(start // tours.SPACING)
        tr.holes_sent = 1
        self.player(1, "alice", lobby)
        self.player(2, "bob", room, 0)
        self.player(3, "cara", room, 1)
        self.player(4, "dave", tr, 0)
        self.player(5, "erin")
        snap = live.snapshot(self.play, now=NOW)
        self.assertEqual(snap["online"], 5)
        self.assertEqual({p["name"]: p["where"] for p in snap["players"]},
                         {"alice": "lobby", "bob": "room", "cara": "room", "dave": "tour", "erin": "server"})
        self.assertEqual(snap["lobbies"], [{"no": 1, "players": 1}])
        self.assertEqual(snap["rooms"], [{"id": 1, "lobby": 1, "caption": "Sunday round",
                                          "players": ["bob", "cara"], "max": 4, "playing": True, "open": True}])
        playing = snap["tours"]["playing"]
        self.assertEqual([(t["players"], t["hole"]) for t in playing], [(1, 1)])
        text = json.dumps(snap)
        self.assertNotIn("secret", text)                        # no passwords
        self.assertNotIn('"key"', text)

    def test_japan_tour_ranking(self):
        www = WWW(self.store, Sessions(), self.tmp.name)
        www.state.data["records"] = [
            {"handle": "alice", "total": 40, "tour": "Nationals", "t": "2025/01/01 10:00", "raw": "ff", "attest": "x"},
            {"handle": "alice", "total": 36, "tour": "Nationals", "t": "2025/01/01 11:00", "raw": "ff", "attest": "x"},
            {"handle": "bob", "total": 36, "tour": "Nationals", "t": "2025/01/01 09:00", "raw": "ff", "attest": "x"},
            {"handle": "cara", "total": 45, "tour": "Nationals", "t": "2025/01/01 12:00", "raw": "ff", "attest": "x"}]
        snap = live.snapshot(self.play, www, now=NOW)
        self.assertEqual([(r["place"], r["name"], r["total"]) for r in snap["japan_tour"]],
                         [(1, "bob", 36), (1, "alice", 36), (3, "cara", 45)])
        self.assertEqual(snap["totals"]["japan_tour_rounds"], 4)
        self.assertNotIn("attest", json.dumps(snap))

    def test_write(self):
        dest = live.path(self.tmp.name)
        self.assertEqual(dest, os.path.join(self.tmp.name, "board", "live.json"))
        live.write(dest, live.snapshot(self.play))
        with open(dest, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["game"], "mingol")


if __name__ == "__main__":
    unittest.main()
