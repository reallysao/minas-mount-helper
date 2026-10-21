#!/bin/bash
# 小米智能存储 Finder 挂载助手 - 启动器
# 同时支持源码目录（mountapp/launcher.sh）和 .app/Contents/MacOS/Resources 布局。
set -u
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$SCRIPT_DIR/main.py" ]; then
    DIR="$SCRIPT_DIR"
elif [ -f "$SCRIPT_DIR/../Resources/main.py" ]; then
    DIR="$(cd "$SCRIPT_DIR/../Resources" && pwd)"
else
    printf 'ERR: main.py not found beside launcher or in ../Resources\n' >&2
    exit 1
fi
cd "$DIR" || exit 1

PY=""
for base in "$HOME/Library/Application Support/Doubao/sandbox_runtime/bases/"*/bin/python3; do
    [ -x "$base" ] || continue
    if "$base" -c "import ssl,tkinter; assert __import__('sys').version_info >= (3,10)" >/dev/null 2>&1; then
        PY="$base"
        break
    fi
done
if [ -z "$PY" ] && [ -x "/usr/bin/python3" ] && \
   "/usr/bin/python3" -c "import ssl,tkinter; assert __import__('sys').version_info >= (3,10)" >/dev/null 2>&1; then
    PY="/usr/bin/python3"
fi
if [ -z "$PY" ]; then
    printf 'ERR: Python 3.10+ with ssl and tkinter is required\n' >&2
    exit 1
fi

# 单实例由 ResidentService 的 flock 负责；启动器不得用宽泛 pgrep 误判并跳过服务。
exec "$PY" "$DIR/main.py" "$@"
