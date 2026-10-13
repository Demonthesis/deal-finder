#!/usr/bin/env bash
# Install or update Deal Finder on Debian (12 or 13), e.g. in a Proxmox LXC. Run as root
# from a checkout of this repository:   sudo deploy/install.sh
#
# Needs uv (https://docs.astral.sh/uv/) already installed; the script doesn't download it,
# and stops with instructions if it can't find it.
#
# BROWSER=1 deploy/install.sh   also installs headless Chromium, for sites with
#                               `fetcher: browser` in the config.
#
# Code goes to /opt/dealfinder, data and config to /var/lib/dealfinder (mount a persistent
# volume there), secrets to /etc/dealfinder/env. Re-running it updates the code and keeps
# data, config and secrets.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }
SRC="$(cd "$(dirname "$0")/.." && pwd)"
APP=/opt/dealfinder
DATA=/var/lib/dealfinder
ETC=/etc/dealfinder

apt-get update -qq
apt-get install -y -qq --no-install-recommends ca-certificates curl rsync tzdata >/dev/null

id dealfinder >/dev/null 2>&1 || useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin dealfinder
install -d -o dealfinder -g dealfinder -m 0750 "$DATA"
install -d -o root -g dealfinder -m 0750 "$ETC"

# Called by full path: /usr/local/bin may be missing from PATH (e.g. under pct exec).
UV="$(command -v uv || true)"
for candidate in /usr/local/bin/uv /root/.local/bin/uv /root/.cargo/bin/uv; do
    [[ -n "$UV" ]] && break
    [[ -x "$candidate" ]] && UV="$candidate"
done
if [[ -z "$UV" ]]; then
    cat >&2 <<'MSG'
uv is not installed. Install it yourself, then run this script again. For example, Astral's
installer (review it first; https://docs.astral.sh/uv/getting-started/installation/):
    curl -LsSf https://astral.sh/uv/install.sh -o /tmp/uv-install.sh
    UV_INSTALL_DIR=/usr/local/bin INSTALLER_NO_MODIFY_PATH=1 sh /tmp/uv-install.sh
MSG
    exit 1
fi
echo "Using uv at $UV ($("$UV" --version))"

rsync -a --delete --exclude .git --exclude .venv --exclude data --exclude bakeoff \
      --exclude python --exclude browsers --exclude "__pycache__" --exclude .pytest_cache \
      "$SRC/" "$APP/"
cd "$APP"
export UV_PYTHON_INSTALL_DIR="$APP/python"
EXTRAS=()
[[ "${BROWSER:-0}" == 1 ]] && EXTRAS=(--extra browser)
"$UV" sync --frozen --no-dev "${EXTRAS[@]}"
if [[ "${BROWSER:-0}" == 1 ]]; then
    PLAYWRIGHT_BROWSERS_PATH="$APP/browsers" "$APP/.venv/bin/playwright" install --with-deps chromium
fi
chown -R root:root "$APP"
chmod -R a+rX "$APP"

if [[ ! -f "$DATA/watchlists.yaml" ]]; then
    install -o dealfinder -g dealfinder -m 0640 "$SRC/config/watchlists.yaml" "$DATA/watchlists.yaml"
    echo "Copied the example config to $DATA/watchlists.yaml; edit it or use the web app."
fi
if [[ ! -f "$ETC/env" ]]; then
    install -o root -g dealfinder -m 0640 "$SRC/deploy/env.example" "$ETC/env"
    echo "Fill in $ETC/env (see the comments in it), then: systemctl restart dealfinder"
fi

runuser -u dealfinder -- "$APP/.venv/bin/dealfinder" validate-config -c "$DATA/watchlists.yaml"
runuser -u dealfinder -- "$APP/.venv/bin/dealfinder" init-db --db "$DATA/dealfinder.db" -c "$DATA/watchlists.yaml"

install -m 0644 "$SRC/deploy/dealfinder.service" /etc/systemd/system/dealfinder.service
systemctl daemon-reload
systemctl enable dealfinder >/dev/null
systemctl restart dealfinder
systemctl --no-pager --lines=5 status dealfinder || true
HOST="$(sed -n 's/^DEALFINDER_HOST=//p' "$ETC/env" | tail -n1)"
HOST="${HOST:-127.0.0.1}"
[[ "$HOST" == 0.0.0.0 ]] && HOST="$(hostname -I | awk '{print $1}')"
echo "Deal Finder is running: http://$HOST:8080"
