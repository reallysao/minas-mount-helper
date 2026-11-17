#!/bin/bash
# 小米智能存储 Finder 挂载助手 - App 启动器
# 全部逻辑（GUI 与常驻服务）都跑在打包的 mwdav_runtime/mwdav_gui 里，
# 不依赖任何本机私有 Python 路径，下载后即可运行。
DIR="$(cd "$(dirname "$0")/../Resources" && pwd)"
cd "$DIR" || exit 1

GUI="$DIR/mwdav_runtime/mwdav_gui"
if [ ! -x "$GUI" ]; then
    echo "运行时缺失：$GUI" >&2
    exit 1
fi

# GUI 单实例：已有 GUI 主进程时不再重复启动（--service 不受影响）。
case "$1" in
    --service|--hidden) ;;
    *)
        if pgrep -f "mwdav_gui$" >/dev/null 2>&1; then
            exit 0
        fi
        ;;
esac

exec "$GUI" "$@"
