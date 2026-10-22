"""Run the Everybody's Golf Online game servers (regi + gate + play + website) in one process.

  python run.py --public-ip 192.0.2.10

regi must be on 12102 (the client's built-in default; DNS regi.mgo.mingol.net -> public ip).
gate and play ports are ours: the client learns them from the regi / gate replies.
Every frame both ways is logged to logs/{regi,gate,play}.jsonl.

Every option also reads a MINGOL_* environment variable. The boards' snapshot goes to
<data dir>/board/live.json (mingol/live.py; MINGOL_LIVE_FILE, MINGOL_LIVE_EVERY).
MINGOL_ADMIN_PORT (+ _BIND, _TOKEN) serves an OpenLobby admin panel (mingol/adminport.py).
Login news set there is kept in <data dir>/login-news.json and wins over --news / MINGOL_NEWS.
--advertise-map hands each client the next-hop address reachable from ITS network, e.g.
"192.168.0.0/16=192.168.0.10,0.0.0.0/0=203.0.113.7"; a client that matches no entry gets
--public-ip.
--http-port also serves the website on the port the game's www/bbs/patch links use (80), with
the update check at patch.mgo.mingol.net; 0 (the default) leaves that to another web server.
"""
import argparse
import asyncio
import os

from mingol import adminport, live
from mingol.accounts import Store
from mingol.play import Play
from mingol.regi_gate import Gate, Regi
from mingol.site.bbs import BBS
from mingol.site.common import Sessions
from mingol.site.httpd import Site
from mingol.site.patch import Patch
from mingol.site.www import WWW

HERE = os.path.dirname(os.path.abspath(__file__))


def env(name, default=None):
    return os.environ.get("MINGOL_" + name, default)


async def main(a):
    store = Store(a.accounts, check_passwords=not a.no_password_check)
    data_dir = os.path.dirname(os.path.abspath(a.accounts))
    regi = Regi(a.public_ip, a.gate_port, advertise_map=a.advertise_map)
    # login news: what the panel last set (data dir/login-news.json), else MINGOL_NEWS
    gate = Gate(a.public_ip, a.play_port, store, news=adminport.load_news(data_dir, a.news),
                advertise_map=a.advertise_map)
    play = Play(a.public_ip, store, advertise_map=a.advertise_map)
    sessions = Sessions()
    # website replica on the web port: bbs.* -> BBS, anything else -> www (+ Japan Tour API)
    www = WWW(store, sessions, data_dir)
    web = Site([(lambda h: h.startswith("bbs."), BBS(store, sessions, data_dir)),
                (lambda h: h.startswith("patch."), Patch()),
                (lambda h: True, www)])
    servers = [await regi.serve(a.bind, a.regi_port), await gate.serve(a.bind, a.gate_port),
               await play.serve(a.bind, a.play_port), await web.serve(a.bind, a.web_port)]
    if a.http_port:
        servers.append(await web.serve(a.bind, a.http_port))
    adminport.start(play, gate, www, log=lambda msg: print(msg, flush=True),  # an OpenLobby panel
                    data_dir=data_dir)
    await asyncio.gather(*(s.serve_forever() for s in servers),
                         live.run(play, www, data_dir))      # the boards' snapshot (mingol/live.py)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--public-ip", default=env("PUBLIC_IP"),
                    help="address the PS2 can reach (sent in regi/gate replies)")
    ap.add_argument("--advertise-map", default=env("ADVERTISE_MAP", ""))
    ap.add_argument("--bind", default=env("BIND", "0.0.0.0"))
    ap.add_argument("--regi-port", type=int, default=int(env("REGI_PORT", 12102)))
    ap.add_argument("--gate-port", type=int, default=int(env("GATE_PORT", 12103)))
    ap.add_argument("--play-port", type=int, default=int(env("PLAY_PORT", 12104)))
    ap.add_argument("--web-port", type=int, default=int(env("WEB_PORT", 12105)),
                    help="website replica + Japan Tour HTTP (ADDRESS.XB WEB/BBS/JAPANTOUR servers point here)")
    ap.add_argument("--http-port", type=int, default=int(env("HTTP_PORT", 0)),
                    help="the website and the update check on the port the game's links use (80); 0 = off")
    ap.add_argument("--accounts", default=env("ACCOUNTS", os.path.join(HERE, "data", "accounts.json")))
    ap.add_argument("--news", default=env("NEWS", "Welcome back to Everybody's Golf Online!"))
    ap.add_argument("--no-password-check", action="store_true",
                    default=env("NO_PASSWORD_CHECK", "") == "1")
    args = ap.parse_args()
    if not args.public_ip:
        ap.error("--public-ip (or MINGOL_PUBLIC_IP) is required")
    asyncio.run(main(args))
