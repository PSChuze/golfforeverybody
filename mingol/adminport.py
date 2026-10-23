"""This server in an OpenLobby admin panel (the panel's admin protocol, mingol/olport.py).

Off unless MINGOL_ADMIN_PORT is set. The panel is then given this address and the token:

  MINGOL_ADMIN_PORT     the HTTP port, e.g. 12190
  MINGOL_ADMIN_BIND     default 127.0.0.1; in Docker 0.0.0.0, reached only over the `openlobby`
                        network (the port is never published), with a token
  MINGOL_ADMIN_TOKEN    the token the panel sends (POL_ADMIN_GAME_MINGOL_TOKEN on the panel's side)
  MINGOL_ADMIN_CONTAINERS  the containers its server check covers (comma list), default this
                        compose file's (project "mingol")

What the panel gets: who is online and where, the lobbies, rooms and real tournaments, and the next
tour, all from the boards' snapshot (live.snapshot, the same document as board/live.json); the
real tournaments on its Calendar, one entry per Japan day with each tour as a session of it
(tour_calendar, from tours.schedule; 96 entries a day would bury a month view). Two actions, both
things the server already does:

  kick   Disconnect a player: closes their play socket. The server then takes them out of their
         lobby or room exactly as when the client hangs up (Play.on_close: cmd 0x1c to the others,
         a new owner for a room). The panel's account Kick sends {"polid", "handles"}; the handles
         are matched against the in-game IDs.
  news   Login news: the text the gate answers the client's NEWS request with (type 0x0e, the
         MINGOL_NEWS line). Players see it at their next sign-in, not while online. It is kept in
         <data dir>/login-news.json (MINGOL_NEWS_FILE) and run.py loads it at start, so it outlasts
         a restart; while that file exists it wins over MINGOL_NEWS (an empty text there means no
         news). The panel user who set it is logged.

One tool page, without script (olpage.py, OpenLobby's look): Japan Tour, the New Nationals rounds
the website keeps (WWW.japan_tour, <data dir>/www.json): the ranking as the website shows it (best
total per player) and the latest rounds posted.

The player lookup (@port.player, the panel's account window): the panel sends the Feega logins
the account's members have linked on the website (who["links"]["feega"], each {"key", "name"}).
A login's key is its CommonInqID, the 10-digit id the Feega service keeps for the account; the
client sends the same id as its account key (gate msg 500 +0x10), so it is the "key" of every
account here made under that login (Store.by_key, at most 5). The Feega service is asked over the
network (mingol/feega.py): for the key of a link that has only a name, and for each login's name as
it is spelled now. When Feega does not answer, the links' own keys are used. Each account is one
character: its in-game ID, whether it is online and where, whether
the server holds play data for it (the GAMEDB record is not decoded, so nothing more), and its
place in the Japan Tour ranking the boards show (live.japan_tour, by in-game ID). OpenLobby
handles are never matched against in-game IDs: a name in common is a coincidence, not a link.

status.json also carries the update notice (mingol/updates.py): the running build's commit
against the newest release on GitHub, checked at most every 30 minutes off the request path.
MINGOL_UPDATE_REPO points it at another repository, or turns it off when set empty.

There is no Announce: the server sends nothing to a player already online that shows text (the
client's system line, chat type 2 without the leading tab, and the cmd 0x44c ticker are known from
static RE only and this server has never sent them), so the panel's maintenance warning has no
action to use here.

Everything runs on the game's event loop (the lobbies and rooms belong to it); the HTTP side waits.
"""
import asyncio
import json
import os
import sys
import time

from . import live, olpage, olport, tours, updates
from .feega import Feega, FeegaError

WAIT = 10.0
NEWS_MAX = 255              # bytes of cp932; the gate's MINGOL_NEWS line is far shorter


def env(name, default=""):
    return os.environ.get("MINGOL_ADMIN_" + name, default)


def _on_loop(loop, fn):
    """Run fn() (a plain function or a coroutine function) on the game's loop and wait for it."""
    async def run():
        out = fn()
        return await out if asyncio.iscoroutine(out) else out
    return asyncio.run_coroutine_threadsafe(run(), loop).result(WAIT)


def tour_calendar(lo, hi):
    """The real tournaments touching [lo, hi) (unix seconds), as one calendar entry per JST day:
    from the day's first start to its last tour's end, each tour's [start, end] in "times"."""
    out = []
    if hi <= lo:
        return out
    day = tours.day_start(lo) - 86400          # the day before: its last tours end after midnight
    while day < hi:
        sched = tours.schedule(day)
        times = [[s, tours.end_of(s)] for _tid, s in sched]
        start, end = times[0][0], times[-1][1]
        if end > lo and start < hi:
            d = time.gmtime(day + tours.JST)
            out.append({"id": "tours-%s" % time.strftime("%Y%m%d", d),
                        "title": "Tournaments, %s %d" % (time.strftime("%b", d), d.tm_mday),
                        "start": start, "end": end, "kind": "tour",
                        "about": "%d nine-hole Open Tours, one every %d minutes of the Japan day; "
                                 "entry opens %d minutes before each start, %d players at most."
                                 % (len(sched), tours.SPACING // 60, tours.SPACING // 60, tours.CAPACITY),
                        "times": times})
        day += 86400
    return out


def _status(snap):
    """board/live.json's document -> the panel's status shape."""
    in_room = {name: r["caption"] or "Room %d" % r["id"] for r in snap["rooms"] for name in r["players"]}
    players = []
    for p in snap["players"]:
        where = p["where"]
        if where == "room" and p["name"] in in_room:
            where = "room: " + in_room[p["name"]]
        players.append({"name": p["name"], "where": where})
    t = snap["tours"]
    rooms = [{"name": "Lobby %d" % lb["no"], "players": lb["players"], "max": 30, "state": "lobby"}
             for lb in snap["lobbies"]]
    rooms += [{"name": r["caption"] or "Room %d" % r["id"], "players": len(r["players"]), "max": r["max"],
               "state": "playing" if r["playing"] else "open" if r["open"] else "closed"}
              for r in snap["rooms"]]
    rooms += [{"name": x["name"], "players": x["players"], "max": tours.CAPACITY,
               "state": "hole %d of %d" % (x["hole"], x["holes"])} for x in t["playing"]]
    rooms += [{"name": x["name"], "players": x["players"], "max": tours.CAPACITY, "state": "taking entries"}
              for x in t["open"]]
    nxt = t["open"][0] if t["open"] else t["next"]
    summary = ("%s JST is taking entries." if t["open"] else "Next tour: %s JST.") % nxt["name"]
    jt = snap["japan_tour"]
    facts = [{"label": "accounts", "value": snap["totals"]["accounts"]},
             {"label": "Japan Tour rounds", "value": snap["totals"]["japan_tour_rounds"]}]
    if jt:
        facts.append({"label": "Japan Tour leader", "value": "%s (%d)" % (jt[0]["name"], jt[0]["total"])})
    return {"up": True, "online": snap["online"], "players": players, "rooms": rooms,
            "rooms_label": "lobbies, rooms and tours", "summary": summary, "facts": facts}


WHERE = {"server": "online", "lobby": "in a lobby", "room": "in a room", "tour": "in a tournament"}


def feega_logins(who, feega=None):
    """The Feega logins in who["links"] -> {key: the login's name now, or None}, in link order.
    Asks the Feega service (blocking, so not on the game's loop) when there is one."""
    out = {}
    for link in (who.get("links") or {}).get("feega", []):
        key = str(link.get("key") or "").strip()
        name = str(link.get("name") or "").strip()
        login = None
        if feega is not None:
            try:
                if not key:
                    key = feega.key_of(name) or ""
                if key:
                    login = feega.account(key)
            except FeegaError:
                pass
        if key and (key not in out or out[key] is None):
            out[key] = login
    return out


def player_lookup(play, www, who, logins=None):
    """The panel's player lookup: the accounts made under the Feega logins in who["links"]
    -> {"characters": [...]}, or None when none of those logins has an account here.
    `logins` is feega_logins(who) when Feega was asked. Runs on the game's loop (play.players
    belongs to it)."""
    logins = feega_logins(who) if logins is None else logins
    keys = list(logins)
    if not keys or play is None:
        return None
    with play.store.lock:
        accts = [dict(a) for key in keys for a in play.store.by_key(key)]
    if not accts:
        return None
    tour = {r["name"]: r for r in live.japan_tour(www)[0]}
    chars = []
    for a in accts:
        pl = play.players.get(a["user_no"])
        facts = [{"label": "Play data", "value": "yes" if a.get("gamedb") else "none yet"}]
        if logins.get(a["key"]):
            facts.insert(0, {"label": "Feega login", "value": logins[a["key"]]})
        jt = tour.get(a["id"])
        if jt:
            facts.append({"label": "Japan Tour", "value": "place %d, best %d" % (jt["place"], jt["total"])})
        chars.append({"name": a["id"], "detail": WHERE[live._where(pl)] if pl is not None else "offline",
                      "online": pl is not None, "facts": facts})
    return {"characters": chars}


async def disconnect(play, names):
    """Close the play socket of each online player named; -> the names that were online.
    Waits (briefly) for the session's own close path to take them out of the server."""
    hit = [pl for pl in list(play.players.values()) if pl.name in names]
    for pl in hit:
        pl.s.log(event="admin-disconnect", user_no=pl.id)
        pl.s.writer.close()
    for _ in range(40):
        if not any(play.players.get(pl.id) is pl for pl in hit):
            break
        await asyncio.sleep(0.05)
    return [pl.name for pl in hit]


def news_path(data_dir):
    return os.environ.get("MINGOL_NEWS_FILE") or os.path.join(data_dir, "login-news.json")


def load_news(data_dir, default):
    """The login news the panel last set (login-news.json), else `default` (MINGOL_NEWS). A file
    that does not parse is logged and `default` is used."""
    path = news_path(data_dir)
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        return str(doc["text"])
    except FileNotFoundError:
        return default
    except (OSError, ValueError, KeyError, TypeError) as e:
        print(f"adminport: {path} unreadable ({e!r}); login news from MINGOL_NEWS", file=sys.stderr, flush=True)
        return default


def save_news(data_dir, text, actor):
    """Keep the login news across restarts (atomic: temp file, then replace)."""
    path = news_path(data_dir)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"text": text, "by": actor or "?", "at": int(time.time())}, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


JT_ROUNDS = 50              # latest rounds the Japan Tour page lists


def japan_tour_page(www):
    """The Japan Tour tool page: the website's live ranking and the latest rounds posted."""
    if www is None:
        return olpage.page("Japan Tour", olpage.panel("", olpage.table([], [], empty=(
            "The website (and with it the Japan Tour API) does not run in this process."))))
    with www.state.lock:
        ranking = www.live_ranking()
        records = list(www.state.data.get("records", []))
    rows, place, prev = [], 0, None
    for i, r in enumerate(ranking, 1):
        if r["total"] != prev:
            place, prev = i, r["total"]
        rows.append([place, r["handle"], r["total"], r.get("tour", ""), r.get("t", ""), r.get("comment", "")])
    latest = [[r.get("t", ""), r.get("handle", "") or "(no name)", r.get("total", ""), r.get("tour", ""),
               r.get("comment", "")] for r in reversed(records[-JT_ROUNDS:])]
    players = len({r.get("handle") for r in records if r.get("handle")})
    return olpage.page(
        "Japan Tour",
        olpage.panel("Ranking", olpage.table(["Place", "Player", "Total", "Tournament", "Posted (JST)", "Comment"],
                                             rows, empty="No rounds posted yet.")),
        olpage.panel("Latest rounds", olpage.table(["Posted (JST)", "Player", "Total", "Tournament", "Comment"],
                                                   latest, empty="No rounds posted yet.")),
        sub="New Nationals rounds posted from the game: %d from %d players. The ranking is each "
            "player's best total (fewest strokes), as the website's Rankings page shows it (its top 100)."
            % (len(records), players))


def find_players(play, q, limit):
    """The panel's player picker: accounts whose in-game ID contains q, online ones first.
    Runs on the game's loop."""
    q = q.strip().lower()
    with play.store.lock:
        accts = [dict(a) for a in play.store.data.get("accounts", []) if q in a["id"].lower()]
    out = []
    for a in accts:
        pl = play.players.get(a["user_no"])
        out.append({"id": a["id"], "label": a["id"],
                    "detail": WHERE[live._where(pl)] if pl is not None else "offline",
                    "online": pl is not None})
    out.sort(key=lambda p: (not p["online"], p["label"].lower() != q, p["label"].lower()))
    return out[:limit]


def build(loop, play, gate, www=None, token=None, bind=None, port=None, containers=None, data_dir=None,
          feega=None, update_check=None):
    """The AdminPort for the running servers (run.py's play, gate and www); not started.
    `data_dir` is where login news is kept (None: not kept, as in the tests that need no file).
    `feega` is the Feega account API client (feega.Feega), None to look nothing up there.
    `update_check` is the status's update notice (updates.UpdateCheck), None for no notice."""
    containers = containers if containers is not None else [
        c.strip() for c in env("CONTAINERS", "mingol-game-1").split(",") if c.strip()]
    ap = olport.AdminPort(
        "mingol", "Everybody's Golf Online", short="Everybody's Golf", platform="PS2 HDD",
        about="Everybody's Golf Online: lobbies, rooms, real tournaments every 15 minutes and the "
              "Japan Tour; sign-in is the shared Feega login.",
        token=env("TOKEN") if token is None else token,
        bind=env("BIND", "127.0.0.1") if bind is None else bind,
        port=int(env("PORT", "0") or 0) if port is None else port,
        accounts="feega",
        services=[{"name": "Everybody's Golf Online", "containers": containers,
                   "ports": {"12102": "regi", "12103": "gate", "12104": "play", "12105": "web"},
                   "warning": "Everyone in Everybody's Golf Online is disconnected, mid-round included."}])

    @ap.status
    def status():
        out = _status(_on_loop(loop, lambda: live.snapshot(play, www)))
        if update_check is not None:
            out["update"] = update_check.get()
        return out

    ap.calendar(tour_calendar)

    @ap.player
    def player(who):
        logins = feega_logins(who, feega)            # here, off the game's loop: it may wait on Feega
        return _on_loop(loop, lambda: player_lookup(play, www, who, logins))

    if play is not None:
        @ap.players
        def players(q, limit):
            return _on_loop(loop, lambda: find_players(play, q, limit))

    @ap.action("kick", "Disconnect a player", kind="kick", danger=True,
               fields=[{"id": "name", "label": "Player ID", "type": "player"}],
               about="Closes the player's connection; the game shows its own line-lost error. They "
                     "can sign in again straight away.")
    def kick(params, actor):
        raw = str(params.get("name") or params.get("handles") or "")
        names = {n.strip() for n in raw.split(",") if n.strip()}
        if not names:
            return False, "No player named."
        gone = _on_loop(loop, lambda: disconnect(play, names))
        if not gone:
            return False, "Not online."
        return True, "Disconnected %s." % ", ".join(sorted(gone))

    @ap.action("news", "Login news", fields=[{"id": "text", "label": "Text (empty clears it)", "type": "text"}],
               about="The news line players see when they sign in, kept across restarts. Players "
                     "already online do not see it.")
    def news(params, actor):
        text = " ".join(str(params.get("text") or "").split())
        if len(text.encode("cp932", "replace")) > NEWS_MAX:
            return False, "Too long (%d bytes at most)." % NEWS_MAX
        if data_dir is not None:
            try:
                save_news(data_dir, text, actor)
            except OSError as e:
                return False, "Not changed: the news could not be saved (%s)." % e.__class__.__name__

        def put():
            gate.news = text
        _on_loop(loop, put)
        print("adminport: %s set the login news to %r" % (actor or "?", text), file=sys.stderr, flush=True)
        return True, "Shown at the next sign-in." if text else "Login news cleared."

    @ap.tool("japantour", "Japan Tour", "japantour",
             about="The New Nationals ranking and the latest rounds posted from the game.")
    def japan_tour(method, sub, query, body, actor):
        if method != "GET":
            return 405, "application/json", b'{"error": "read-only"}'
        return 200, olpage.CTYPE, japan_tour_page(www).encode()

    return ap


def start(play, gate, www=None, log=print, data_dir=None):
    """Called from run.py's main inside the running loop. Off unless MINGOL_ADMIN_PORT is set; never
    raises: a bad setting is logged and the game runs on without it."""
    if env("PORT", "0").strip() in ("", "0"):
        return None
    try:
        feega = Feega()
        ap = build(asyncio.get_running_loop(), play, gate, www, data_dir=data_dir,
                   feega=feega if feega.url else None, update_check=updates.from_env())
        host, n = ap.start()
    except (OSError, ValueError) as ex:
        log(f"adminport: not started ({ex})")
        return None
    log(f"adminport: OpenLobby admin protocol on {host}:{n}" + ("" if ap.token else " (no token)"))
    return ap
