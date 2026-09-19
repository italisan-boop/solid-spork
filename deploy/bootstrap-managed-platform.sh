#!/usr/bin/env bash
set -euo pipefail

readonly RELEASE_ROOT=/opt/bookapp/releases/current
readonly ETC_ROOT=/etc/bookapp
readonly KEY_ROOT=/etc/bookapp/keys
readonly STATE_ROOT=/var/lib/bookapp
readonly SYSTEMD_ROOT=/etc/systemd/system
readonly CADDY_IMPORT=/etc/caddy/bookapp-tenants.import
readonly CADDY_ROUTE_ROOT=/etc/caddy/bookapp-tenants
readonly SOURCE_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
readonly PYTHON_BIN="${BOOKAPP_PYTHON_BIN:-python3.12}"

fail() {
  printf '%s\n' "$*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "required command is unavailable: $1"
}

ensure_group() {
  local name="$1"
  getent group "$name" >/dev/null 2>&1 || groupadd --system "$name"
}

ensure_user() {
  local name="$1"
  local group="$2"
  if ! getent passwd "$name" >/dev/null 2>&1; then
    useradd --system --gid "$group" --no-create-home --shell /usr/sbin/nologin "$name"
  fi
  [ "$(id -gn "$name")" = "$group" ] || fail "existing user has unexpected group: $name"
}

install_if_identical_or_missing() {
  local source="$1"
  local target="$2"
  if [ -e "$target" ]; then
    cmp -s "$source" "$target" || fail "refusing to overwrite: $target"
    return
  fi
  install -o root -g root -m 0644 "$source" "$target"
}

ensure_release_virtualenv() {
  local virtualenv="$RELEASE_ROOT/.venv"
  local lock_file="$RELEASE_ROOT/requirements-linux.txt"
  local lock_marker="$virtualenv/.bookapp-requirements.sha256"
  local lock_digest
  [ -f "$lock_file" ] || fail "managed dependency lock is unavailable"
  [ ! -L "$virtualenv" ] || fail "managed release virtualenv is invalid"
  require_command "$PYTHON_BIN"
  require_command sha256sum
  lock_digest="$(sha256sum "$lock_file" | cut -d' ' -f1)"
  if [ ! -x "$virtualenv/bin/python" ]; then
    "$PYTHON_BIN" -m venv --clear "$virtualenv"
  fi
  if [ ! -f "$lock_marker" ] || [ "$(cat "$lock_marker")" != "$lock_digest" ]; then
    "$virtualenv/bin/python" -m pip install --require-hashes -r "$lock_file"
    umask 077
    printf '%s\n' "$lock_digest" >"$lock_marker"
    chmod 0400 "$lock_marker"
  fi
}

write_sealer_environment() {
  local target="$ETC_ROOT/sealer.env"
  if [ -e "$target" ] || [ -L "$target" ]; then
    return
  fi
  umask 077
  cat >"$target" <<EOF
PLATFORM_SEALER_SOCKET=/run/bookapp-sealer/sealer.sock
PLATFORM_SEALER_KEK_FILE=$KEY_ROOT/controller-kek
PLATFORM_SEALER_KEY_VERSION=v1
PLATFORM_SEALER_ALLOWED_UID=$(id -u platform-console)
PLATFORM_SEALER_ALLOWED_GID=$(getent group platform-control | cut -d: -f3)
EOF
  chmod 0400 "$target"
}

write_host_operations_environment() {
  local target="$ETC_ROOT/host-operations.env"
  if [ -e "$target" ] || [ -L "$target" ]; then
    return
  fi
  umask 077
  cat >"$target" <<EOF
PLATFORM_HOST_OPERATIONS_SOCKET=/run/bookapp-host-operations/host-operations.sock
PLATFORM_HOST_OPERATIONS_ALLOWED_UID=$(id -u platform-controller)
PLATFORM_HOST_OPERATIONS_ALLOWED_GID=$(getent group platform-control | cut -d: -f3)
PLATFORM_HOST_OPERATIONS_RELEASE_ROOT=$RELEASE_ROOT
PLATFORM_HOST_OPERATIONS_CREDENTIAL_ROOT=$STATE_ROOT/tenant-credentials
PLATFORM_HOST_OPERATIONS_RUNTIME_ROOT=$STATE_ROOT/tenant-runtime
PLATFORM_HOST_OPERATIONS_TENANT_DATA_ROOT=$STATE_ROOT/tenants
PLATFORM_HOST_OPERATIONS_TENANT_BACKUP_ROOT=$STATE_ROOT/backups
PLATFORM_HOST_OPERATIONS_MANIFEST_PUBLIC_KEY_FILE=$STATE_ROOT/manifest-public.key
PLATFORM_HOST_OPERATIONS_UNIT_ROOT=$SYSTEMD_ROOT
PLATFORM_HOST_OPERATIONS_CADDY_ROUTE_ROOT=$CADDY_ROUTE_ROOT
PLATFORM_HOST_OPERATIONS_CADDY_CONFIG=/etc/caddy/Caddyfile
PLATFORM_HOST_OPERATIONS_CADDY_USER=caddy
EOF
  chmod 0400 "$target"
}

write_controller_environment() {
  local target="$ETC_ROOT/controller.env"
  if [ -e "$target" ] || [ -L "$target" ]; then
    return
  fi
  umask 077
  cat >"$target" <<EOF
PLATFORM_CONTROLLER_DATABASE_PATH=$STATE_ROOT/control/control.sqlite
PLATFORM_CONTROLLER_RELEASE_ROOT=$RELEASE_ROOT
PLATFORM_CONTROLLER_CREDENTIAL_ROOT=$STATE_ROOT/tenant-credentials
PLATFORM_CONTROLLER_RUNTIME_ROOT=$STATE_ROOT/tenant-runtime
PLATFORM_CONTROLLER_TENANT_DATA_ROOT=$STATE_ROOT/tenants
PLATFORM_CONTROLLER_TENANT_BACKUP_ROOT=$STATE_ROOT/backups
PLATFORM_CONTROLLER_MANIFEST_PUBLIC_KEY_FILE=$STATE_ROOT/manifest-public.key
PLATFORM_CONTROLLER_KEK_VERSION=v1
PLATFORM_CONTROLLER_HOST_OPERATIONS_SOCKET=/run/bookapp-host-operations/host-operations.sock
PLATFORM_CONTROLLER_LOCK_FILE=$STATE_ROOT/control/controller.lock
PLATFORM_CONTROLLER_POLL_SECONDS=5
EOF
  chmod 0400 "$target"
}

create_controller_keys() {
  local kek="$KEY_ROOT/controller-kek"
  local signing="$KEY_ROOT/controller-manifest-signing.key"
  if [ -e "$kek" ] || [ -e "$signing" ]; then
    [ -f "$kek" ] && [ -f "$signing" ] || fail "controller key set is incomplete"
    return
  fi
  umask 077
  "$RELEASE_ROOT/.venv/bin/python" - "$kek" "$signing" <<'PY'
import base64
import os
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def write_secret(path: Path, value: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    with os.fdopen(descriptor, "wb") as output:
        output.write(value)


kek_path = Path(sys.argv[1])
signing_path = Path(sys.argv[2])
write_secret(kek_path, base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=") + b"\n")
private_key = Ed25519PrivateKey.generate().private_bytes(
    serialization.Encoding.Raw,
    serialization.PrivateFormat.Raw,
    serialization.NoEncryption(),
)
write_secret(signing_path, base64.urlsafe_b64encode(private_key).rstrip(b"=") + b"\n")
PY
}

[ "${EUID}" -eq 0 ] || fail "run as root"
for command in caddy chown chmod cmp curl cut getent groupadd install setfacl systemctl useradd; do
  require_command "$command"
done
getent passwd caddy >/dev/null 2>&1 || fail "required Caddy user is unavailable: caddy"
ensure_release_virtualenv

ensure_group platform-control
ensure_group platform-console
ensure_group platform-bot
ensure_user platform-console platform-console
ensure_user platform-bot platform-bot
ensure_user platform-controller platform-control

install -d -o root -g root -m 0750 "$ETC_ROOT" "$KEY_ROOT"
install -d -o root -g root -m 0711 "$STATE_ROOT"
install -d -o platform-console -g platform-control -m 2770 "$STATE_ROOT/control"
install -d -o root -g root -m 0700 \
  "$STATE_ROOT/tenants" \
  "$STATE_ROOT/backups" \
  "$STATE_ROOT/tenant-credentials" \
  "$STATE_ROOT/tenant-runtime"
install -d -o root -g root -m 0755 "$CADDY_ROUTE_ROOT"
if [ ! -e "$CADDY_ROUTE_ROOT/00-placeholder.caddy" ]; then
  printf '%s\n' '# managed tenant routes are generated by bookapp-host-operations' \
    >"$CADDY_ROUTE_ROOT/00-placeholder.caddy"
  chown root:root "$CADDY_ROUTE_ROOT/00-placeholder.caddy"
  chmod 0644 "$CADDY_ROUTE_ROOT/00-placeholder.caddy"
fi

create_controller_keys
write_sealer_environment
write_host_operations_environment
write_controller_environment
install_if_identical_or_missing \
  "$SOURCE_ROOT/systemd/bookapp-console.service" \
  "$SYSTEMD_ROOT/bookapp-console.service"
install_if_identical_or_missing \
  "$SOURCE_ROOT/systemd/bookapp-platform-bot.service" \
  "$SYSTEMD_ROOT/bookapp-platform-bot.service"
install_if_identical_or_missing \
  "$SOURCE_ROOT/systemd/bookapp-sealer.service" \
  "$SYSTEMD_ROOT/bookapp-sealer.service"
install_if_identical_or_missing \
  "$SOURCE_ROOT/systemd/bookapp-host-operations.service" \
  "$SYSTEMD_ROOT/bookapp-host-operations.service"
install_if_identical_or_missing \
  "$SOURCE_ROOT/systemd/bookapp-controller.service" \
  "$SYSTEMD_ROOT/bookapp-controller.service"
install_if_identical_or_missing \
  "$SOURCE_ROOT/systemd/bookapp-tenant@.service" \
  "$SYSTEMD_ROOT/bookapp-tenant@.service"
install_if_identical_or_missing \
  "$SOURCE_ROOT/caddy/bookapp-tenants.import" \
  "$CADDY_IMPORT"
systemctl daemon-reload

cat <<'EOF'
Bootstrap assets are installed but no service was enabled or started.

Before enabling anything, create root-owned, mode 0400 files:
  /etc/bookapp/platform-bot-token
  /etc/bookapp/console.env
  /etc/bookapp/platform-bot.env

The bootstrap generated root-only sealer.env and controller.env; review their
absolute paths before enabling services.

Add `import /etc/caddy/bookapp-tenants.import` once to the main Caddyfile,
then validate Caddy. Test the complete flow only with a newly-created disposable
tenant before enabling it for production. Do not adopt a legacy tenant through
this bootstrap.
EOF
