"""Build the Golf Server Setup program: python packaging/build_setup.py [--version v1.2.3]

One file made with PyInstaller (pip install pyinstaller==6.11.1), written to
dist/. It carries this repository's committed files as server.zip and unpacks
them into the host's server folder on its first run, so a host needs only the
program and Docker. Build each platform's program on that platform.

server.zip also carries server-revision.json, the commit it was made from and when,
which the Docker image copies in so the admin panel can tell whether a newer release
is out (mingol/updates.py).
"""
import argparse
import datetime
import json
import os
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAMES = {"win32": "Golf-Server-Setup", "darwin": "Golf-Server-Setup", "linux": "Golf-Server-Setup-linux-x86_64"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default="dev", help="the release tag the program reports and updates from")
    ap.add_argument("--commit", help="the commit server.zip is made from (default: git's HEAD)")
    args = ap.parse_args()
    work = os.path.join(HERE, "build", "setup")
    os.makedirs(work, exist_ok=True)
    archive = os.path.join(work, "server.zip")
    subprocess.run(["git", "-C", HERE, "archive", "--format=zip", "--prefix=golfforeverybody/", "-o", archive, "HEAD"],
                   check=True)
    commit = args.commit or subprocess.run(["git", "-C", HERE, "rev-parse", "HEAD"], check=True,
                                           stdout=subprocess.PIPE, text=True).stdout.strip()
    revision = {"commit": commit.lower(),
                "built_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    if args.version.startswith("v"):
        revision["tag"] = args.version
    with zipfile.ZipFile(archive, "a", zipfile.ZIP_DEFLATED) as z:
        z.writestr("golfforeverybody/server-revision.json", json.dumps(revision) + "\n")
    version = os.path.join(work, "server-version.txt")
    with open(version, "w", encoding="utf-8") as f:
        f.write(args.version + "\n")
    name = NAMES.get(sys.platform, NAMES["linux"])
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--console",
                    "--name", name, "--distpath", os.path.join(HERE, "dist"), "--workpath", os.path.join(work, "work"),
                    "--specpath", work, "--add-data", archive + os.pathsep + ".",
                    "--add-data", version + os.pathsep + ".", "--paths", HERE,
                    os.path.join(HERE, "setup.py")], check=True, cwd=HERE)
    print(os.path.join(HERE, "dist", name + (".exe" if sys.platform == "win32" else "")))


if __name__ == "__main__":
    main()
