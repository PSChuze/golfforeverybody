"""Real tournaments and the Japan Tour record.

All tour times on the wire are real UTC seconds; the client compares them against its own
clock, which runs on JST (UTC + 0x7e90).

Schedule: an "always open" rolling day. Every 15 minutes of the current JST business day there
is a 9-hole tour (course 8). Entry opens for everyone 15 minutes before the start (A = B =
S - 900), so a tour can always be joined within 15 minutes. Each tour has its own 0x2f4 map
entry (map key = index in the day) whose roomId is the tourId, so overlapping rounds never
share a room. tourId = S // 900, so the start time is recoverable from the id alone.
"""
import struct
import time

JST = 0x7E90
SPACING = 900
HOLES = 9
HOLE_SECONDS = 240
GAP = 30
TAIL = 60
CAPACITY = 100
COURSE_9_HOLES = 8
TOUR_LOBBY = 5              # lobbyNo of the real-tournament lobby (gate msg 3, category 1)


def day_start(now=None):
    """UTC seconds of the current JST midnight (business day boundary 00:00, cmd 0x2fd not sent)."""
    now = time.time() if now is None else now
    return int((now + JST) // 86400 * 86400 - JST)


def schedule(now=None):
    """[(tourId, start)] for every tour of the current JST day."""
    d = day_start(now)
    return [((d + i * SPACING) // SPACING, d + i * SPACING) for i in range(86400 // SPACING)]


def start_of(tour_id):
    return tour_id * SPACING


def hole_starts(start):
    """UTC seconds at which hole k (1-based) begins: S, then + hole time + gap each hole."""
    out, t = [], start
    for _ in range(HOLES):
        out.append(t)
        t += HOLE_SECONDS + GAP
    return out


def end_of(start):
    return start + HOLES * HOLE_SECONDS + (HOLES - 1) * GAP + TAIL


def _name(s, n):
    b = s.encode("cp932", "replace")[:n - 1]
    return b.ljust(n, b"\0")


def tour_record(tour_id, map_key, start):
    """0x80-byte cmd 0x2f3 record, all restrictions neutral."""
    r = bytearray(0x80)
    hh, mm = time.gmtime(start + JST)[3:5]
    struct.pack_into("<IHBB", r, 0, tour_id, map_key, 0, COURSE_9_HOLES)
    r[0x08:0x28] = _name("Open Tour %02d:%02d" % (hh, mm), 0x20)
    struct.pack_into("<III", r, 0x28, start - SPACING, start - SPACING, start)
    struct.pack_into("<HHH", r, 0x34, TAIL, GAP, CAPACITY)
    struct.pack_into("<9H", r, 0x3A, *([HOLE_SECONDS] * HOLES))
    r[0x60] = 1                     # official
    r[0x64] = 0x13                  # class max 0x13 + min 0 = no class limit
    r[0x66] = 0x80                  # rating max/min -128 = none
    r[0x67] = 0x80
    r[0x68] = 0xFF                  # age max none
    r[0x6C] = 1
    r[0x6D] = 1
    r[0x73] = 1
    return bytes(r)


def map_records(sched):
    """cmd 0x2f4 map: key = index (m_realtour indexes map[mapKey] directly), roomId = tourId."""
    return b"".join(struct.pack("<HHHHI", i, 0, TOUR_LOBBY, TOUR_LOBBY, tid)
                    for i, (tid, _s) in enumerate(sched))


def tour_list(sched):
    return b"".join(tour_record(tid, i, s) for i, (tid, s) in enumerate(sched))


# ---------------------------------------------------------------- Japan Tour (HTTP)

JAPAN_TOUR_CODE = 1


def japan_tour_record(now=None):
    """0x50c-byte TourDB record, open from yesterday to +1 year."""
    now = int(time.time() if now is None else now)
    r = bytearray(0x50C)
    struct.pack_into("<I", r, 0x004, JAPAN_TOUR_CODE)
    r[0x090:0x0B8] = _name("Everybody's Golf Nationals", 0x28)
    r[0x0B8:0x0B8 + 0x408] = _name("An always-open solo tournament on the revival server. "
                                   "Play a 9-hole round; your score is recorded.", 0x408)
    struct.pack_into("<II", r, 0x4C0, now - 86400, now + 365 * 86400)
    struct.pack_into("<I", r, 0x4E4, 99)
    r[0x4ED] = COURSE_9_HOLES
    return bytes(r)
