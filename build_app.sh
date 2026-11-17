#!/bin/bash
# 一键构建「小米智能存储挂载助手.app」
#
# 说明：
#   - 运行 App 不需要 Python；只有本机构建时才需要 Python 3.10+ 与 PyInstaller。
#   - 源码使用 `X | None` 注解语法，低于 3.10 的解释器会在运行时报 TypeError，
#     所以构建脚本会先检查版本。
#   - PyInstaller 不能跨架构编译：在 Intel 机器上产出 x86_64 包，
#     要原生 arm64 包需在 Apple Silicon 机器上构建。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
SRC="$ROOT/mountapp"
BUILD="$ROOT/build"
APP="$BUILD/小米智能存储挂载助手.app"
PY="${PYTHON:-python3}"

echo "==> 检查 Python 版本"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' || {
    echo "需要 Python 3.10+（当前 $("$PY" -V 2>&1)）"; exit 1;
}

echo "==> PyInstaller 打包运行时"
"$PY" -m PyInstaller --noconfirm --onedir --windowed --name mwdav_gui \
    --add-data "$SRC/netfs_mount:." \
    --add-data "$SRC/localhost.crt:." \
    --add-data "$SRC/localhost.key:." \
    --hidden-import ipc --hidden-import client --hidden-import tls_transport \
    --hidden-import state_store --hidden-import launch_agent \
    --distpath "$BUILD/dist" --workpath "$BUILD/work" --specpath "$BUILD" \
    "$SRC/main.py"

echo "==> 组装 .app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cp "$SRC/launcher.sh" "$APP/Contents/MacOS/launcher.sh"
chmod +x "$APP/Contents/MacOS/launcher.sh"

cp -R "$BUILD/dist/mwdav_gui" "$APP/Contents/Resources/mwdav_runtime"
cp "$SRC"/*.py "$APP/Contents/Resources/"
cp "$SRC/netfs_mount" "$SRC/localhost.crt" "$SRC/localhost.key" "$SRC/AppIcon.icns" \
   "$APP/Contents/Resources/" 2>/dev/null || true

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>CFBundleDisplayName</key>
	<string>小米智能存储挂载助手</string>
	<key>CFBundleExecutable</key>
	<string>launcher.sh</string>
	<key>CFBundleIconFile</key>
	<string>AppIcon</string>
	<key>CFBundleIdentifier</key>
	<string>com.minas.mounthelper</string>
	<key>CFBundleInfoDictionaryVersion</key>
	<string>6.0</string>
	<key>CFBundleName</key>
	<string>小米智能存储挂载助手</string>
	<key>CFBundlePackageType</key>
	<string>APPL</string>
	<key>CFBundleShortVersionString</key>
	<string>1.5</string>
	<key>CFBundleVersion</key>
	<string>1.5</string>
	<key>LSApplicationCategoryType</key>
	<string>public.app-category.utilities</string>
	<key>LSMinimumSystemVersion</key>
	<string>11.0</string>
	<key>NSHighResolutionCapable</key>
	<true/>
	<key>NSHumanReadableCopyright</key>
	<string>本地挂载助手（非小米官方应用）</string>
</dict>
</plist>
PLIST

echo "==> 完成：$APP"
echo "    架构：$(file -b "$APP/Contents/Resources/mwdav_runtime/mwdav_gui" | grep -o 'arm64\|x86_64' | head -1)"
