#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LaunchAgent 模板与安装/卸载（runtime owner；可测，默认不写真实用户系统）。

安全：
- launchctl 一律经 argv 列表调用（subprocess.run, check=True），无 shell、无字符串拼接注入；
  domain 用当前真实 uid（gui/{uid}），不硬编码 501；uid 可注入便于测试。
  runner 可注入为接收 argv 列表的可调用，测试用 fake，绝不触碰真实 LaunchAgents。
- plist 用 plistlib 生成合法 XML，launcher 路径中的 & / < 等特殊字符被正确转义。
- plist 文件 0600；agent 目录按 0700 创建。

常驻服务由 launcher.sh --service 前台运行；RunAtLoad=true，KeepAlive 采用受限策略
（仅崩溃 Crashed 重启，SuccessfulExit=false 正常退出不强拉），ThrottleInterval=10s，
避免无限重启风暴、不误杀其他进程。
"""
import os
import plistlib
import subprocess

LABEL = "com.minas.mount"
THROTTLE_INTERVAL = 10


def _domain(uid=None) -> str:
    return f"gui/{os.getuid() if uid is None else uid}"


def build_plist(launcher_path: str) -> bytes:
    """用 plistlib 生成合法 plist XML；ProgramArguments=[launcher, --service]。"""
    data = {
        "Label": LABEL,
        "ProgramArguments": [launcher_path, "--service"],
        "RunAtLoad": True,
        "KeepAlive": {"Crashed": True, "SuccessfulExit": False},
        "ThrottleInterval": THROTTLE_INTERVAL,
    }
    return plistlib.dumps(data)


def plist_path(agent_dir: str) -> str:
    return os.path.join(agent_dir, f"{LABEL}.plist")


def _run(argv, runner=None):
    if runner is not None:
        runner(argv)
        return
    subprocess.run(argv, check=True)


def install(launcher_path: str, agent_dir: str, runner=None, uid=None) -> str:
    """写 plist（0600）并 launchctl bootstrap gui/{uid}；runner 接收 argv 列表（可注入 fake）。"""
    os.makedirs(agent_dir, mode=0o700, exist_ok=True)
    path = plist_path(agent_dir)
    with open(path, "wb") as f:
        f.write(build_plist(launcher_path))
    os.chmod(path, 0o600)
    _run(["launchctl", "bootstrap", _domain(uid), path], runner=runner)
    return path


def uninstall(agent_dir: str, runner=None, uid=None) -> None:
    path = plist_path(agent_dir)
    if os.path.exists(path):
        _run(["launchctl", "bootout", _domain(uid), path], runner=runner)
        try:
            os.remove(path)
        except OSError:
            pass
