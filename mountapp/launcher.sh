#!/bin/bash
# 小米智能存储 Finder 挂载助手 - 源码运行启动器
# Python 查找链（通用 Mac 环境）：Homebrew → PATH → 系统。
# 找不到 3.10+ 时给出明确提示，绝不静默降级到旧版解释器。
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
for c in \
    "/opt/homebrew/opt/python@3.13/bin/python3" \
    "/opt/homebrew/opt/python@3.12/bin/python3" \
    "/opt/homebrew/opt/python@3.11/bin/python3" \
    "/usr/local/opt/python@3.13/bin/python3" \
    "/usr/local/opt/python@3.12/bin/python3" \
    "/usr/local/opt/python@3.11/bin/python3" \
    "/opt/homebrew/bin/python3" \
    "/usr/local/bin/python3" \
    "python3"; do
    if [ "$c" = "python3" ]; then
        command -v "$c" >/dev/null 2>&1 || continue
    elif [ ! -x "$c" ]; then
        continue
    fi
    if "$c" -c "import ssl,tkinter; assert __import__('sys').version_info >= (3,10)" >/dev/null 2>&1; then
        PY="$c"
        break
    fi
done
if [ -z "$PY" ] && [ -x "/usr/bin/python3" ] && \
   "/usr/bin/python3" -c "import ssl,tkinter; assert __import__('sys').version_info >= (3,10)" >/dev/null 2>&1; then
    PY="/usr/bin/python3"
fi
if [ -z "$PY" ]; then
    MSG="无法启动：需要 Python 3.10+（含 ssl 与 tkinter）。

请安装 Homebrew 后运行：
    brew install python@3.11
然后重新打开应用。"
    if command -v osascript >/dev/null 2>&1; then
        osascript -e "display dialog \"$MSG\" buttons {\"好\"} default button \"好\" with icon caution" >/dev/null 2>&1
    fi
    printf 'ERR: 需要 Python 3.10+（含 ssl 与 tkinter）。请安装 Homebrew 后执行: brew install python@3.11\n' >&2
    exit 1
fi

# 单实例由 ResidentService 的 flock 负责；启动器不得用宽泛 pgrep 误判并跳过服务。
exec "$PY" "$DIR/main.py" "$@"
