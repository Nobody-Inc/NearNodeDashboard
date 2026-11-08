"""The dashboard's version: the latest git release tag (vX.Y.Z) plus the installed commit."""

import json
import os
import re
import subprocess

# The project root (the folder holding server.py, static/ and VERSION.json).
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# Fallback only: the real version comes from the latest git tag vX.Y.Z (see version_info()).
APP_VERSION = "1.0.0"

REPO_URL = "https://github.com/Nobody-Inc/NearNodeDashboard"

VERSION_TAG_RE = re.compile(r"^v([0-9][0-9A-Za-z.+-]{0,40})-(\d+)-g([0-9a-f]{4,40})$")

def version_info():
    """Version from the latest release tag (vX.Y.Z) the code descends from, plus the commit.

    `distance` is how many commits the code is past that tag (0 = exactly the release).
    install.sh writes VERSION.json (the install folder has no .git); a copy run straight from a git
    clone asks git. APP_VERSION is only used when there's no git or no release tag."""
    info = {"version": APP_VERSION, "distance": None, "tagged": False, "commit": None, "date": None,
            "modified": False, "repo": REPO_URL}
    try:
        with open(os.path.join(BASE_DIR, "VERSION.json")) as f:
            data = json.load(f)
        info.update({k: data.get(k) for k in ("commit", "date", "modified") if k in data})
        if isinstance(data.get("version"), str) and re.match(r"^[0-9][0-9A-Za-z.+-]{0,40}$", data["version"]):
            info.update(version=data["version"], distance=data.get("distance"), tagged=True)
        return info
    except (OSError, ValueError):
        pass

    def git(*args):
        out = subprocess.run(["git", "-C", BASE_DIR, *args], capture_output=True, text=True, timeout=5)
        return out.stdout.strip() if out.returncode == 0 else ""
    try:
        log = git("log", "-1", "--format=%h %cs")
        if log:
            info["commit"], info["date"] = log.split()[:2]
            info["modified"] = bool(git("status", "--porcelain", "--untracked-files=no"))
            m = VERSION_TAG_RE.match(git("describe", "--tags", "--long", "--match", "v[0-9]*"))
            if m:
                info.update(version=m.group(1), distance=int(m.group(2)), tagged=True)
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return info
