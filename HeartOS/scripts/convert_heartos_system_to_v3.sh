#!/usr/bin/env bash
set -euo pipefail

LOCAL_ROOT="${LOCAL_ROOT:-$(pwd)}"
SERVER_ROOT="${SERVER_ROOT:-}"
OUTPUT_ROOT="${OUTPUT_ROOT:-}"
BACKEND_URL="${BACKEND_URL:-http://219.147.100.43:18005}"
BACKEND_HOST_PORT="${BACKEND_HOST_PORT:-}"
CLEAN=0

usage() {
  cat <<'EOF'
Usage:
  ./scripts/convert_heartos_system_to_v3.sh [--clean] [--local-root PATH] [--server-root PATH] [--output-root PATH] [--backend-url URL] [--backend-host-port PORT]

Defaults:
  --local-root         current directory
  --server-root        auto-detected from ../heartOS_v3/HeartOS
  --output-root        ../HeartOS_v3_for_server
  --backend-url        http://219.147.100.43:18005
  --backend-host-port  unchanged unless provided

Example:
  cd ~/HeartOS/HeartOS
  ./scripts/convert_heartos_system_to_v3.sh \
    --clean \
    --server-root ../heartOS_v3/HeartOS \
    --output-root ../HeartOS_v3_for_server \
    --backend-url http://219.147.100.43:18008 \
    --backend-host-port 18008
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --clean)
      CLEAN=1
      shift
      ;;
    --local-root)
      LOCAL_ROOT="$2"
      shift 2
      ;;
    --server-root)
      SERVER_ROOT="$2"
      shift 2
      ;;
    --output-root)
      OUTPUT_ROOT="$2"
      shift 2
      ;;
    --backend-url)
      BACKEND_URL="$2"
      shift 2
      ;;
    --backend-host-port)
      BACKEND_HOST_PORT="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

abs_path() {
  local path="$1"
  if [[ -d "$path" ]]; then
    (cd "$path" && pwd -P)
  else
    local dir
    dir="$(dirname "$path")"
    local base
    base="$(basename "$path")"
    if [[ ! -d "$dir" ]]; then
      echo "Parent directory does not exist for path: $path" >&2
      exit 1
    fi
    (cd "$dir" && printf '%s/%s\n' "$(pwd -P)" "$base")
  fi
}

detect_server_root() {
  local local_root="$1"
  local local_parent
  local_parent="$(dirname "$local_root")"

  local candidates=(
    "$local_parent/heartOS_v3/HeartOS"
    "$local_parent/HeartOS_v3/HeartOS"
    "$local_root/heartOS_v3/HeartOS"
    "$local_root/HeartOS_v3/HeartOS"
    "/opt/heartos/heartOS_v3/HeartOS"
    "/opt/heartos/HeartOS_v3/HeartOS"
  )

  local candidate
  for candidate in "${candidates[@]}"; do
    if [[ -d "$candidate" ]]; then
      abs_path "$candidate"
      return
    fi
  done

  echo "Could not auto-detect server root. Pass --server-root, for example: --server-root ../heartOS_v3/HeartOS" >&2
  exit 1
}

replace_text() {
  local file="$1"
  local old="$2"
  local new="$3"

  if [[ ! -f "$file" ]]; then
    echo "Cannot patch missing file: $file" >&2
    exit 1
  fi

  OLD_TEXT="$old" NEW_TEXT="$new" perl -0pi -e 's/\Q$ENV{OLD_TEXT}\E/$ENV{NEW_TEXT}/g' "$file"
}

assert_not_root() {
  local path="$1"
  local name="$2"

  if [[ -z "$path" || "$path" == "/" ]]; then
    echo "$name points to an unsafe path: $path" >&2
    exit 1
  fi
}

need_cmd rsync
need_cmd perl
need_cmd grep

LOCAL_ROOT="$(abs_path "$LOCAL_ROOT")"
if [[ -z "$SERVER_ROOT" ]]; then
  SERVER_ROOT="$(detect_server_root "$LOCAL_ROOT")"
else
  SERVER_ROOT="$(abs_path "$SERVER_ROOT")"
fi
if [[ -z "$OUTPUT_ROOT" ]]; then
  OUTPUT_ROOT="$(dirname "$LOCAL_ROOT")/HeartOS_v3_for_server"
fi
OUTPUT_ROOT="$(abs_path "$OUTPUT_ROOT")"

assert_not_root "$LOCAL_ROOT" "LOCAL_ROOT"
assert_not_root "$SERVER_ROOT" "SERVER_ROOT"
assert_not_root "$OUTPUT_ROOT" "OUTPUT_ROOT"

if [[ ! -d "$LOCAL_ROOT" ]]; then
  echo "Local root does not exist: $LOCAL_ROOT" >&2
  exit 1
fi

if [[ ! -d "$SERVER_ROOT" ]]; then
  echo "Server root does not exist: $SERVER_ROOT" >&2
  exit 1
fi

if [[ "$OUTPUT_ROOT" == "$LOCAL_ROOT" || "$OUTPUT_ROOT" == "$SERVER_ROOT" ]]; then
  echo "Output root must be separate from local root and server root." >&2
  exit 1
fi

echo "[1/5] Preparing output: $OUTPUT_ROOT"
if [[ -e "$OUTPUT_ROOT" && "$CLEAN" -eq 1 ]]; then
  backup_path="${OUTPUT_ROOT}.backup.$(date +%Y%m%d_%H%M%S)"
  echo "Backing up existing output to: $backup_path"
  mv "$OUTPUT_ROOT" "$backup_path"
fi

mkdir -p "$OUTPUT_ROOT"

echo "[2/5] Copying local HeartOS code"
rsync -a \
  --exclude='.git/' \
  --exclude='.venv/' \
  --exclude='__pycache__/' \
  --exclude='node_modules/' \
  --exclude='model_outputs/' \
  --exclude='data/' \
  --exclude='*.pyc' \
  "$LOCAL_ROOT"/ "$OUTPUT_ROOT"/

echo "[3/5] Applying server configuration overlay from heartOS_v3"
mkdir -p "$OUTPUT_ROOT/heartos_backend"
cp -f "$SERVER_ROOT/heartos_backend/.env" "$OUTPUT_ROOT/heartos_backend/.env"

echo "[4/5] Applying fixed server patches"
replace_text \
  "$OUTPUT_ROOT/index.html" \
  "http://127.0.0.1:9000" \
  "$BACKEND_URL"

replace_text \
  "$OUTPUT_ROOT/index.html" \
  "http://219.147.100.43:18005" \
  "$BACKEND_URL"

replace_text \
  "$OUTPUT_ROOT/heartos_backend/app/config.py" \
  'ai_ecg_digitize_url: str = Field(default="")' \
  'ai_ecg_digitize_url: str = Field(default="http://219.147.100.43:18004/digitize")'

if [[ -n "$BACKEND_HOST_PORT" ]]; then
  replace_text \
    "$OUTPUT_ROOT/heartos_backend/docker-compose.yml" \
    'container_name: heartos-backend' \
    "container_name: heartos-backend-${BACKEND_HOST_PORT}"

  replace_text \
    "$OUTPUT_ROOT/heartos_backend/docker-compose.yml" \
    '"9000:9000"' \
    "\"${BACKEND_HOST_PORT}:9000\""

  replace_text \
    "$OUTPUT_ROOT/heartos_backend/docker-compose.yml" \
    "APP_PUBLIC_BASE_URL: http://127.0.0.1:9000" \
    "APP_PUBLIC_BASE_URL: $BACKEND_URL"
fi

server_data_path="$SERVER_ROOT/heartos_backend/data"
output_data_path="$OUTPUT_ROOT/heartos_backend/data"

if [[ -d "$server_data_path" ]]; then
  echo "Copying server data from heartOS_v3"
  mkdir -p "$output_data_path"
  rsync -a \
    --exclude='__pycache__/' \
    --exclude='*.pyc' \
    "$server_data_path"/ "$output_data_path"/
else
  mkdir -p "$output_data_path/uploads"
fi

echo "[5/5] Verifying converted package"
required_files=(
  "index.html"
  "ecg_digitizer_enhanced.html"
  "heartos_backend/.env"
  "heartos_backend/docker-compose.yml"
  "heartos_backend/app/main.py"
  "heartos_backend/app/config.py"
)

for relative_path in "${required_files[@]}"; do
  if [[ ! -f "$OUTPUT_ROOT/$relative_path" ]]; then
    echo "Required package file is missing: $OUTPUT_ROOT/$relative_path" >&2
    exit 1
  fi
done

if ! grep -q 'APP_AUTH_MODE=upstream' "$OUTPUT_ROOT/heartos_backend/.env"; then
  echo "Server .env was not applied: APP_AUTH_MODE is not upstream." >&2
  exit 1
fi

backend_url_pattern="$(printf '%s' "$BACKEND_URL" | sed 's/[.[\*^$()+?{}|]/\\&/g')"
if ! grep -q "$backend_url_pattern" "$OUTPUT_ROOT/index.html"; then
  echo "index.html was not patched to the server backend address." >&2
  exit 1
fi

echo "Converted HeartOS v3 package ready: $OUTPUT_ROOT"
