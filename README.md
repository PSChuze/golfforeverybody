# Everybody's Golf Online

A reference implementation of the servers behind Everybody's Golf Online on the PlayStation 2
(originally released in Japan as *Minna no Golf Online*), and a base for hosting your own. It
answers what the game
connects to after sign-in: the register server, the gate (accounts and login), the play server
(lobbies, rooms, rounds, real tournaments every 15 minutes), the game's website with the
Clubhouse BBS, the Japan Tour (New Nationals) rankings and the update check, all in one Python
process with no dependencies outside the standard library.

Players sign in with a Feega login. Feega is its own service ([freega](https://github.com/PSChuze/freega)),
shared with Bomberman Online; setup starts it when this computer has none.

No game data is included, and the server needs none.

## Quick start

1. Download **Golf Server Setup** for your computer from the [Releases](../../releases) page:
   `Golf-Server-Setup.exe` (Windows), `Golf-Server-Setup-mac.zip` (Mac with Apple silicon) or
   `Golf-Server-Setup-linux-x86_64`.
2. Install [Docker Desktop](https://www.docker.com/products/docker-desktop/) (on Linux, Docker
   Engine) and start it.
3. Double-click Golf Server Setup and answer the questions: who will play, and the server's name.

Setup does the rest: it starts Feega, the shared DNS server and the shared web server on port 80
if they are not running yet, starts the game server, asks your router to open the ports if friends play over the internet, and tells
you what to tell your players. Run it again for a menu: start and stop, settings, update, backups,
logs.

From a clone of this repository, `python setup.py` does the same (Python 3.8 or newer).

## Updating

- From an OpenLobby admin panel: press Update on the game's page. The panel's owner approves
  the updater once, on Settings > Modules, after setup has paired with the panel.
- Run Golf Server Setup and choose Update.
- Or, in the server folder: `python setup.py --update --yes`

Installs of v1.0.0 must run Golf Server Setup and choose Update once; after that the panel's
Update button works.

An update downloads the newest release, builds it while the old version keeps running, then
swaps the files and restarts only what was running. `.env`, `state/`, `backups/` and the logs
are left as they are. The panel shows when a newer release is out; `MINGOL_UPDATE_REPO` in
`.env` points that check at another GitHub repository, or turns it off when empty.

## Players

Players need a game install whose network settings and DNS point at this server; setup shows the
address to use.

The game finds the servers by name. The shared DNS server answers these names with your address:

| name | what |
|---|---|
| `regi.mgo.mingol.net` | register server, TCP 12102 |
| `www.mgo.mingol.net`, `bbs.mgo.mingol.net` | the website and the Clubhouse BBS, TCP 80 |
| `patch.mgo.mingol.net` | the update check, TCP 80 |
| `lib.feega.sonyfinance.co.jp` | Feega sign-in, TCP 14020 (added by Feega) |

The gate (12103) and play (12104) servers are reached by address, not by name.

## Ports

Players must reach TCP 80, 12102 to 12105 and Feega's 14020. Setup opens them on the router with
UPnP when it can, and otherwise lists them.

The game asks port 80 for its update check and website. Port 80 belongs to OpenLobby's shared web
server (the container labelled `org.openlobby.role=http`), which forwards each host name to its
game; setup starts it when there is none and registers `patch.`, `www.` and `bbs.mgo.mingol.net`
for `http://mingol:12105`. The game's own website also answers directly on 12105.

The shared DNS server answers home networks only by default. For friends over the internet, either
give them DNS entries of their own for the names above, or allow their networks in the DNS server's
`POL_DNS_CLIENTS` setting (see OpenLobby's docs/installs.md) and open UDP 53. An open DNS server on
the internet gets abused, so allow only the networks you need.

## Running it by hand

```sh
cp .env.example .env            # fill in MINGOL_PUBLIC_IP and MINGOL_ADMIN_TOKEN
docker network inspect openlobby >/dev/null 2>&1 || docker network create openlobby
docker compose up -d --build
```

Feega must run too (see [freega](https://github.com/PSChuze/freega)), the names above must lead to
this computer, and port 80 must reach the website: register the hosts with the shared web server
(`docker exec <it> python stub.py http-hosts add mingol http://mingol:12105 patch.mgo.mingol.net
www.mgo.mingol.net bbs.mgo.mingol.net`), or put your own forwarder in front.
Without Docker: `python run.py --public-ip 192.0.2.10 --http-port 80` serves port 80 itself. Every option also reads a
`MINGOL_*` environment variable; `python run.py --help` lists them.

Accounts, the website's posts and Japan Tour rounds are kept under `state/data/`, and every frame
both ways is logged under `state/logs/`. Setup's backup zips `state/data/`.

## OpenLobby admin panel

The server can be run from an [OpenLobby](https://github.com/PrettyOpenLobby/OpenLobby) admin
panel on the same computer: who is online and where, lobbies, rooms and tournaments, the
tournament calendar, a Japan Tour page, kicking a player and the news line players see when they
sign in. Setup finds a running panel and pairs with it by the code the panel shows. Without
pairing, give the panel `POL_ADMIN_GAME_MINGOL_URL=http://mingol:12190` and
`POL_ADMIN_GAME_MINGOL_TOKEN` (`MINGOL_ADMIN_TOKEN` from `.env`). The admin port is only on the
`openlobby` Docker network and is never published.

The panel's player lookup finds a member's characters by the Feega logins they linked; the server
asks Feega's account API (`MINGOL_FEEGA_URL`, default `http://feega:24030`) for the logins' keys
and names.

## Development

```sh
python -m unittest discover -s tests -t .
```

The Docker image runs the same tests when it is built.

## License

GPL-3.0-or-later (see [LICENSE](LICENSE)). Third-party code and sources are listed in
[CREDITS.md](CREDITS.md).

Everybody's Golf and Minna no Golf are trademarks of Sony Interactive Entertainment. This project
is not affiliated with or endorsed by Sony Interactive Entertainment or Clap Hanz. All trademarks
belong to their owners.
