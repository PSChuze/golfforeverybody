"""Finding the other installs on this machine: the shared DNS, Feega, an OpenLobby panel.

They find each other through Docker labels and one shared Docker network, `openlobby`:

  org.openlobby.role=dns         the one DNS server every install shares (OpenLobby's image)
  org.openlobby.role=http        the one web server on port 80, forwarding each host name to its game
  org.openlobby.game.key=feega   the Feega sign-in service (the freega repository)
  org.openlobby.role=panel       an OpenLobby admin panel, with org.openlobby.panel.url

Every function takes `run`, which runs a docker command and returns (exit code, output); the
tests pass a fake one.
"""
import json
import os
import secrets
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import zipfile

NETWORK = "openlobby"
OPENLOBBY_IMAGE = "ghcr.io/prettyopenlobby/openlobby:latest"
DNS_IMAGE = HTTP_IMAGE = OPENLOBBY_IMAGE
FEEGA_REPO = "PSChuze/freega"
FEEGA_PROJECT = "freega"
FEEGA_NAMES = ("lib.feega.sonyfinance.co.jp",)
FEEGA_API = "http://feega:24030"

# OpenLobby's dns service on its own, for a machine with no OpenLobby (docs/installs.md there).
DNS_COMPOSE = """\
# The DNS server every game install on this machine shares (OpenLobby's dns service).
# Started by the first setup program that found none; the others only add their names.
name: openlobby-dns
services:
  dns:
    image: %(image)s
    command: ["dns"]
    restart: unless-stopped
    labels:
      org.openlobby.role: dns
    environment:
      POL_ADVERTISE: "%(address)s"
    ports:
      - "0.0.0.0:53:53/udp"
    volumes:
      - dns-data:/data
volumes:
  dns-data:
"""


# OpenLobby's http service on its own: port 80 shared by host name (docs/installs.md there).
HTTP_COMPOSE = """# The web server on port 80 every game install on this machine shares (OpenLobby's http service).
# Started by the first setup program that found none; the others only add their host names.
name: openlobby-http
services:
  http:
    image: %(image)s
    command: ["http"]
    restart: unless-stopped
    labels:
      org.openlobby.role: http
    ports:
      - "80:80"
    volumes:
      - http-data:/data
    networks:
      - openlobby
volumes:
  http-data:
networks:
  openlobby:
    external: true
"""


def run_docker(args, timeout=600, cwd=None):
    """Run `docker <args>` -> (exit code, its output)."""
    try:
        r = subprocess.run(["docker"] + list(args), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, encoding="utf-8", errors="replace", timeout=timeout, cwd=cwd)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)
    return r.returncode, r.stdout or ""


def running(label, run=run_docker):
    """Names of the running containers with this label (key=value)."""
    code, out = run(["ps", "--filter", "label=" + label, "--format", "{{.Names}}"])
    return [n.strip() for n in out.splitlines() if n.strip()] if code == 0 else []


def label_of(container, key, run=run_docker):
    code, out = run(["inspect", "--format", '{{index .Config.Labels "%s"}}' % key, container])
    value = out.strip() if code == 0 else ""
    return "" if value == "<no value>" else value


def ensure_network(run=run_docker):
    """Create the shared network when it is missing; True when it is there."""
    if run(["network", "inspect", NETWORK])[0] == 0:
        return True
    return run(["network", "create", NETWORK])[0] == 0


# --- the shared DNS ----------------------------------------------------------------------------

def find_dns(run=run_docker):
    names = running("org.openlobby.role=dns", run)
    return names[0] if names else None


def start_dns(folder, address, run=run_docker):
    """Start OpenLobby's dns service from `folder` (made if missing); -> its container or None."""
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "compose.yml")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(DNS_COMPOSE % {"image": DNS_IMAGE, "address": address})
    code, out = run(["compose", "-f", path, "up", "-d"], cwd=folder)
    if code != 0:
        return None
    for _ in range(20):
        name = find_dns(run)
        if name:
            return name
        time.sleep(1)
    return None


def dns_names(container, key, names, run=run_docker):
    """Answer `names` (and their subdomains) with this machine's address -> (ok, output)."""
    code, out = run(["exec", container, "python", "stub.py", "dns-names", "add", key] + list(names),
                    timeout=60)
    return code == 0, out.strip()


def dns_remove(container, key, run=run_docker):
    code, out = run(["exec", container, "python", "stub.py", "dns-names", "remove", key], timeout=60)
    return code == 0, out.strip()


# --- the shared web server on port 80 ------------------------------------------------------------

def find_http(run=run_docker):
    names = running("org.openlobby.role=http", run)
    return names[0] if names else None


def start_http(folder, run=run_docker):
    """Start OpenLobby's http service from `folder` (made if missing); -> its container or None."""
    if not ensure_network(run):
        return None
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "compose.yml")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(HTTP_COMPOSE % {"image": HTTP_IMAGE})
    code, out = run(["compose", "-f", path, "up", "-d"], cwd=folder)
    if code != 0:
        return None
    for _ in range(20):
        name = find_http(run)
        if name:
            return name
        time.sleep(1)
    return None


def http_hosts(container, key, upstream, hosts, run=run_docker):
    """Send these exact host names on port 80 to `upstream` (http://<alias>:<port>) -> (ok, output)."""
    code, out = run(["exec", container, "python", "stub.py", "http-hosts", "add", key, upstream] + list(hosts),
                    timeout=60)
    return code == 0, out.strip()


def http_remove(container, key, run=run_docker):
    code, out = run(["exec", container, "python", "stub.py", "http-hosts", "remove", key], timeout=60)
    return code == 0, out.strip()


# --- Feega ---------------------------------------------------------------------------------------

def find_feega(run=run_docker):
    names = running("org.openlobby.game.key=feega", run)
    return names[0] if names else None


def feega_source(tag="", fetch=urllib.request.urlopen):
    """The zip URL of a freega release: `tag`, else the newest one; None when GitHub cannot be asked."""
    if not tag:
        try:
            req = urllib.request.Request("https://api.github.com/repos/%s/releases/latest" % FEEGA_REPO,
                                         headers={"Accept": "application/vnd.github+json"})
            with fetch(req, timeout=10) as r:
                tag = json.load(r).get("tag_name") or ""
        except (OSError, ValueError):
            return None
    return "https://github.com/%s/archive/refs/tags/%s.zip" % (FEEGA_REPO, tag) if tag else None


def unpack_zip(archive, folder):
    """Unpack a source zip (one top folder, as GitHub makes them) into folder, keeping .env and state/."""
    with zipfile.ZipFile(archive) as z:
        names = [n for n in z.namelist() if not n.endswith("/")]
        tops = {n.split("/", 1)[0] for n in z.namelist()}
        strip = len(tops) == 1 and all("/" in n for n in names)
        for name in names:
            rel = name.split("/", 1)[1] if strip else name
            if not rel or rel.split("/", 1)[0] in (".env", "state"):
                continue
            target = os.path.normpath(os.path.join(folder, rel))
            if not target.startswith(os.path.normpath(folder) + os.sep):
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with z.open(name) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)


def feega_env(folder, address):
    """Write the freega folder's .env from its .env.example when there is none (new tokens)."""
    env = os.path.join(folder, ".env")
    if os.path.exists(env):
        return env
    values = {"FEEGA_ADMIN_TOKEN": secrets.token_hex(24), "FEEGA_ADVERTISE": address}
    lines = []
    example = os.path.join(folder, ".env.example")
    if os.path.exists(example):
        with open(example, encoding="utf-8") as f:
            for line in f.read().splitlines():
                key = line.lstrip("#").split("=", 1)[0].strip()
                if "=" in line and key in values:
                    line = "%s=%s" % (key, values.pop(key))
                lines.append(line)
    lines += ["%s=%s" % kv for kv in values.items()]
    with open(env, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    return env


def start_feega(folder, address, source, run=run_docker, fetch=urllib.request.urlretrieve, log=None):
    """Fetch freega into `folder` (unless it is there), start it as compose project "freega" and
    wait for its container. `source` is a zip URL or a folder with freega in it. -> (ok, message)."""
    os.makedirs(folder, exist_ok=True)
    if not os.path.exists(os.path.join(folder, "compose.yml")):
        if source and os.path.isdir(source):
            shutil.copytree(source, folder, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns(".git", ".env", "state"))
        elif source:
            archive = os.path.join(folder, "freega-download.zip")
            try:
                fetch(source, archive)
                unpack_zip(archive, folder)
            except (OSError, zipfile.BadZipFile) as e:
                return False, "Feega could not be downloaded (%s)." % e.__class__.__name__
            finally:
                if os.path.exists(archive):
                    os.remove(archive)
        else:
            return False, "Feega could not be downloaded: GitHub did not answer."
    feega_env(folder, address)
    if not ensure_network(run):
        return False, "The Docker network %s could not be made." % NETWORK
    code, out = run(["compose", "-p", FEEGA_PROJECT, "--env-file", ".env", "up", "-d", "--build"], cwd=folder)
    if log:
        log.write(out)
    if code != 0:
        return False, "Feega did not start: " + (out.strip().splitlines() or ["(no output)"])[-1]
    for _ in range(60):
        if find_feega(run):
            return True, "Feega is running."
        time.sleep(2)
    return False, "Feega started but its container is not running."


# --- an OpenLobby admin panel --------------------------------------------------------------------

def find_panel(run=run_docker):
    """The address of a running OpenLobby panel, or None."""
    for name in running("org.openlobby.role=panel", run):
        url = label_of(name, "org.openlobby.panel.url", run)
        if url:
            return url.rstrip("/")
    return None


def _post(url, doc, opener):
    req = urllib.request.Request(url, data=json.dumps(doc).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with opener(req, timeout=15) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}
    except (OSError, ValueError) as e:
        return 0, {"error": e.__class__.__name__}


def pair(panel_url, code, key, title, url, token, updater=None, opener=urllib.request.urlopen):
    """Pair with the panel by its one-time code -> (True, pairing secret, the panel's word on the
    updater) or (False, why, None). `updater` is {"folder", "command", "timeout_minutes"}: how the
    panel's deployer runs this game's update; the panel answers "requested" until its owner
    approves it."""
    doc = {"code": code.strip(), "key": key, "title": title, "url": url, "token": token}
    if updater:
        doc["updater"] = updater
    status, out = _post(panel_url + "/api/pair", doc, opener)
    if status == 200 and out.get("ok") and out.get("pairing"):
        return True, out["pairing"], out.get("updater")
    why = {403: "The code is wrong, used or older than 10 minutes.", 429: "Too many tries. Wait a minute.",
           0: "The panel did not answer."}.get(status, out.get("error") or "The panel said no (%d)." % status)
    return False, why, None


def pair_update(panel_url, key, pairing, token, url=None, updater=None, opener=urllib.request.urlopen):
    """Tell the panel the new admin token (and the updater, when given) -> (ok, why, the panel's
    word on the updater)."""
    doc = {"key": key, "pairing": pairing, "token": token}
    if url:
        doc["url"] = url
    if updater:
        doc["updater"] = updater
    status, out = _post(panel_url + "/api/pair/update", doc, opener)
    if status == 200 and out.get("ok"):
        return True, "", out.get("updater")
    return False, {403: "The panel no longer knows this pairing.", 404: "The panel has no such game.",
                   0: "The panel did not answer."}.get(status, out.get("error") or "HTTP %d" % status), None
