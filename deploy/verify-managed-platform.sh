#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  verify-managed-platform.sh --socket ABSOLUTE_SOCKET --tenant UUID \
    --generation POSITIVE_INTEGER --unit bookapp-tenant@UUID.service \
    [--caddy-config ABSOLUTE_CADDYFILE]

Performs read-only managed tenant verification. It does not start, restart,
enable, reload, provision, mutate databases, or print health response content.
EOF
}

fail() {
  printf '%s\n' "$*" >&2
  exit 1
}

socket_path=''
tenant_id=''
generation=''
unit_name=''
caddy_config=''
python_bin="${PYTHON_BIN:-python3}"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --socket)
      [ "$#" -ge 2 ] || fail "missing socket path"
      socket_path="$2"
      shift 2
      ;;
    --tenant)
      [ "$#" -ge 2 ] || fail "missing tenant id"
      tenant_id="$2"
      shift 2
      ;;
    --generation)
      [ "$#" -ge 2 ] || fail "missing generation"
      generation="$2"
      shift 2
      ;;
    --unit)
      [ "$#" -ge 2 ] || fail "missing unit name"
      unit_name="$2"
      shift 2
      ;;
    --caddy-config)
      [ "$#" -ge 2 ] || fail "missing Caddy config path"
      caddy_config="$2"
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      fail "unsupported argument"
      ;;
  esac
done

[ -n "$socket_path" ] && [ -n "$tenant_id" ] && [ -n "$generation" ] && [ -n "$unit_name" ] || {
  usage >&2
  exit 2
}

case "$socket_path" in
  /*) ;;
  *) fail "socket path must be absolute" ;;
esac
[[ "$tenant_id" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]] || fail "tenant id must be a UUID"
case "$generation" in
  *[!0-9]*|'') fail "generation must be a positive integer" ;;
esac
[ "$generation" -gt 0 ] || fail "generation must be a positive integer"
expected_unit="bookapp-tenant@${tenant_id}.service"
[ "$unit_name" = "$expected_unit" ] || fail "unit name does not match tenant"
[ ! -L "$socket_path" ] && [ -S "$socket_path" ] || fail "socket is unavailable"

command -v systemctl >/dev/null 2>&1 || fail "systemctl is unavailable"
command -v curl >/dev/null 2>&1 || fail "curl is unavailable"
command -v "$python_bin" >/dev/null 2>&1 || fail "python3 is unavailable"

systemctl is-active --quiet "$unit_name" || fail "tenant unit is not active"
curl --fail --silent --show-error \
  --connect-timeout 2 \
  --max-time 5 \
  --unix-socket "$socket_path" \
  http://localhost/health |
  "$python_bin" -c '
import json
import sys

tenant_id, generation = sys.argv[1:]
try:
    payload = json.load(sys.stdin)
except (json.JSONDecodeError, ValueError):
    raise SystemExit(1)
if payload != {"tenant_id": tenant_id, "generation": int(generation)}:
    raise SystemExit(1)
' "$tenant_id" "$generation" || fail "tenant health response is invalid"

if [ -n "$caddy_config" ]; then
  case "$caddy_config" in
    /*) ;;
    *) fail "Caddy config path must be absolute" ;;
  esac
  [ -f "$caddy_config" ] && [ ! -L "$caddy_config" ] || fail "Caddy config is unavailable"
  command -v caddy >/dev/null 2>&1 || fail "caddy is unavailable"
  caddy validate --config "$caddy_config" >/dev/null || fail "Caddy config is invalid"
fi

printf '%s\n' "managed tenant verification passed"
