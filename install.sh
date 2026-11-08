#!/usr/bin/env bash
# Near Node Dashboard installer.
#
# Run on the Linux host that runs neard, from inside the cloned repository:
#   sudo ./install.sh                  install, or upgrade an existing install (keeps settings and data)
#   sudo ./install.sh --dry-run        print what it would do without changing anything
#   sudo ./install.sh --uninstall      remove the service (keeps files, data and the user)
#   sudo ./install.sh --uninstall --purge   also delete the install folder, its data and the user
#
# Options: --user NAME (default nearnode)  --dir PATH (default /opt/NearNodeDashboard)
#          --port N (default 8787)  --neard-unit NAME (default: detected, else neard.service)
#          --listen ADDR (default 0.0.0.0 = all IPv4; '::' = all IPv4+IPv6; 127.0.0.1 = this host only)
# Optional exporter (second service publishing the data for central scraping):
#          --with-exporter  install it (kept on later upgrades)   --without-exporter  remove it
#          --exporter-port N (default 9787)  --exporter-listen ADDR (default 0.0.0.0)
set -euo pipefail

SERVICE_NAME="near-node-dashboard"
SERVICE_USER="nearnode"
INSTALL_DIR="/opt/NearNodeDashboard"
PORT="8787"
LISTEN="0.0.0.0"
NEARD_UNIT=""
DRY_RUN=0
UNINSTALL=0
PURGE=0
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
UNIT_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
EXPORTER_NAME="near-node-exporter"
EXPORTER_UNIT="/etc/systemd/system/${EXPORTER_NAME}.service"
EXPORTER="auto"          # auto = keep it if it's already installed
EXPORTER_PORT="9787"
EXPORTER_LISTEN="0.0.0.0"

say()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33mWARNING:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }
# Run a command, or just print it in --dry-run mode.
run()  { if [ "$DRY_RUN" = 1 ]; then printf '    [dry-run] %s\n' "$*"; else "$@"; fi; }

usage() { sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --user)       SERVICE_USER="${2:?--user needs a name}"; shift 2 ;;
    --dir)        INSTALL_DIR="${2:?--dir needs a path}"; shift 2 ;;
    --port)       PORT="${2:?--port needs a number}"; shift 2 ;;
    --listen)     LISTEN="${2:?--listen needs an IP address}"; shift 2 ;;
    --neard-unit) NEARD_UNIT="${2:?--neard-unit needs a unit name}"; shift 2 ;;
    --with-exporter)    EXPORTER=yes; shift ;;
    --without-exporter) EXPORTER=no; shift ;;
    --exporter-port)    EXPORTER_PORT="${2:?--exporter-port needs a number}"; shift 2 ;;
    --exporter-listen)  EXPORTER_LISTEN="${2:?--exporter-listen needs an IP address}"; shift 2 ;;
    --dry-run)    DRY_RUN=1; shift ;;
    --uninstall)  UNINSTALL=1; shift ;;
    --purge)      PURGE=1; shift ;;
    -h|--help)    usage 0 ;;
    *)            warn "unknown option: $1"; usage 1 ;;
  esac
done

# ---------------------------------------------------------------- validate inputs
[[ "$SERVICE_USER" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || die "invalid user name: $SERVICE_USER"
[[ "$PORT" =~ ^[0-9]+$ ]] && [ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ] || die "invalid port: $PORT"
[[ "$LISTEN" =~ ^[0-9A-Fa-f.:]+$ ]] || die "--listen must be an IP address (e.g. 0.0.0.0, ::, 127.0.0.1)"
[[ "$EXPORTER_PORT" =~ ^[0-9]+$ ]] && [ "$EXPORTER_PORT" -ge 1 ] && [ "$EXPORTER_PORT" -le 65535 ] || \
  die "invalid exporter port: $EXPORTER_PORT"
[ "$EXPORTER_PORT" != "$PORT" ] || die "the exporter port must differ from the dashboard port ($PORT)"
[[ "$EXPORTER_LISTEN" =~ ^[0-9A-Fa-f.:]+$ ]] || die "--exporter-listen must be an IP address"
case "$INSTALL_DIR" in /*) ;; *) die "--dir must be an absolute path" ;; esac
# Normalise before the safety check, so "/opt/" or "//opt" can't slip past it (and later be
# chown'd or, with --purge, deleted).
INSTALL_DIR="$(printf '%s' "$INSTALL_DIR" | tr -s '/')"   # squeeze repeated slashes (portable across bash versions)
[ "$INSTALL_DIR" != "/" ] && INSTALL_DIR="${INSTALL_DIR%/}"
case "$INSTALL_DIR" in *"/."*|*"/.."*) die "--dir must not contain . or .. components" ;; esac
[ "$(printf '%s' "$INSTALL_DIR" | tr -cd '/' | wc -c)" -ge 2 ] || \
  die "refusing to use $INSTALL_DIR: pick a dedicated folder at least two levels deep, e.g. /opt/NearNodeDashboard"

if [ "$DRY_RUN" = 0 ]; then
  [ "$(uname -s)" = "Linux" ] || die "this installer is for Linux (found $(uname -s))"
  [ "$(id -u)" = 0 ] || die "run it with sudo: sudo $0 $*"
  command -v systemctl >/dev/null || die "systemd (systemctl) is required"
else
  say "Dry run: nothing will be changed"
fi

# ---------------------------------------------------------------- uninstall
if [ "$UNINSTALL" = 1 ]; then
  if [ -f "$EXPORTER_UNIT" ] || [ "$DRY_RUN" = 1 ]; then
    say "Removing the ${EXPORTER_NAME} service"
    run systemctl disable --now "$EXPORTER_NAME" || true
    run rm -f "$EXPORTER_UNIT"
  fi
  say "Removing the ${SERVICE_NAME} service"
  if [ -f "$UNIT_FILE" ] || [ "$DRY_RUN" = 1 ]; then
    run systemctl disable --now "$SERVICE_NAME" || true
    run rm -f "$UNIT_FILE"
    run systemctl daemon-reload
  else
    say "No service file at $UNIT_FILE; skipping"
  fi
  if [ "$PURGE" = 1 ]; then
    say "Purging $INSTALL_DIR and user $SERVICE_USER"
    if [ -d "$INSTALL_DIR" ] && [ ! -f "$INSTALL_DIR/server.py" ]; then
      die "$INSTALL_DIR doesn't look like a Near Node Dashboard install (no server.py); not deleting it"
    fi
    run rm -rf "$INSTALL_DIR"
    if id "$SERVICE_USER" >/dev/null 2>&1; then run userdel "$SERVICE_USER" || true; fi
  else
    say "Kept $INSTALL_DIR (settings and data) and user $SERVICE_USER. Add --purge to delete them."
  fi
  say "Done"
  exit 0
fi

# ---------------------------------------------------------------- prerequisites
[ -f "$SRC_DIR/server.py" ] && [ -d "$SRC_DIR/static" ] || \
  die "run this from the cloned repository (server.py and static/ not found next to install.sh)"

PYTHON="$(command -v python3 || true)"
if [ -z "$PYTHON" ] || ! "$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then
  die "Python 3.9 or newer is required (found: ${PYTHON:-none}${PYTHON:+, $("$PYTHON" -V 2>&1)})"
fi
say "Using $("$PYTHON" -V 2>&1) at $PYTHON"

# `python3 -m venv` needs ensurepip, which Debian/Ubuntu ship separately as python3-venv.
if ! "$PYTHON" -c 'import ensurepip, venv' 2>/dev/null; then
  say "Installing the Python venv package"
  if command -v apt-get >/dev/null; then
    run apt-get update -qq
    run env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-venv
  elif command -v dnf >/dev/null; then
    run dnf install -y -q python3-pip
  elif command -v yum >/dev/null; then
    run yum install -y -q python3-pip
  else
    die "install the Python venv module for $PYTHON (e.g. the python3-venv package) and re-run"
  fi
fi

# ---------------------------------------------------------------- detect neard
if [ -z "$NEARD_UNIT" ]; then
  # The unit that owns the running neard process, if any.
  NEARD_PID="$(pgrep -xo neard 2>/dev/null || true)"
  if [ -n "$NEARD_PID" ]; then
    NEARD_UNIT="$(ps -o unit= -p "$NEARD_PID" 2>/dev/null | tr -d ' ' || true)"
  fi
  case "$NEARD_UNIT" in *.service) ;; *)
    NEARD_UNIT="$(systemctl list-unit-files --type=service --no-legend 2>/dev/null | awk '{print $1}' | grep -m1 -E '^neard[^ ]*\.service$' || true)" ;;
  esac
  if [ -n "$NEARD_UNIT" ]; then
    say "Detected neard service: $NEARD_UNIT"
  else
    NEARD_UNIT="neard.service"
    warn "couldn't find neard's systemd service; assuming $NEARD_UNIT (use --neard-unit to change)"
  fi
fi
case "$NEARD_UNIT" in *.service) ;; *) NEARD_UNIT="${NEARD_UNIT}.service" ;; esac
[[ "$NEARD_UNIT" =~ ^[A-Za-z0-9@_.:-]+\.service$ ]] || die "invalid neard unit name: $NEARD_UNIT"

# ---------------------------------------------------------------- service user
NOLOGIN="$(command -v nologin || echo /usr/sbin/nologin)"
if id "$SERVICE_USER" >/dev/null 2>&1; then
  say "User $SERVICE_USER already exists"
else
  say "Creating system user $SERVICE_USER (no login shell)"
  run useradd --system --user-group --home-dir "$INSTALL_DIR" --no-create-home \
      --shell "$NOLOGIN" --comment "Near Node Dashboard" "$SERVICE_USER"
fi
# Read neard's journal. Only systemd-journal: the broader `adm` group would let the service read
# every log in /var/log (auth logs included), which it doesn't need.
if getent group systemd-journal >/dev/null 2>&1; then run usermod -aG systemd-journal "$SERVICE_USER"; fi
# Older versions of this installer also added `adm`; take it away again.
if id -nG "$SERVICE_USER" 2>/dev/null | tr ' ' '\n' | grep -qx adm; then
  say "Removing $SERVICE_USER from the adm group (no longer needed)"
  run gpasswd -d "$SERVICE_USER" adm
fi

# ---------------------------------------------------------------- files
# Layout and ownership (so the service user can never get root through an upgrade):
#   $INSTALL_DIR, code, .venv   root:$SERVICE_USER, not writable by the service
#   $INSTALL_DIR/data           $SERVICE_USER only: database and config.json (Settings saves it)
# Root never runs anything from the old install and never writes inside data/ itself;
# writes there are done as the service user, so a planted symlink can't redirect them.
DATA_DIR="$INSTALL_DIR/data"
CONFIG="$DATA_DIR/config.json"
command -v runuser >/dev/null || [ "$DRY_RUN" = 1 ] || die "runuser (util-linux) is required"
as_svc() { run runuser -u "$SERVICE_USER" -- "$@"; }

if [ "$SRC_DIR" = "$(cd "$INSTALL_DIR" 2>/dev/null && pwd -P || echo "$INSTALL_DIR")" ]; then
  die "clone the repository somewhere else (e.g. ~/near-node-dashboard); the installer copies it into $INSTALL_DIR"
fi
[ -L "$INSTALL_DIR" ] && die "$INSTALL_DIR is a symlink; refusing to install there"
[ -L "$DATA_DIR" ] && die "$DATA_DIR is a symlink; refusing to use it"

UPGRADE=0
[ -f "$INSTALL_DIR/server.py" ] && UPGRADE=1
if [ "$UPGRADE" = 1 ] && [ "$DRY_RUN" = 0 ] && systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
  say "Stopping the running service for the upgrade"
  systemctl stop "$SERVICE_NAME"
fi

# Lock the install folder first, so nothing inside it can be swapped while we work.
run mkdir -p "$INSTALL_DIR"
run chown root:root "$INSTALL_DIR"
run chmod 755 "$INSTALL_DIR"
if [ ! -d "$DATA_DIR" ]; then
  run install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 750 "$DATA_DIR"
fi

# Settings used to live in $INSTALL_DIR/config.json; move them into data/ (copied as the
# service user, so it can only ever copy a file it could already read).
if [ -f "$INSTALL_DIR/config.json" ] && [ ! -e "$CONFIG" ]; then
  say "Moving settings to $CONFIG"
  as_svc cp "$INSTALL_DIR/config.json" "$CONFIG"
fi

# Replace everything except data/ with a fresh copy. rm -rf doesn't follow symlinks.
say "Installing files into $INSTALL_DIR"
if [ "$DRY_RUN" = 1 ]; then
  run "find $INSTALL_DIR -mindepth 1 -maxdepth 1 ! -name data -exec rm -rf -- {} +"
  run "tar -C $SRC_DIR --exclude=./.venv --exclude=./data --exclude=__pycache__ --exclude=./config.json --exclude=./.git --exclude=./docs -cf - . | tar -C $INSTALL_DIR --no-same-owner -xf -"
else
  find "$INSTALL_DIR" -mindepth 1 -maxdepth 1 ! -name data -exec rm -rf -- {} +
  tar -C "$SRC_DIR" --exclude=./.venv --exclude=./data --exclude=__pycache__ \
      --exclude=./config.json --exclude=./.git --exclude=./docs -cf - . | tar -C "$INSTALL_DIR" --no-same-owner -xf -
fi

# Record which commit this install came from (the install folder has no .git); the page footer
# shows it so an out-of-date server is easy to spot.
if command -v git >/dev/null && git -c safe.directory="$SRC_DIR" -C "$SRC_DIR" rev-parse --git-dir >/dev/null 2>&1; then
  G="git -c safe.directory=$SRC_DIR -C $SRC_DIR"
  COMMIT="$($G log -1 --format=%h)"; CDATE="$($G log -1 --format=%cs)"
  MODIFIED=false; [ -n "$($G status --porcelain --untracked-files=no)" ] && MODIFIED=true
  say "Installing commit $COMMIT ($CDATE)$([ "$MODIFIED" = true ] && echo ' with local changes')"
  if [ "$DRY_RUN" = 1 ]; then
    run "write $INSTALL_DIR/VERSION.json (commit $COMMIT, $CDATE)"
  else
    printf '{"commit": "%s", "date": "%s", "modified": %s}\n' "$COMMIT" "$CDATE" "$MODIFIED" > "$INSTALL_DIR/VERSION.json"
  fi
else
  warn "not installing from a git clone; the footer won't show which commit is installed"
fi

say "Setting up the Python environment"
run "$PYTHON" -m venv "$INSTALL_DIR/.venv"   # always fresh: built from the system Python
run "$INSTALL_DIR/.venv/bin/pip" install --quiet --disable-pip-version-check --upgrade pip
run "$INSTALL_DIR/.venv/bin/pip" install --quiet --disable-pip-version-check -r "$INSTALL_DIR/requirements.txt"

# Code and virtualenv: owned by root, readable (not writable) by the service user.
# chown -h changes symlinks themselves (e.g. .venv/bin/python), never the system files they point to;
# chmod -R skips symlinks.
if [ "$DRY_RUN" = 1 ]; then
  run "find $INSTALL_DIR -mindepth 1 -maxdepth 1 ! -name data -exec chown -R -h root:$SERVICE_USER {} + -exec chmod -R u=rwX,g=rX,o= {} +"
else
  find "$INSTALL_DIR" -mindepth 1 -maxdepth 1 ! -name data \
      -exec chown -R -h "root:$SERVICE_USER" {} + -exec chmod -R u=rwX,g=rX,o= {} +
fi
run chown "root:$SERVICE_USER" "$INSTALL_DIR"
run chmod 750 "$INSTALL_DIR"

if [ ! -e "$CONFIG" ]; then
  # New install: default settings, with the log reader pointed at the detected neard unit.
  # Other defaults (RPC at 127.0.0.1:3030, 60s sampling, 24h retention) suit a dashboard on the node.
  say "Writing default settings to $CONFIG"
  UNIT_BASE="${NEARD_UNIT%.service}"
  if [ "$DRY_RUN" = 1 ]; then
    run "write defaults (log_unit=$UNIT_BASE) to $CONFIG as $SERVICE_USER"
  else
    runuser -u "$SERVICE_USER" -- "$INSTALL_DIR/.venv/bin/python" - "$CONFIG" "$UNIT_BASE" <<'PY'
import json, sys
path, unit = sys.argv[1], sys.argv[2]
with open(path, "x") as f:          # "x": never follow or overwrite an existing path
    json.dump({"log_unit": unit}, f, indent=2)
PY
  fi
else
  say "Keeping existing settings in $CONFIG"
fi

# ---------------------------------------------------------------- systemd unit
say "Writing $UNIT_FILE"
UNIT_CONTENT="[Unit]
Description=Near Node Dashboard
Documentation=file://$INSTALL_DIR/README.md
# Start after neard so the startup host checks see it running (they also wait up to 2 minutes).
After=network-online.target $NEARD_UNIT
Wants=network-online.target

[Service]
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/.venv/bin/python server.py --host $LISTEN --port $PORT --config $CONFIG
Restart=always
RestartSec=5
# Hardening: the dashboard can only write to its data folder.
ProtectSystem=full
ProtectHome=read-only
PrivateTmp=true
ReadWritePaths=$DATA_DIR

[Install]
WantedBy=multi-user.target
"
if [ "$DRY_RUN" = 1 ]; then
  printf '%s' "$UNIT_CONTENT" | sed 's/^/    [dry-run unit] /'
else
  printf '%s' "$UNIT_CONTENT" > "$UNIT_FILE"
  chmod 644 "$UNIT_FILE"
fi

run systemctl daemon-reload
# Starts the service (an upgrade stopped it above, so this also brings up the new version).
run systemctl enable --now "$SERVICE_NAME"

# ---------------------------------------------------------------- verify
if [ "$DRY_RUN" = 0 ]; then
  say "Waiting for the dashboard to answer on 127.0.0.1:$PORT"
  ok=0
  for _ in $(seq 1 20); do
    if "$PYTHON" - "$PORT" <<'PY' 2>/dev/null
import sys, urllib.request
urllib.request.urlopen(f"http://127.0.0.1:{sys.argv[1]}/api/config", timeout=2).read()
PY
    then ok=1; break; fi
    sleep 1
  done
  if [ "$ok" = 1 ]; then
    say "Near Node Dashboard is running (service: $SERVICE_NAME, user: $SERVICE_USER)"
  else
    warn "the service didn't answer yet. Check: systemctl status $SERVICE_NAME; journalctl -u $SERVICE_NAME -n 50"
    exit 1
  fi
fi

# ---------------------------------------------------------------- optional exporter
if [ "$EXPORTER" = "auto" ]; then
  if [ -f "$EXPORTER_UNIT" ]; then EXPORTER=yes; else EXPORTER=no; fi
fi
if [ "$EXPORTER" = yes ]; then
  case "$LISTEN" in
    0.0.0.0|::|127.0.0.1) DASH_URL="http://127.0.0.1:$PORT" ;;
    ::1)                  DASH_URL="http://[::1]:$PORT" ;;
    *:*)                  DASH_URL="http://[$LISTEN]:$PORT" ;;
    *)                    DASH_URL="http://$LISTEN:$PORT" ;;
  esac
  TOKEN_FILE="$DATA_DIR/exporter.token"
  if [ ! -e "$TOKEN_FILE" ]; then
    say "Generating an access token for the exporter"
    if [ "$DRY_RUN" = 1 ]; then
      run "create $TOKEN_FILE (random token, mode 600) as $SERVICE_USER"
    else
      # Written by the service user (never root inside data/), readable only by it.
      runuser -u "$SERVICE_USER" -- "$PYTHON" - "$TOKEN_FILE" <<'PY'
import os, secrets, sys
fd = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as f:
    f.write(secrets.token_urlsafe(32) + "\n")
PY
    fi
  fi
  say "Writing $EXPORTER_UNIT"
  EXPORTER_CONTENT="[Unit]
Description=Near Node Dashboard exporter (publishes the dashboard's data for central scraping)
Documentation=file://$INSTALL_DIR/README.md
After=network-online.target ${SERVICE_NAME}.service
Wants=${SERVICE_NAME}.service

[Service]
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=$INSTALL_DIR/.venv/bin/python exporter.py --dashboard $DASH_URL --host $EXPORTER_LISTEN --port $EXPORTER_PORT --token-file $TOKEN_FILE
Restart=always
RestartSec=5
# Hardening: the exporter only reads; it can't write anywhere.
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=true

[Install]
WantedBy=multi-user.target
"
  if [ "$DRY_RUN" = 1 ]; then
    printf '%s' "$EXPORTER_CONTENT" | sed 's/^/    [dry-run unit] /'
  else
    printf '%s' "$EXPORTER_CONTENT" > "$EXPORTER_UNIT"
    chmod 644 "$EXPORTER_UNIT"
  fi
  run systemctl daemon-reload
  run systemctl enable "$EXPORTER_NAME"
  run systemctl restart "$EXPORTER_NAME"
  if [ "$DRY_RUN" = 0 ]; then
    ok=0
    for _ in $(seq 1 15); do
      if "$PYTHON" - "$EXPORTER_PORT" <<'PY' 2>/dev/null
import sys, urllib.request, urllib.error
try:
    urllib.request.urlopen(f"http://127.0.0.1:{sys.argv[1]}/healthz", timeout=2)
except urllib.error.HTTPError:
    pass   # 503 = running, dashboard not read yet; still answering
PY
      then ok=1; break; fi
      sleep 1
    done
    if [ "$ok" = 1 ]; then say "Exporter is running (service: $EXPORTER_NAME, port $EXPORTER_PORT)"
    else warn "the exporter didn't answer yet. Check: journalctl -u $EXPORTER_NAME -n 50"; fi
  fi
elif [ -f "$EXPORTER_UNIT" ]; then
  say "Removing the ${EXPORTER_NAME} service (--without-exporter)"
  run systemctl disable --now "$EXPORTER_NAME" || true
  run rm -f "$EXPORTER_UNIT"
  run systemctl daemon-reload
fi

HOST_HINT="$(hostname -f 2>/dev/null || hostname)"
echo
case "$LISTEN" in
  127.0.0.1|::1)
    echo "To view it from your computer (it only listens on this host):"
    echo "    ssh -L $PORT:127.0.0.1:$PORT <you>@$HOST_HINT, then open http://localhost:$PORT" ;;
  *)
    echo "Open it in a browser at:"
    if [ "$LISTEN" = "0.0.0.0" ] || [ "$LISTEN" = "::" ]; then
      for ip in $(hostname -I 2>/dev/null); do
        case "$ip" in *:*) [ "$LISTEN" = "::" ] && echo "    http://[$ip]:$PORT" ;; *) echo "    http://$ip:$PORT" ;; esac
      done
    else
      case "$LISTEN" in *:*) echo "    http://[$LISTEN]:$PORT" ;; *) echo "    http://$LISTEN:$PORT" ;; esac
    fi
    echo
    echo "The dashboard has no login. Anyone who can reach port $PORT can view it and change its settings."
    echo "Allow only your own IP, e.g. with ufw:"
    echo "    sudo ufw allow from <your-ip> to any port $PORT proto tcp" ;;
esac
cat <<EOF

Manage it:
    systemctl status $SERVICE_NAME
    journalctl -u $SERVICE_NAME -f
    sudo systemctl restart $SERVICE_NAME
To upgrade: git pull in $SRC_DIR, then run this installer again (settings and data are kept).
EOF
if [ "$EXPORTER" = yes ]; then
  echo
  echo "Exporter (for central scraping) on port $EXPORTER_PORT:"
  for ip in $(hostname -I 2>/dev/null | tr ' ' '\n' | grep -v ':'); do echo "    http://$ip:$EXPORTER_PORT/   (status page)"; done
  echo "    /metrics = Prometheus format, /api/v1/snapshot = JSON, /healthz = health check"
  if [ "$DRY_RUN" = 0 ]; then
    echo "  Access token (send as 'Authorization: Bearer <token>', or ?token=<token> in a browser):"
    echo "    $(cat "$TOKEN_FILE")"
  fi
  echo "  Show it again later: sudo cat $DATA_DIR/exporter.token"
fi
