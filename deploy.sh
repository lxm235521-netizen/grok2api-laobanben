#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
PACKAGE_INSTALLER="$PROJECT_DIR/../install_grok2api.sh"

if [ -f "$PACKAGE_INSTALLER" ]; then
  exec bash "$PACKAGE_INSTALLER" "$@"
fi

if [ "${EUID:-$(id -u)}" -ne 0 ]; then
  echo "ERROR: Please run as root, or use sudo." >&2
  exit 1
fi

echo "Grok2API source-only deploy"
echo "Project directory: $PROJECT_DIR"
echo
echo "Tip: the recommended one-click installer is ../install_grok2api.sh."
echo "This fallback only starts the local source compose stack."
echo

if ! command -v docker >/dev/null 2>&1; then
  echo "Installing Docker..."
  curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
  sh /tmp/get-docker.sh
  rm -f /tmp/get-docker.sh
fi

if command -v systemctl >/dev/null 2>&1; then
  systemctl enable --now docker || true
else
  service docker start || true
fi

docker info >/dev/null 2>&1 || {
  echo "ERROR: Docker is not running." >&2
  exit 1
}

docker compose version >/dev/null 2>&1 || {
  echo "ERROR: Docker Compose plugin is unavailable." >&2
  exit 1
}

mkdir -p "$PROJECT_DIR/data" "$PROJECT_DIR/logs" "$PROJECT_DIR/data/tmp"

if [ ! -f "$PROJECT_DIR/data/config.toml" ]; then
  cp "$PROJECT_DIR/config.defaults.toml" "$PROJECT_DIR/data/config.toml"
fi

if [ ! -f "$PROJECT_DIR/data/token.json" ]; then
  printf '{}\n' > "$PROJECT_DIR/data/token.json"
fi

chmod 600 "$PROJECT_DIR/data/config.toml" "$PROJECT_DIR/data/token.json" || true

cd "$PROJECT_DIR"
docker compose up -d --build grok2api flaresolverr

echo
echo "Deployment started."
echo "Open /admin to configure tokens and proxy settings."
docker compose ps
