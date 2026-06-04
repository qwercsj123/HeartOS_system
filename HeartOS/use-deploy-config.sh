#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND_DIR="$ROOT_DIR/heartos_backend"
TARGET_ENV="$BACKEND_DIR/.env"
TARGET_FRONTEND="$ROOT_DIR/deploy-config.js"

read_env_value() {
  local key="$1"
  local file="$2"
  if [ ! -f "$file" ]; then
    return 0
  fi
  local line
  line="$(grep -E "^${key}=" "$file" | tail -n 1 || true)"
  line="${line#*=}"
  line="${line%\"}"
  line="${line#\"}"
  printf '%s' "$line"
}

read_frontend_backend_url() {
  local file="$1"
  if [ ! -f "$file" ]; then
    return 0
  fi
  sed -n "s/.*HEARTOS_BACKEND_BASE_URL = '\(.*\)';/\1/p" "$file" | tail -n 1
}

usage() {
  cat <<'EOF'
用法:
  ./use-deploy-config.sh local
  ./use-deploy-config.sh server
EOF
}

if [ "${1:-}" = "" ]; then
  usage
  exit 1
fi

MODE="$1"
case "$MODE" in
  local)
    ENV_TEMPLATE="$BACKEND_DIR/.env.local.example"
    FRONTEND_TEMPLATE="$ROOT_DIR/deploy-config.local.example.js"
    ;;
  server)
    ENV_TEMPLATE="$BACKEND_DIR/.env.server.example"
    FRONTEND_TEMPLATE="$ROOT_DIR/deploy-config.server.example.js"
    ;;
  *)
    echo "未知参数: $MODE" >&2
    usage
    exit 1
    ;;
esac

if [ ! -f "$ENV_TEMPLATE" ]; then
  echo "未找到模板: $ENV_TEMPLATE" >&2
  exit 1
fi

if [ ! -f "$FRONTEND_TEMPLATE" ]; then
  echo "未找到模板: $FRONTEND_TEMPLATE" >&2
  exit 1
fi

cp "$ENV_TEMPLATE" "$TARGET_ENV"
cp "$FRONTEND_TEMPLATE" "$TARGET_FRONTEND"

APP_PORT_VALUE="$(read_env_value APP_PORT "$TARGET_ENV")"
APP_CORS_VALUE="$(read_env_value APP_CORS_ORIGINS "$TARGET_ENV")"
APP_PUBLIC_BASE_URL_VALUE="$(read_env_value APP_PUBLIC_BASE_URL "$TARGET_ENV")"
APP_AUTH_MODE_VALUE="$(read_env_value APP_AUTH_MODE "$TARGET_ENV")"
APP_AUTH_UPSTREAM_BASE_VALUE="$(read_env_value APP_AUTH_UPSTREAM_BASE "$TARGET_ENV")"
FRONTEND_BACKEND_URL_VALUE="$(read_frontend_backend_url "$TARGET_FRONTEND")"

echo "已切换到 $MODE 配置:"
echo "  后端: $TARGET_ENV"
echo "  前端: $TARGET_FRONTEND"
echo
echo "当前关键配置:"
echo "  APP_PORT=$APP_PORT_VALUE"
echo "  APP_CORS_ORIGINS=$APP_CORS_VALUE"
echo "  APP_PUBLIC_BASE_URL=$APP_PUBLIC_BASE_URL_VALUE"
echo "  APP_AUTH_MODE=$APP_AUTH_MODE_VALUE"
if [ -n "$APP_AUTH_UPSTREAM_BASE_VALUE" ]; then
  echo "  APP_AUTH_UPSTREAM_BASE=$APP_AUTH_UPSTREAM_BASE_VALUE"
fi
echo "  HEARTOS_BACKEND_BASE_URL=$FRONTEND_BACKEND_URL_VALUE"
echo
echo "接下来请检查并按需修改:"
echo "  1. $TARGET_ENV"
echo "  2. $TARGET_FRONTEND"
echo
echo "然后重启后端并刷新前端页面。"
