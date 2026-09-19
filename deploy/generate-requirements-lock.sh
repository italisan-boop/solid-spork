#!/usr/bin/env bash
set -euo pipefail

readonly SCRIPT_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
readonly REPOSITORY_ROOT="$(CDPATH= cd -- "$SCRIPT_ROOT/.." && pwd)"
readonly INPUT_PATH="${1:-$REPOSITORY_ROOT/requirements.in}"
readonly OUTPUT_PATH="${2:-$REPOSITORY_ROOT/requirements-linux.txt}"
readonly PYTHON_BIN="${PYTHON_BIN:-python3}"

fail() {
  printf '%s\n' "$*" >&2
  exit 1
}

absolute_path() {
  case "$1" in
    /*) printf '%s\n' "$1" ;;
    *) fail "path must be absolute: $1" ;;
  esac
}

is_regular_file() {
  [ -f "$1" ] && [ ! -L "$1" ]
}

input_path="$(absolute_path "$INPUT_PATH")"
output_path="$(absolute_path "$OUTPUT_PATH")"

[ "$(uname -s)" = "Linux" ] || fail "dependency lock generation requires Linux"
command -v "$PYTHON_BIN" >/dev/null 2>&1 || fail "Python executable is unavailable: $PYTHON_BIN"
is_regular_file "$input_path" || fail "dependency input is not a regular file: $input_path"

python_version="$($PYTHON_BIN -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
[ "$python_version" = "3.12" ] || fail "dependency lock generation requires Python 3.12"

$PYTHON_BIN -c 'import piptools' >/dev/null 2>&1 || fail "pip-tools is unavailable in the selected Python environment"

case "$output_path" in
  "$REPOSITORY_ROOT"/*) ;;
  *) fail "lock output must be inside the repository root" ;;
esac

if [ -e "$output_path" ] || [ -L "$output_path" ]; then
  [ "${REPLACE_EXISTING:-0}" = "1" ] || fail "refusing to overwrite existing lock; set REPLACE_EXISTING=1"
  [ ! -L "$output_path" ] || fail "refusing to replace a symbolic link"
fi

output_directory="$(dirname -- "$output_path")"
[ -d "$output_directory" ] || fail "lock output directory is unavailable: $output_directory"
temporary_output="$(mktemp "$output_directory/.requirements-lock.XXXXXX")"
cleanup() {
  rm -f -- "$temporary_output"
}
trap cleanup EXIT

$PYTHON_BIN -m piptools compile \
  --generate-hashes \
  --no-annotate \
  --no-header \
  --strip-extras \
  --resolver=backtracking \
  --output-file "$temporary_output" \
  "$input_path"

[ -s "$temporary_output" ] || fail "pip-tools produced an empty dependency lock"
if ! grep -q -- '--hash=sha256:' "$temporary_output"; then
  fail "generated dependency lock contains no SHA-256 hashes"
fi

mv -- "$temporary_output" "$output_path"
printf 'Generated Linux/Python 3.12 dependency lock: %s\n' "$output_path"
