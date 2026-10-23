"""Everybody's Golf Online Server Setup: python setup.py (or the "Golf Server Setup" program)

The first run asks who plays and what the server is called, writes .env, starts the server in
Docker with the Feega sign-in service and the shared DNS it needs, and checks it. Later runs
show a menu: start and stop, settings, update, backups, logs. Needs Docker and nothing else
(Python 3.8+ when run as a script, standard library only).

`python setup.py --update --yes` updates without a question: it is what an OpenLobby panel's
deployer runs in the server folder when the panel's Update button is pressed.
"""
import argparse
import datetime
import json
import os
import re
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile

from hostsetup import openlobby
from hostsetup import progress as proglib
from hostsetup import upnp
from mingol import updates

REPO = os.environ.get("MINGOL_SETUP_REPO", "PSChuze/golfforeverybody")
RELEASES = "https://github.com/%s/releases" % REPO
DOCKER_DOWNLOAD = "https://www.docker.com/products/docker-desktop/"
DOCKER_LINUX = "https://docs.docker.com/engine/install/"
PORT_HELP = "https://portforward.com/router.htm"
TITLE = "Everybody's Golf Online"
KEY = "mingol"
DEFAULT_FOLDER = "Golf Server"
DEFAULT_NAME = "Everybody's Golf Online"
FROZEN = getattr(sys, "frozen", False)

GAME_PORTS = (12102, 12103, 12104, 12105)        # fixed in compose.yml
HTTP_PORT = 80                                   # the shared web server (OpenLobby's http service)
SITE_PORT = 12105                                # the game's website, also published for checks
HTTP_HOSTS = ("patch.mgo.mingol.net", "www.mgo.mingol.net", "bbs.mgo.mingol.net")
SITE_UPSTREAM = "http://mingol:%d" % SITE_PORT
FEEGA_PORT = 14020                               # Feega over TLS, published by freega
DNS_NAMES = ("mgo.mingol.net",)                   # regi, www, bbs and patch are under it
ADMIN_URL = "http://mingol:12190"
# A freega release to start when this machine has no Feega yet ("" = the newest).
# MINGOL_FEEGA_SOURCE (a zip URL or a folder) overrides it.
FEEGA_TAG = ""
# Files of ours in the server folder that an update must never replace.
KEEP = (".env", "compose.override.yml", "state", "backups", "logs")
# What a panel's deployer runs in the server folder to update it (python 3 is in its image).
UPDATER_COMMAND = ["python", "setup.py", "--update", "--yes"]
UPDATER_MINUTES = 60
# Where the update's check finds the register server: this machine, or the game's name on the
# openlobby network when the update runs in a container there (a panel's deployer).
CHECK_HOSTS = ("127.0.0.1", "mingol")


def use_root(path):
    """Point every path at the server folder `path`."""
    global ROOT, ENV, ENV_EXAMPLE, BACKUPS, CHOICES, LOG, VERSION_FILE
    ROOT = os.path.abspath(path)
    ENV = os.path.join(ROOT, ".env")
    ENV_EXAMPLE = os.path.join(ROOT, ".env.example")
    BACKUPS = os.path.join(ROOT, "backups")
    CHOICES = os.path.join(ROOT, "state", "setup.json")
    LOG = os.path.join(ROOT, "logs", "setup.log")
    VERSION_FILE = os.path.join(ROOT, ".setup-version")


use_root(os.path.dirname(os.path.abspath(__file__)))


# --- terminal ----------------------------------------------------------------------------

def say(text=""):
    print(text, flush=True)


def ask(question, default=""):
    shown = " [%s]" % default if default else ""
    try:
        answer = input("%s%s: " % (question, shown)).strip()
    except EOFError:
        answer = ""
    return answer or default


def ask_yes(question, default=True):
    answer = ask(question + (" (Y/n)" if default else " (y/N)")).lower()
    return default if not answer else answer.startswith("y")


def ask_choice(question, options, default=1):
    """Number options from 1 and return the 1-based pick."""
    if question:
        say(question)
    for i, text in enumerate(options, 1):
        say("  %d. %s" % (i, text))
    while True:
        answer = ask("Type a number", str(default))
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return int(answer)
        say("  Type one of the numbers above.")


def heading(text):
    say()
    say(text)
    say("-" * len(text))


def log_file():
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    f = open(LOG, "a", encoding="utf-8")
    f.write("\n=== %s ===\n" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    return f


# --- the server's files -------------------------------------------------------------------

def find_root(args_folder=None):
    """The server folder: the one given, else the folder this runs from when it holds the
    server, else "Golf Server" in the home folder (made when missing)."""
    if args_folder:
        return os.path.abspath(args_folder)
    if not FROZEN:
        return os.path.dirname(os.path.abspath(__file__))
    for d in (os.path.dirname(sys.executable), os.getcwd()):
        if os.path.exists(os.path.join(d, "compose.yml")):
            return d
    return os.path.join(os.path.expanduser("~"), DEFAULT_FOLDER)


def bundled(name):
    """A file packed into the setup program, or None."""
    base = getattr(sys, "_MEIPASS", None)
    path = os.path.join(base, name) if base else None
    return path if path and os.path.exists(path) else None


def unpack_server(archive, root, version=""):
    """Unpack a source zip of this repository into root, replacing the files the last one put
    there and leaving the host's own (.env, state, ...) alone. Returns the paths it wrote."""
    with zipfile.ZipFile(archive) as z:
        names = [n for n in z.namelist() if not n.endswith("/")]
        # GitHub's and git archive's zips put everything in one top folder.
        tops = {n.split("/", 1)[0] for n in z.namelist()}
        strip = len(tops) == 1 and all("/" in n for n in names)
        old = set(read_manifest(root))
        written = []
        for name in names:
            rel = name.split("/", 1)[1] if strip else name
            if not rel or rel.split("/", 1)[0] in KEEP:
                continue
            target = os.path.normpath(os.path.join(root, rel))
            if not target.startswith(os.path.normpath(root) + os.sep):
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with z.open(name) as src, open(target + ".tmp", "wb") as dst:
                shutil.copyfileobj(src, dst)
            os.replace(target + ".tmp", target)
            written.append(rel)
    for rel in old - set(written):
        try:
            os.remove(os.path.join(root, rel))
        except OSError:
            pass
    with open(os.path.join(root, ".setup-files"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(sorted(written)) + "\n")
    if version:
        with open(os.path.join(root, ".setup-version"), "w", encoding="utf-8") as f:
            f.write(version + "\n")
    return written


def read_manifest(root):
    try:
        with open(os.path.join(root, ".setup-files"), encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip()]
    except OSError:
        return []


def installed_version():
    try:
        with open(VERSION_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def ensure_server_files():
    """Put the server's files in ROOT when they are not there yet."""
    if os.path.exists(os.path.join(ROOT, "compose.yml")):
        return True
    archive = bundled("server.zip")
    if not archive:
        say("The server's files are not in %s, and this setup program does not carry them." % ROOT)
        say("Download Golf Server Setup again from %s" % RELEASES)
        return False
    os.makedirs(ROOT, exist_ok=True)
    version = ""
    if bundled("server-version.txt"):
        with open(bundled("server-version.txt"), encoding="utf-8") as f:
            version = f.read().strip()
    unpack_server(archive, ROOT, version)
    say("The server's files are in %s" % ROOT)
    return True


def latest_release():
    """The newest release ({"tag", "commit", "date", "zip"}). Raises updates.Failed."""
    return updates.latest_release(REPO)


# --- docker ------------------------------------------------------------------------------

def run(cmd, check=True, capture=False, **kw):
    """Run a command in the server folder; its output goes to the terminal unless captured."""
    if capture:
        kw.update(stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    result = subprocess.run(cmd, cwd=ROOT if os.path.isdir(ROOT) else None, **kw)
    if check and result.returncode != 0:
        raise SystemExit("Failed (exit %d): %s" % (result.returncode, " ".join(cmd)))
    return result


def compose(*args, **kw):
    return run(["docker", "compose"] + list(args), **kw)


def add_docker_to_path():
    """Docker installed while this window was open is not on its PATH yet."""
    places = [os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Docker", "Docker", "resources",
                           "bin"),
              "/usr/local/bin", "/Applications/Docker.app/Contents/Resources/bin",
              os.path.join(os.path.expanduser("~"), ".docker", "bin")]
    path = os.environ.get("PATH", "").split(os.pathsep)
    for p in places:
        if os.path.isdir(p) and p not in path:
            path.append(p)
    os.environ["PATH"] = os.pathsep.join(path)


def docker_state():
    """"ok", "missing", "stopped", "denied" (Linux user not allowed) or "old"."""
    add_docker_to_path()
    if not shutil.which("docker"):
        return "missing"
    try:
        info = run(["docker", "info"], check=False, capture=True, timeout=60)
    except subprocess.TimeoutExpired:
        return "stopped"
    if info.returncode != 0:
        return "denied" if "permission denied" in (info.stdout or "").lower() else "stopped"
    if run(["docker", "compose", "version"], check=False, capture=True).returncode != 0:
        return "old"
    return "ok"


def start_docker_desktop():
    """Ask Docker Desktop to start; True when there was one to start."""
    if os.name == "nt":
        exe = os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Docker", "Docker",
                           "Docker Desktop.exe")
        if os.path.exists(exe):
            subprocess.Popen([exe], close_fds=True)
            return True
    elif sys.platform == "darwin" and os.path.isdir("/Applications/Docker.app"):
        return subprocess.run(["open", "-a", "Docker"]).returncode == 0
    return False


def wait_for_docker(wait=True):
    """Return once Docker works. Explains what is missing and waits for the host to fix it
    (or returns False at once with wait=False)."""
    started = False
    while True:
        state = docker_state()
        if state == "ok":
            return True
        say()
        if state == "missing":
            if sys.platform.startswith("linux"):
                say("Docker is not installed. Install Docker Engine: %s" % DOCKER_LINUX)
            else:
                say("Docker Desktop is not installed. This server runs inside it.")
                say("Download it here, install it and start it: %s" % DOCKER_DOWNLOAD)
                say("If it asks you to sign in, you can skip that.")
        elif state == "stopped" and not started and wait and start_docker_desktop():
            started = True
            say("Starting Docker Desktop. This can take a minute or two.")
            for _ in range(60):
                time.sleep(3)
                if docker_state() == "ok":
                    say("Docker Desktop is running.")
                    return True
            say("Docker Desktop has not started yet. If it shows a message (terms to accept,")
            say("an update to install), answer it.")
        elif state == "stopped":
            if sys.platform.startswith("linux"):
                say("Docker is installed but not running. Start it with: sudo systemctl start docker")
            else:
                say("Docker Desktop is installed but not running. Open it and wait until it says")
                say("it is running.")
        elif state == "denied":
            say("Your user may not use Docker yet. Run this, then log out and back in:")
            say("  sudo usermod -aG docker $USER")
        else:
            say("This Docker is too old (it has no 'docker compose'). Update it: %s" % DOCKER_DOWNLOAD)
        if not wait:
            return False
        ask("Press Enter to check again (or close this window to stop)")


def docker_run(args, timeout=600, cwd=None):
    """openlobby's way to run docker, from the server folder unless told otherwise."""
    return openlobby.run_docker(args, timeout=timeout, cwd=cwd or (ROOT if os.path.isdir(ROOT) else None))


def server_running():
    out = compose("ps", "--status", "running", "--services", check=False, capture=True).stdout or ""
    return "game" in out.split()


# --- checks ------------------------------------------------------------------------------

def regi_answers(host="127.0.0.1", port=12102, timeout=5.0):
    """Ask the register server for the world list as the game does; True when it answers."""
    hello = struct.pack("<BBH4sHH", 12, 0x1D, 0, socket.inet_aton("127.0.0.1"), 0, 0)
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.sendall(hello)
            head = b""
            while len(head) < 12:
                chunk = s.recv(12 - len(head))
                if not chunk:
                    return False
                head += chunk
    except OSError:
        return False
    return head[0] == 12 and head[1] & 0x1F == 0x1D


def patch_answers(port=SITE_PORT, timeout=5.0, host="127.0.0.1"):
    """The update check the game makes first; True when it gets the "no patches" answer."""
    req = urllib.request.Request("http://%s:%d/patch/ipatcher.inf" % (host, port),
                                 headers={"Host": "patch.mgo.mingol.net"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200 and r.read() == bytes.fromhex("2821213833")
    except (OSError, ValueError):
        return False


def wait_ready(env, timeout=120, hosts=("127.0.0.1",)):
    """Wait for the register server to answer -> the host it answered on, or None."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for host in hosts:
            if regi_answers(host):
                return host
        time.sleep(2)
    return None


def smoke(quiet=False):
    env = read_env()
    if not quiet:
        say("Checking the server...")
    regi = regi_answers()
    patch = patch_answers(SITE_PORT)
    shared = patch_answers(HTTP_PORT)
    if regi and patch and shared:
        say("The server answers as it should.")
        return True
    if not regi:
        say("The register server (port 12102) does not answer. See the server's log (menu: logs).")
    if not patch:
        say("The website on port %d does not answer the game's update check." % SITE_PORT)
    elif not shared:
        say("The shared web server on port 80 does not pass the game's update check on. Choose")
        say("Start the server again, which registers the game's names with it.")
    return False


# --- settings ----------------------------------------------------------------------------

def read_env(path=None):
    path = path or ENV
    values = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                m = re.match(r"\s*([A-Z_][A-Z0-9_]*)=(.*)$", line.rstrip("\n"))
                if m:
                    values[m.group(1)] = m.group(2).strip()
    return values


def write_env(values, path=None, template=None):
    """Write values into the template's lines (a commented-out `#KEY=` line is switched on,
    a value of None comments a line out); keys the template does not have go at the end.
    Comments stay as they are."""
    path, template = path or ENV, template or ENV_EXAMPLE
    source = path if os.path.exists(path) else template
    with open(source, encoding="utf-8") as f:
        lines = f.read().splitlines()
    left = dict(values)
    out = []
    for line in lines:
        m = re.match(r"(#?)([A-Z_][A-Z0-9_]*)=", line)
        if m and m.group(2) in left:
            value = left.pop(m.group(2))
            out.append(("#" + line.lstrip("#")) if value is None else "%s=%s" % (m.group(2), value))
            continue
        out.append(line)
    for key, value in left.items():
        if value is not None:
            out.append("%s=%s" % (key, value))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(out) + "\n")
    os.replace(tmp, path)


def read_choices():
    try:
        with open(CHOICES, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_choices(choices):
    os.makedirs(os.path.dirname(CHOICES), exist_ok=True)
    with open(CHOICES, "w", encoding="utf-8", newline="\n") as f:
        json.dump(choices, f, indent=1)
        f.write("\n")


def lan_address():
    """This machine's address on the local network (no packet is sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            return s.getsockname()[0]
    except OSError:
        return ""


def public_address():
    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=4) as r:
            ip = r.read().decode().strip()
        socket.inet_aton(ip)
        return ip
    except (OSError, ValueError):
        return ""


def to_ip(address):
    """The IPv4 address for what the host typed (an address, or a name that leads to one), or ""."""
    try:
        socket.inet_aton(address)
        return address if address.count(".") == 3 else ""
    except OSError:
        pass
    try:
        return socket.gethostbyname(address)
    except (OSError, UnicodeError):
        return ""


def who_plays(env, choices):
    """"home" or "internet": as chosen, or guessed from the address."""
    if choices.get("who") in ("home", "internet"):
        return choices["who"]
    return "home" if upnp.is_private(env.get("MINGOL_PUBLIC_IP", "")) else "internet"


def first_settings(address, name, env):
    """The .env values for the answers. Everything not asked keeps its value in env (the
    template on a first run) or gets the usual default."""
    changes = {"MINGOL_PUBLIC_IP": address, "MINGOL_NEWS": "Welcome to %s!" % name}
    if not env.get("MINGOL_ADMIN_TOKEN"):
        changes["MINGOL_ADMIN_TOKEN"] = secrets.token_hex(24)
    return changes


def ports_to_open(env, feega_here=True):
    """The TCP ports players must reach, in order."""
    ports = list(GAME_PORTS) + [HTTP_PORT]
    if feega_here:
        ports.append(FEEGA_PORT)
    return ports


def port_ranges(ports):
    """[(first, last)] runs of consecutive ports, for a short table."""
    out = []
    for p in sorted(set(ports)):
        if out and p == out[-1][1] + 1:
            out[-1] = (out[-1][0], p)
        else:
            out.append((p, p))
    return out


def port_in_use(port):
    """True when something on this machine already listens on the TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


# --- the questions -----------------------------------------------------------------------

def ask_who(current="home", total=2):
    heading("Question 1 of %d: Who will play on this server?" % total)
    pick = ask_choice("", ["Just people on my home network", "Friends over the internet"],
                      2 if current == "internet" else 1)
    return "internet" if pick == 2 else "home"


def ask_address(who, current=""):
    """The address players' consoles reach: this machine's for home, the internet one otherwise."""
    if who == "home":
        lan = lan_address()
        if lan:
            say("Players at home reach this computer at %s." % lan)
            return lan
        say("Setup could not find this computer's address on your network.")
        default = current
    else:
        public = public_address()
        default = current if current and not upnp.is_private(current) else public
        if public:
            say("Your internet address is %s." % public)
        if default:
            say("If that is the address friends reach, press Enter. Otherwise type it.")
        else:
            say("Setup could not find your internet address. Type it, or a name that leads to it.")
    while True:
        typed = ask("Address", default)
        address = to_ip(typed) if typed else ""
        if address:
            if address != typed:
                say("That name leads to %s; setup uses that address." % address)
            return address
        if typed:
            say("  That is not an address, and no address could be found for that name.")


def ask_name(current=DEFAULT_NAME, number=2, total=2):
    heading("Question %d of %d: Give your server a name" % (number, total))
    say("Players see it in the news when they sign in.")
    return ask("Server name", current) or DEFAULT_NAME


def ask_pairing(panel, number=3, total=3):
    heading("Question %d of %d: Use your OpenLobby admin panel?" % (number, total))
    say("An OpenLobby admin panel runs on this computer (%s)." % panel)
    return ask_yes("Manage %s from it?" % TITLE)


# --- the parts it needs ------------------------------------------------------------------

def feega_folder():
    return os.path.join(os.path.dirname(ROOT), "Freega")


def ensure_feega(env, progress, log):
    """Use the running Feega, or start one. -> True when one runs."""
    if openlobby.find_feega(docker_run):
        return True
    progress.begin("Starting Feega, the sign-in service", "a few minutes the first time")
    source = os.environ.get("MINGOL_FEEGA_SOURCE") or openlobby.feega_source(FEEGA_TAG)
    ok, msg = openlobby.start_feega(feega_folder(), env.get("MINGOL_PUBLIC_IP", ""), source,
                                    run=docker_run, log=log)
    progress.end(ok)
    if not ok:
        say(msg)
        say("Players cannot sign in without Feega. Start it yourself (see %s/INTERFACE.md)," % openlobby.FEEGA_REPO)
        say("then run Golf Server Setup again.")
    return ok


def ensure_dns(env, progress, log, feega_started=False):
    """Register the game's names (and Feega's) with the shared DNS, starting it when there is none."""
    progress.begin("Setting up the DNS server", "a few seconds")
    dns = openlobby.find_dns(docker_run)
    if not dns:
        if port_in_use(53) or udp_port_taken(53):
            progress.end(False)
            say("Port 53 is taken by another DNS server on this computer, so the shared DNS cannot")
            say("start. Point these names at %s on that server: %s, %s" % (
                env.get("MINGOL_PUBLIC_IP"), ", ".join(DNS_NAMES), ", ".join(openlobby.FEEGA_NAMES)))
            return False
        dns = openlobby.start_dns(os.path.join(ROOT, "state", "dns"), env.get("MINGOL_PUBLIC_IP", ""), docker_run)
    ok = False
    if dns:
        ok, out = openlobby.dns_names(dns, KEY, DNS_NAMES, docker_run)
        log.write(out + "\n")
        if ok and (feega_started or not feega_names_known(dns)):
            openlobby.dns_names(dns, "feega", openlobby.FEEGA_NAMES, docker_run)
    progress.end(ok)
    if not ok:
        say("The shared DNS server did not take the game's names. Its image may be too old to")
        say("have 'dns-names': update it (docker pull %s) and run setup again." % openlobby.DNS_IMAGE)
    return ok


def ensure_http(progress, log):
    """Send the game's web host names on port 80 to it, starting the shared web server when there is none."""
    progress.begin("Setting up the web server on port 80", "a few seconds")
    http = openlobby.find_http(docker_run)
    if not http:
        if port_in_use(HTTP_PORT):
            progress.end(False)
            say("Port 80 is used by another program on this computer, so the shared web server cannot")
            say("start. The game asks for its update check there: players cannot sign in until it is")
            say("free, or until that program forwards %s to port %d here." % (", ".join(HTTP_HOSTS), SITE_PORT))
            return False
        http = openlobby.start_http(os.path.join(ROOT, "state", "http"), docker_run)
    ok = False
    if http:
        ok, out = openlobby.http_hosts(http, KEY, SITE_UPSTREAM, HTTP_HOSTS, docker_run)
        log.write(out + "\n")
    progress.end(ok)
    if not ok:
        say("The shared web server did not take the game's names. Its image may be too old to")
        say("have 'http-hosts': update it (docker pull %s) and run setup again." % openlobby.HTTP_IMAGE)
    return ok


def feega_names_known(dns):
    code, out = docker_run(["exec", dns, "python", "stub.py", "dns-names", "list"], timeout=60)
    return code == 0 and openlobby.FEEGA_NAMES[0] in out


def udp_port_taken(port):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.bind(("0.0.0.0", port))
        except OSError as e:
            return getattr(e, "errno", 0) not in (13, 10013)     # no permission is not "taken"
    return False


def pair_with_panel(env, panel):
    """Pair with the panel by its one-time code; keeps the secret in .env."""
    say("In the panel, open Settings, Modules, \"Pair with a setup program\". It shows a code.")
    for _ in range(3):
        code = ask("The code (Enter to skip)")
        if not code:
            say("Skipped. Choose Change settings, then OpenLobby admin panel, to pair later.")
            return False
        ok, got, answer = openlobby.pair(panel, code, KEY, TITLE, ADMIN_URL, env["MINGOL_ADMIN_TOKEN"],
                                         updater=updater())
        if ok:
            write_env({"OPENLOBBY_PAIRING": got})
            say("Paired: the panel now shows %s." % TITLE)
            updater_sent(answer)
            return True
        say(got)
    return False


def updater():
    """How the panel's deployer updates this server: the command, run in this folder."""
    return {"folder": ROOT, "command": list(UPDATER_COMMAND), "timeout_minutes": UPDATER_MINUTES}


def updater_sent(answer):
    """Remember which folder the panel was given, and pass on what it said about it."""
    choices = read_choices()
    choices["updater_folder"] = ROOT
    write_choices(choices)
    if answer == "requested":
        say("For the panel's Update button, its owner must approve it on Settings > Modules.")


def resend_updater():
    """Give a paired panel this folder again when it is not the one it was given (the folder
    moved, or the pairing is older than the panel's Update button)."""
    env = read_env()
    if not env.get("OPENLOBBY_PAIRING") or read_choices().get("updater_folder") == ROOT:
        return
    panel = openlobby.find_panel(docker_run)
    if not panel:
        return
    ok, _why, answer = openlobby.pair_update(panel, KEY, env["OPENLOBBY_PAIRING"],
                                             env.get("MINGOL_ADMIN_TOKEN", ""), updater=updater())
    if ok:
        updater_sent(answer)


# --- building and starting -------------------------------------------------------------------

def failed(what, tail):
    say()
    say(what)
    reason = proglib.explain(tail)
    if reason:
        say(reason)
    say("The last lines it wrote:")
    for line in tail[-12:]:
        say("  " + line)
    say("Everything it wrote is in %s" % LOG)


def start_server(progress, log, build=True):
    env = read_env()
    if not openlobby.ensure_network(docker_run):
        say("The Docker network %s could not be made." % openlobby.NETWORK)
        return False
    if build:
        progress.begin("Getting the server program ready", "about a minute the first time")
        code, tail = proglib.run_logged(["docker", "compose", "build", "game"], log, cwd=ROOT, progress=progress)
        progress.end(code == 0)
        if code != 0:
            failed("Getting the server program ready failed.", tail)
            return False
    progress.begin("Starting the server", "under a minute")
    code, tail = proglib.run_logged(["docker", "compose", "up", "-d"], log, cwd=ROOT, progress=progress)
    ok = code == 0 and wait_ready(env)
    progress.end(ok)
    if code != 0:
        failed("Starting the server failed.", tail)
        return False
    if not ok:
        say("The server did not answer. Its last lines:")
        say(compose("logs", "--tail", "30", "game", check=False, capture=True).stdout)
        return False
    progress.begin("Checking the server", "a few seconds")
    ok = patch_answers(SITE_PORT)
    progress.end(ok)
    if not ok:
        say("The server runs, but its website does not answer the game's update check.")
    return ok


# --- router ------------------------------------------------------------------------------

def open_router_ports(env, quiet=False):
    """Ask the router to forward the ports. Returns (all open?, the router's internet address or "")."""
    lan = lan_address()
    ports = ports_to_open(env)
    if not quiet:
        say("Asking your router to open the ports...")
    gateway = upnp.find_gateway(lan) if lan else None
    if not gateway:
        if not quiet:
            say("Your router did not answer (its UPnP setting may be off). The ports need opening by hand.")
        return False, ""
    results = upnp.open_ports(gateway, ports, lan)
    try:
        external = gateway.external_ip()
    except upnp.UpnpError:
        external = ""
    good = [p for p, r in results.items() if r in ("opened", "already")]
    bad = {p: r for p, r in results.items() if p not in good}
    if not quiet or bad:
        if not bad:
            say("Your router opened the ports for this computer (%s)." % lan)
        else:
            say("Your router opened %d of %d ports. Not opened:" % (len(good), len(ports)))
            for p, r in bad.items():
                say("  %d: %s" % (p, "another device uses it" if r == "taken" else r))
    if external and upnp.is_private(external):
        say()
        say("Note: your router has no internet address of its own (%s). Your internet provider" % external)
        say("probably shares one address between customers, or there is a second router in front")
        say("of this one. Friends outside your home will likely not reach the server. Ask your")
        say("provider for a public IP address.")
    return not bad, external


def show_port_help(env, router=""):
    lan = lan_address() or "this computer"
    heading("Open these ports on your router")
    say("Your router needs to send these ports to this computer (%s)." % lan)
    if router:
        say("Open the router's settings page (usually http://%s, or the address printed on the" % router)
        say('router) and look for "Port forwarding" (or "Virtual server", or "NAT").')
    else:
        say("Open the router's settings page (its address is printed on the router) and look for")
        say('"Port forwarding" (or "Virtual server", or "NAT").')
    say()
    say("  Ports            Type   To")
    for first, last in port_ranges(ports_to_open(env)):
        shown = str(first) if first == last else "%d-%d" % (first, last)
        say("  %-16s TCP    %s" % (shown, lan))
    say()
    say("Steps for many router models: %s" % PORT_HELP)


# --- finishing ---------------------------------------------------------------------------

def finish(env, choices):
    host = env["MINGOL_PUBLIC_IP"]
    say()
    say("=" * 72)
    say("Your server is running.")
    say()
    say("Tell your players: set the DNS server in the game's network settings to %s" % host)
    if who_plays(env, choices) == "internet":
        say()
        if choices.get("router") == "open":
            say("Your router has the ports open.")
        else:
            say("Friends can join once the ports above are open on your router.")
        say("The shared DNS server answers only home networks unless you allow more (see README.md),")
        say("so friends over the internet may need their own DNS entries for %s." % ", ".join(DNS_NAMES))
    say()
    say("The server keeps running when you close this window, as long as Docker runs.")
    say("Run Golf Server Setup again to stop it, change settings or make backups.")
    say("=" * 72)


def first_run(dry_run=False):
    say("Golf Server Setup")
    say("=================")
    say("This sets up an %s server on this computer. Answer a few questions," % TITLE)
    say("then setup does the rest in a few minutes.")
    env = read_env(ENV_EXAMPLE)
    choices = read_choices()
    panel = None if dry_run else openlobby.find_panel(docker_run)
    total = 3 if panel else 2
    who = ask_who(total=total)
    address = ask_address(who)
    name = ask_name(total=total)
    use_panel = ask_pairing(panel, total=total) if panel else False
    changes = first_settings(address, name, env)
    choices.update(who=who, name=name)

    heading("Ready")
    say("  Players:  %s, at %s" % ("people on your home network" if who == "home" else "friends over the internet",
                                   address))
    say("  Name:     %s" % name)
    if panel:
        say("  Panel:    %s" % ("manage from the OpenLobby panel" if use_panel else "not used"))
    say("Everything else uses the usual settings. You can change them later: run Golf Server Setup")
    say("again and choose Change settings.")
    if dry_run:
        say()
        say("Dry run: nothing was written. Setup would write these settings to %s:" % ENV)
        for key, value in changes.items():
            if key != "MINGOL_ADMIN_TOKEN":
                say("  %s=%s" % (key, value))
        say("  MINGOL_ADMIN_TOKEN: made at random")
        return
    if not ask_yes("Start?"):
        return
    write_env(changes)
    env = read_env()
    write_choices(choices)
    say()
    say("Details of every step go to %s" % LOG)
    if who == "internet":
        heading("Router")
        ok, router = open_router_ports(env)
        choices["router"] = "open" if ok else "closed"
        write_choices(choices)
        if not ok:
            show_port_help(env, router)
    if complete_setup(env, choices) and use_panel:
        heading("OpenLobby admin panel")
        pair_with_panel(read_env(), panel)


def complete_setup(env, choices):
    """Start what is missing, then the server."""
    heading("Starting")
    progress = proglib.Progress()
    with log_file() as log:
        if not openlobby.ensure_network(docker_run):
            say("The Docker network %s could not be made." % openlobby.NETWORK)
            return False
        had_feega = bool(openlobby.find_feega(docker_run))
        if not ensure_feega(env, progress, log):
            return False
        ensure_dns(env, progress, log, feega_started=not had_feega)
        if not start_server(progress, log):
            return False
        ensure_http(progress, log)
    finish(read_env(), choices)
    return True


# --- menu --------------------------------------------------------------------------------

def restart_if_running(was_running):
    if was_running:
        say("Restarting the server with the new settings...")
        compose("up", "-d")
        if wait_ready(read_env()):
            smoke()


def change_settings():
    """The settings menu. Returns once the host goes back."""
    while True:
        env, choices = read_env(), read_choices()
        who = who_plays(env, choices)
        items = [
            ("Who will play (now: %s, at %s)" % ("home network" if who == "home" else "friends over the internet",
                                                 env.get("MINGOL_PUBLIC_IP")), change_who),
            ("Server name and sign-in news (now: %s)" % (choices.get("name") or DEFAULT_NAME), change_name),
            ("OpenLobby admin panel (now: %s)" % ("paired" if env.get("OPENLOBBY_PAIRING") else "not paired"),
             change_panel),
            ("Make a new admin token", new_token),
        ]
        heading("Change settings")
        for i, (title, _) in enumerate(items, 1):
            say("  %d. %s" % (i, title))
        say("  0. Back")
        pick = ask("Type a number", "0")
        if pick == "0":
            return
        if pick.isdigit() and 1 <= int(pick) <= len(items):
            running = server_running()
            if items[int(pick) - 1][1](env, read_choices()):
                restart_if_running(running)


def change_who(env, choices):
    who = ask_who(who_plays(env, choices), total=1)
    address = ask_address(who, env.get("MINGOL_PUBLIC_IP", ""))
    choices["who"] = who
    write_env({"MINGOL_PUBLIC_IP": address})
    if who == "internet":
        ok, router = open_router_ports(read_env())
        choices["router"] = "open" if ok else "closed"
        if not ok:
            show_port_help(read_env(), router)
    write_choices(choices)
    say("Players now set their DNS server to %s." % address)
    return True


def change_name(env, choices):
    name = ask("Server name", choices.get("name") or DEFAULT_NAME)
    news = ask("Sign-in news", env.get("MINGOL_NEWS") or "Welcome to %s!" % name)
    write_env({"MINGOL_NEWS": news})
    choices["name"] = name
    write_choices(choices)
    say("Saved. News set in the admin panel wins over this one until it is cleared there.")
    return True


def change_panel(env, choices):
    panel = openlobby.find_panel(docker_run)
    if not panel:
        say("No OpenLobby admin panel runs on this computer.")
        say("Without pairing, a panel can add this game with POL_ADMIN_GAME_MINGOL_URL=%s" % ADMIN_URL)
        say("and POL_ADMIN_GAME_MINGOL_TOKEN set to MINGOL_ADMIN_TOKEN from %s." % ENV)
        return False
    pair_with_panel(env, panel)
    return False


def new_token(env, choices):
    if not ask_yes("Make a new admin token? A panel that is not paired needs the new one by hand.", False):
        return False
    token = secrets.token_hex(24)
    write_env({"MINGOL_ADMIN_TOKEN": token})
    panel = openlobby.find_panel(docker_run)
    if env.get("OPENLOBBY_PAIRING") and panel:
        ok, why, answer = openlobby.pair_update(panel, KEY, env["OPENLOBBY_PAIRING"], token, updater=updater())
        say("The panel has the new token." if ok else "The panel was not told (%s). Pair again." % why)
        if ok:
            updater_sent(answer)
    return True


def start_server_menu():
    if server_running():
        say("The server is already running.")
        return
    env, choices = read_env(), read_choices()
    if who_plays(env, choices) == "internet":
        open_router_ports(env, quiet=True)
    progress = proglib.Progress()
    with log_file() as log:
        if start_server(progress, log, build=False):
            ensure_http(progress, log)
            say("Started.")


def uninstall():
    """Stop and remove the server's container, and take its names off the shared DNS and web server.
    The server folder (accounts in state/data) stays; Feega and the shared services keep running."""
    say("This removes the server's container and takes its names off the shared DNS and web")
    say("server. Accounts and backups stay in %s; delete that folder yourself if you want." % ROOT)
    if not ask_yes("Remove the server?", False):
        return
    compose("down", check=False)
    http = openlobby.find_http(docker_run)
    if http:
        openlobby.http_remove(http, KEY, docker_run)
    dns = openlobby.find_dns(docker_run)
    if dns:
        openlobby.dns_remove(dns, KEY, docker_run)
    say("Removed. Run Golf Server Setup again to set it up anew.")


def stop_server():
    if not server_running():
        say("The server is not running.")
        return
    if ask_yes("Stop the server? Players in the game are disconnected.", False):
        compose("stop")
        say("Stopped. It starts again when you choose Start the server.")


class UpdateFailed(Exception):
    """An update that stopped; the message is the one sentence the host (or the panel) sees last."""


def same_commit(a, b):
    return bool(a and b) and (a.startswith(b) or b.startswith(a))


def running_services():
    """The compose services running now. Raises UpdateFailed when Docker does not say."""
    result = run(["docker", "compose", "ps", "--status", "running", "--services"], check=False,
                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                 text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise UpdateFailed("Docker did not list the server's services, so nothing was updated.")
    return [s for s in (result.stdout or "").split() if re.match(r"^[A-Za-z0-9_.-]+$", s)]


def download(url, path, timeout=60):
    with urllib.request.urlopen(url, timeout=timeout) as r, open(path, "wb") as f:
        shutil.copyfileobj(r, f)


def stage_release(release, work):
    """Download and unpack the release into work/server -> (archive, staged folder)."""
    archive = os.path.join(work, "source.zip")
    say("Downloading %s..." % release["tag"])
    try:
        download(release["zip"], archive)
    except (OSError, ValueError) as e:
        raise UpdateFailed("The download of %s failed (%s), so nothing was updated."
                           % (release["tag"], e.__class__.__name__))
    say("Unpacking %s..." % release["tag"])
    stage = os.path.join(work, "server")
    try:
        unpack_server(archive, stage, release["tag"])
    except (OSError, zipfile.BadZipFile) as e:
        raise UpdateFailed("The download of %s is damaged (%s), so nothing was updated."
                           % (release["tag"], e.__class__.__name__))
    for need in ("compose.yml", "Dockerfile", "setup.py", "run.py"):
        if not os.path.exists(os.path.join(stage, need)):
            raise UpdateFailed("Release %s has no %s, so nothing was updated." % (release["tag"], need))
    # The build reads the compose file, which needs the host's settings.
    for own in (".env", "compose.override.yml"):
        if os.path.exists(os.path.join(ROOT, own)):
            shutil.copy2(os.path.join(ROOT, own), os.path.join(stage, own))
    return archive, stage


def build_images(folder, log):
    """docker compose build in `folder`; True when it worked."""
    progress = proglib.Progress()
    progress.begin("Building the new server image", "a few minutes")
    code, tail = proglib.run_logged(["docker", "compose", "build"], log, cwd=folder, progress=progress)
    progress.end(code == 0)
    if code != 0:
        for line in tail[-8:]:
            say("  " + line)
    return code == 0


def restart(services, log):
    """Start the new image for the services that ran before, then check the server answers."""
    say("Restarting %s..." % ", ".join(services))
    code, tail = proglib.run_logged(["docker", "compose", "up", "-d", "--no-deps", "--no-build"] + services,
                                    log, cwd=ROOT)
    if code != 0:
        for line in tail[-8:]:
            say("  " + line)
        raise UpdateFailed("The new version is in place, but the server did not start again; see %s." % LOG)
    if "game" not in services:
        return
    say("Checking the server...")
    host = wait_ready(read_env(), hosts=CHECK_HOSTS)
    if not host:
        raise UpdateFailed("The new version started, but its register server does not answer; see %s." % LOG)
    if not patch_answers(SITE_PORT, host=host):
        raise UpdateFailed("The new version started, but its website does not answer the game's update check.")


def update_server(yes=False):
    """Bring the server folder to the newest release and restart what was running. Without `yes`
    it may ask. -> the one-line summary; raises UpdateFailed.

    The release is downloaded, unpacked and built in a temporary folder first; the server
    folder's code is replaced only once the new image is built, so a failed download or build
    leaves the old version running. A git checkout is pulled instead."""
    if not os.path.exists(os.path.join(ROOT, "compose.yml")):
        raise UpdateFailed("%s holds no server (no compose.yml), so there is nothing to update." % ROOT)
    say("Checking Docker...")
    state = docker_state()
    if state != "ok":
        raise UpdateFailed("Docker does not work here (%s), so nothing was updated." % state)
    work = None
    with log_file() as log:
        try:
            before = running_services()
            if os.path.exists(os.path.join(ROOT, ".git")):
                say("Pulling the newest commit with git...")
                head = run(["git", "rev-parse", "HEAD"], check=False, capture=True).stdout.strip()
                if run(["git", "pull", "--ff-only"], check=False, capture=True).returncode != 0:
                    raise UpdateFailed("git pull failed (local changes?), so nothing was updated.")
                new = run(["git", "rev-parse", "HEAD"], check=False, capture=True).stdout.strip()
                release = {"tag": "the newest commit", "commit": new}
                if head == new and (yes or not ask_yes("This is the newest commit. Rebuild anyway?", False)):
                    return "Already up to date (commit %s)." % new[:7]
                updates.write_revision(ROOT, new)
                stage = ROOT
            else:
                say("Asking GitHub for the newest release...")
                try:
                    release = latest_release()
                except updates.Failed as e:
                    raise UpdateFailed("Could not find the newest release (%s), so nothing was updated." % e)
                running = updates.read_revision(ROOT)[0]
                current = (same_commit(running, release["commit"]) if running
                           else installed_version() == release["tag"])
                if current:
                    if yes or not ask_yes("This is the newest version (%s). Rebuild anyway?" % release["tag"], False):
                        return "Already up to date (%s)." % release["tag"]
                    stage = ROOT
                else:
                    say("%s is out; this server runs %s." % (release["tag"], installed_version() or
                                                            (running[:7] if running else "an unknown version")))
                    if not yes and not ask_yes("Update now? Players are disconnected for a moment."):
                        return "Not updated."
                    work = tempfile.mkdtemp(prefix="golf-update-")
                    archive, stage = stage_release(release, work)
                    updates.write_revision(stage, release["commit"], tag=release["tag"])
            if not build_images(stage, log):
                raise UpdateFailed("Building the new version failed, so the old one keeps running; see %s." % LOG)
            if stage != ROOT:
                say("Putting the new files in place...")
                unpack_server(archive, ROOT, release["tag"])
                shutil.copy2(os.path.join(stage, updates.REVISION_FILE), os.path.join(ROOT, updates.REVISION_FILE))
            if before:
                restart(before, log)
                done = "restarted %s" % ", ".join(before)
            else:
                say("The server was stopped and stays stopped.")
                done = "the server stays stopped"
            return "Updated to %s (%s); %s." % (release["tag"], release["commit"][:7], done)
        except UpdateFailed as e:
            log.write("update failed: %s\n" % e)
            raise
        finally:
            if work:
                shutil.rmtree(work, ignore_errors=True)


def update():
    """The menu's Update: the same steps as --update, with questions."""
    try:
        say(update_server(yes=False))
    except UpdateFailed as e:
        say(str(e))
        return
    if FROZEN:
        say("A new Golf Server Setup may also be on %s" % RELEASES)


def update_command(yes):
    """python setup.py --update [--yes] -> the exit code; the last line printed is the summary."""
    try:
        say(update_server(yes=yes))
        return 0
    except UpdateFailed as e:
        say(str(e))
    except KeyboardInterrupt:
        say("The update was stopped.")
    except Exception as e:      # the last line must still be a sentence the panel can show
        say("The update failed unexpectedly (%s: %s)." % (e.__class__.__name__, e))
    return 1


def state_data():
    return os.path.join(ROOT, read_env().get("MINGOL_STATE") or "state", "data")


def backup():
    data = state_data()
    if not os.path.isdir(data):
        say("There is nothing to back up yet (%s is missing)." % data)
        return
    os.makedirs(BACKUPS, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    archive = shutil.make_archive(os.path.join(BACKUPS, "golf-%s" % stamp), "zip", data)
    say("Saved %s" % archive)
    say("Also keep a copy of %s somewhere safe." % ENV)


def restore():
    zips = sorted(f for f in os.listdir(BACKUPS) if f.endswith(".zip")) if os.path.isdir(BACKUPS) else []
    if not zips:
        say("No backups in %s." % BACKUPS)
        return
    for i, name in enumerate(zips, 1):
        say("  %d. %s" % (i, name))
    pick = ask("Restore which one (number, Enter to cancel)")
    if not pick.isdigit() or not 1 <= int(pick) <= len(zips):
        return
    say("This replaces every account, post and Japan Tour round with the ones in %s." % zips[int(pick) - 1])
    if not ask_yes("Go on?", False):
        return
    running = server_running()
    compose("stop", check=False)
    data = state_data()
    if os.path.isdir(data):
        shutil.rmtree(data)
    with zipfile.ZipFile(os.path.join(BACKUPS, zips[int(pick) - 1])) as z:
        z.extractall(data)
    say("Restored.")
    if running:
        compose("up", "-d")


def logs():
    say("Showing what the server is doing. Press Ctrl+C to go back to the menu.")
    try:
        compose("logs", "-f", "--tail", "200", "game", check=False)
    except KeyboardInterrupt:
        pass


def router_again():
    env, choices = read_env(), read_choices()
    if who_plays(env, choices) != "internet":
        say("Your server is set up for your home network only, so the router needs no open ports.")
        say("Choose Change settings, then Who will play, to change that.")
        return
    ok, router = open_router_ports(env)
    choices["router"] = "open" if ok else "closed"
    write_choices(choices)
    if not ok:
        show_port_help(env, router)


def tell_players():
    finish(read_env(), read_choices())


MENU = [("Start the server", start_server_menu),
        ("Stop the server", stop_server),
        ("Change settings", change_settings),
        ("Update", update),
        ("Make a backup", backup),
        ("Restore a backup", restore),
        ("Show what the server is doing (logs)", logs),
        ("Check that the server works", smoke),
        ("Open the router ports again", router_again),
        ("Show what to tell players", tell_players),
        ("Remove the server", uninstall)]


def menu():
    while True:
        env = read_env()
        heading("Golf server: %s (players' DNS: %s)" % ("running" if server_running() else "stopped",
                                                        env.get("MINGOL_PUBLIC_IP", "?")))
        for i, (title, _) in enumerate(MENU, 1):
            say("  %2d. %s" % (i, title))
        say("   0. Quit")
        pick = ask("Type a number", "0")
        if pick == "0":
            return
        if pick.isdigit() and 1 <= int(pick) <= len(MENU):
            try:
                MENU[int(pick) - 1][1]()
            except KeyboardInterrupt:
                say()
            except SystemExit as e:
                say(str(e))


def parse_args(argv=None):
    ap = argparse.ArgumentParser(prog="Golf Server Setup", description=(
        "Sets up and runs an Everybody's Golf Online server in Docker. The first run asks a few "
        "questions; later runs show a menu."))
    ap.add_argument("--folder", help="the server folder (default: the one this runs from, or "
                                     "'Golf Server' in your home folder for the setup program)")
    ap.add_argument("--dry-run", action="store_true",
                    help="ask the questions, then show what would be done; writes nothing and needs no Docker")
    ap.add_argument("--update", action="store_true",
                    help="update the server to the newest release, rebuild it and restart what was running")
    ap.add_argument("--yes", action="store_true", help="with --update: ask nothing (for a panel's deployer)")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    use_root(find_root(args.folder))
    if args.update:
        if os.path.isdir(ROOT):
            os.chdir(ROOT)
        return update_command(args.yes)
    try:
        if args.dry_run:
            say("Server folder: %s%s" % (ROOT, "" if os.path.exists(os.path.join(ROOT, "compose.yml")) else
                                         " (the server's files would be unpacked here)"))
            say("Docker: %s" % docker_state())
            first_run(dry_run=True)
            return
        if not ensure_server_files():
            return
        os.chdir(ROOT)
        wait_for_docker()
        if not os.path.exists(ENV):
            first_run()
            return 0
        resend_updater()
        menu()
    except KeyboardInterrupt:
        say()
        return 1
    return 0


if __name__ == "__main__":
    code = 1
    try:
        code = main()
    finally:
        if FROZEN and "--yes" not in sys.argv:
            # A double-clicked program's window closes when it ends: keep it open to be read.
            try:
                input("Press Enter to close this window.")
            except (EOFError, KeyboardInterrupt):
                pass
    sys.exit(code or 0)
