#!/usr/bin/env bash
set -e

# 切换到脚本所在目录
cd "$(dirname "$0")"

VENV_DIR="${VENV_DIR:-.venv}"
MIN_PYTHON_MAJOR=3
MIN_PYTHON_MINOR=10

python_ok() {
    "$1" -c "import sys; raise SystemExit(0 if sys.version_info >= (${MIN_PYTHON_MAJOR}, ${MIN_PYTHON_MINOR}) else 1)" >/dev/null 2>&1
}

pick_python() {
    if [ -n "${PYTHON_BIN:-}" ]; then
        if command -v "$PYTHON_BIN" >/dev/null 2>&1 && python_ok "$PYTHON_BIN"; then
            printf '%s' "$PYTHON_BIN"
            return 0
        fi
        echo "PYTHON_BIN=$PYTHON_BIN 版本不足或不可执行，需要 Python ${MIN_PYTHON_MAJOR}.${MIN_PYTHON_MINOR}+" >&2
        return 1
    fi

    candidates=(
        python3.12
        python3.11
        python3.10
        /opt/homebrew/bin/python3
        /usr/local/bin/python3
        /Users/chen/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3
        python3
    )
    for py in "${candidates[@]}"; do
        if command -v "$py" >/dev/null 2>&1 && python_ok "$py"; then
            printf '%s' "$py"
            return 0
        fi
    done

    echo "未找到 Python ${MIN_PYTHON_MAJOR}.${MIN_PYTHON_MINOR}+。请安装 Python 3.10+，或用 PYTHON_BIN=/path/to/python ./start.sh 指定路径。" >&2
    return 1
}

PYTHON_BIN="$(pick_python)"
echo "使用 Python: $("$PYTHON_BIN" --version 2>&1) ($PYTHON_BIN)"

if [ ! -f ".env" ] && [ -f ".env.example" ]; then
    echo "未找到 .env，已从 .env.example 复制一份。请按需修改账号模式和 API Key。"
    cp .env.example .env
fi

# 检查并创建虚拟环境。Windows 复制过来的环境只有 Scripts/python.exe，macOS 下需要重建。
if [ -d "$VENV_DIR" ] && { [ ! -x "$VENV_DIR/bin/python" ] || ! python_ok "$VENV_DIR/bin/python"; }; then
    backup="${VENV_DIR}.windows-backup.$(date +%Y%m%d%H%M%S)"
    echo "检测到 $VENV_DIR 不可用或 Python 版本不足，已备份为 $backup"
    mv "$VENV_DIR" "$backup"
fi

if [ ! -d "$VENV_DIR" ]; then
    "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

VENV_PYTHON="$VENV_DIR/bin/python"

if [ ! -x "$VENV_PYTHON" ]; then
    echo "虚拟环境创建失败：未找到 $VENV_PYTHON"
    exit 1
fi

if [ "${HEARTOS_SKIP_PIP_INSTALL:-0}" != "1" ]; then
    "$VENV_PYTHON" -m pip install --upgrade pip
    "$VENV_PYTHON" -m pip install -r requirements.txt
fi

env_value() {
    key="$1"
    default="$2"
    current="${!key:-}"
    if [ -n "$current" ]; then
        printf '%s' "$current"
        return
    fi
    if [ -f ".env" ]; then
        value="$(grep -E "^${key}=" .env | tail -n 1 | cut -d= -f2- | tr -d '\r' || true)"
        value="${value%\"}"
        value="${value#\"}"
        if [ -n "$value" ]; then
            printf '%s' "$value"
            return
        fi
    fi
    printf '%s' "$default"
}

APP_HOST_VALUE="$(env_value APP_HOST 0.0.0.0)"
APP_PORT_VALUE="$(env_value APP_PORT 9010)"
RELOAD_ARGS=()
if [ "${HEARTOS_RELOAD:-1}" = "1" ]; then
    RELOAD_ARGS=(--reload)
fi

# 启动应用
echo "HeartOS Backend: http://127.0.0.1:${APP_PORT_VALUE}"
"$VENV_PYTHON" -m uvicorn app.main:app --host "$APP_HOST_VALUE" --port "$APP_PORT_VALUE" "${RELOAD_ARGS[@]}"
