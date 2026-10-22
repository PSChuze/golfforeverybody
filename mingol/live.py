"""The game servers' public snapshot, for a status board or a website.

A board can run in its own container with ONLY <data dir>/board/ mounted read-only: it never
connects to the servers (a board bug can never touch a round) and never sees accounts.json or
www.json beside it. So everything the board draws is published here, and nothing else: the handles
the game itself shows other players, where they are, the real tournaments and the Japan Tour
(New Nationals) ranking.

Shape (version 1), written atomically to MINGOL_LIVE_FILE (default <data dir>/board/live.json)
every MINGOL_LIVE_EVERY seconds (10; 0 = off):
  {"v": 1, "game": "mingol", "updated": unix, "online": N,
   "players": [{"name", "where": "server" | "lobby" | "room" | "tour"}],
   "lobbies": [{"no", "players"}],                                  # lobbies with anyone in them
   "rooms": [{"id", "lobby", "caption", "players": [names], "max", "playing", "open"}],
   "tours": {"open": [TOUR], "playing": [TOUR], "next": TOUR | null},
   "japan_tour": [{"place", "name", "total", "tour", "at"}],          # best round per handle
   "totals": {"accounts", "japan_tour_rounds"}}
  TOUR = {"id", "name", "start": unix, "end": unix, "holes", "hole", "players"}: "open" takes
  entries now (from 15 min before the start), "playing" is between its start and its end; hole is
  the 1-based hole being played (0 before the start). Japan Tour totals are strokes (lower is
  better); at is the JST time the record was posted.
"""
import asyncio
import json
import os
import sys
import time

from . import play as playmod
from . import tours

VERSION = 1
JAPAN_TOUR_ROWS = 25


def path(data_dir):
    return os.environ.get("MINGOL_LIVE_FILE") or os.path.join(data_dir, "board", "live.json")


def _where(pl):
    w = pl.where
    if w is None:
        return "server"
    if isinstance(w, playmod.TourRoom):
        return "tour"
    if isinstance(w, playmod.Room):
        return "room"
    return "lobby"


def _tour(tid, room, reserved, now):
    start = tours.start_of(tid)
    hh, mm = time.gmtime(start + tours.JST)[3:5]
    n = len(room.members()) if room else 0
    return {"id": tid, "name": "Open Tour %02d:%02d" % (hh, mm), "start": start,
            "end": tours.end_of(start), "holes": tours.HOLES,
            "hole": room.holes_sent if room else (0 if now < start else None),
            "players": max(n, len(reserved.get(tid, ())))}


def tour_view(play, now=None):
    """Tours run every SPACING s and tourId = start // SPACING, so the open one and the next
    follow from the clock; the playing ones are the tour rooms with someone still in them."""
    now = time.time() if now is None else now
    open_id = int(now // tours.SPACING) + 1             # starts within SPACING, entry is open
    open_ = [] if tours.start_of(open_id) - 30 <= now else [open_id]
    playing = sorted(tid for tid, room in play.tour_rooms.items()
                     if room.members() and tours.start_of(tid) <= now < tours.end_of(tours.start_of(tid)))
    view = lambda tid: _tour(tid, play.tour_rooms.get(tid), play.reserved, now)
    return {"open": [view(t) for t in open_], "playing": [view(t) for t in playing],
            "next": view(open_id + 1)}


def japan_tour(www):
    """The website's live New Nationals ranking (best total per handle), public fields only."""
    if www is None:
        return [], 0
    with www.state.lock:
        rows = www.live_ranking()[:JAPAN_TOUR_ROWS]
        n = len(www.state.data.get("records", []))
    out, place, prev = [], 0, None
    for i, r in enumerate(rows, 1):
        if r["total"] != prev:
            place, prev = i, r["total"]
        out.append({"place": place, "name": r["handle"], "total": r["total"], "tour": r.get("tour", ""),
                    "at": r.get("t", "")})
    return out, n


def snapshot(play, www=None, now=None):
    now = time.time() if now is None else now
    pls = list(play.players.values())
    players = sorted(({"name": p.name, "where": _where(p)} for p in pls), key=lambda p: p["name"])
    lobbies = [{"no": no, "players": len(lb.members())} for no, lb in sorted(play.lobbies.items())
               if lb.members()]
    rooms = [{"id": r.id, "lobby": r.lobby_no, "caption": r.caption,
              "players": [m.name for m in r.members()], "max": r.max, "playing": bool(r.started),
              "open": bool(r.open)} for r in sorted(play.rooms.values(), key=lambda r: r.id) if r.members()]
    jt, jt_n = japan_tour(www)
    return {"v": VERSION, "game": "mingol", "updated": int(now), "online": len(players),
            "players": players, "lobbies": lobbies, "rooms": rooms, "tours": tour_view(play, now),
            "japan_tour": jt,
            "totals": {"accounts": len(play.store.data.get("accounts", [])), "japan_tour_rounds": jt_n}}


def write(dest, snap):
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    tmp = dest + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, dest)


async def run(play, www, data_dir):
    """The writer task (run.py starts it). A failed write is logged and retried."""
    every = float(os.environ.get("MINGOL_LIVE_EVERY", "10"))
    if every <= 0:
        return
    dest = path(data_dir)
    print(f"live: snapshot -> {dest} every {every:g}s", file=sys.stderr, flush=True)
    while True:
        try:
            write(dest, snapshot(play, www))
        except Exception as e:                      # noqa: BLE001 never take the servers down
            print(f"live: cannot write {dest}: {e!r}", file=sys.stderr, flush=True)
        await asyncio.sleep(every)
