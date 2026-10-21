#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
离线测试统一入口（默认安全，按模块独立子进程隔离）。

行为：
- 只发现并运行 tests/ 目录下匹配 pattern 的 test_*.py（递归）；绝不收集仓库根目录历史探针。
- 每个模块在独立子进程运行，避免模块间全局补丁/mock 互相污染导致顺序相关失败。
- 默认不连真实 NAS、不执行挂载、不用真实凭证；用例使用 mock/临时目录/回环临时证书。
- 解析每个子进程输出的 "Ran N tests" 与 skipped；模块未收集到任何用例（Ran=0）判失败，
  避免"0 tests 也算成功"。
- 子进程设合理超时（可用 MINAS_TEST_TIMEOUT 覆盖，秒），超时即判失败并终止。

用法：
  python3 tests/run_tests.py              # 全部离线用例（CI 默认）
  python3 tests/run_tests.py -v           # 透传 unittest 详细输出
  python3 tests/run_tests.py --pattern 'test_tls*.py'
"""
import argparse
import fnmatch
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
DEFAULT_TIMEOUT = int(os.environ.get("MINAS_TEST_TIMEOUT", "180"))

ENV = dict(os.environ)
ENV["PYTHONPATH"] = HERE + os.pathsep + ENV.get("PYTHONPATH", "")
ENV["MINAS_TEST_OFFLINE"] = "1"

_RAN = re.compile(r"Ran\s+(\d+)\s+tests?")
_SKIPPED = re.compile(r"skipped=(\d+)")


def discover_modules(pattern: str) -> list[str]:
    """收集 tests/ 内匹配 pattern 的测试模块 dotted 名（pattern 必须生效）。"""
    modules = []
    for dirpath, _dirs, files in os.walk(HERE):
        for f in sorted(files):
            if not f.endswith(".py"):
                continue
            if not fnmatch.fnmatch(f, pattern):
                continue
            if not f.startswith("test_"):
                continue
            rel = os.path.relpath(os.path.join(dirpath, f), HERE)[:-3]
            modules.append(rel.replace(os.sep, "."))
    return sorted(modules)


def run_module(dotted: str, verbose: bool) -> tuple[bool, int, int, str]:
    """返回 (是否成功, ran, skipped, 输出)。子进程超时即失败。"""
    cmd = [sys.executable, "-m", "unittest", dotted]
    if verbose:
        cmd.append("-v")
    try:
        proc = subprocess.run(
            cmd, env=ENV, capture_output=True, text=True,
            cwd=REPO_ROOT, timeout=DEFAULT_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, 0, 0, f"超时（>{DEFAULT_TIMEOUT}s）终止"
    out = (proc.stdout or "") + (proc.stderr or "")
    m = _RAN.search(out)
    ran = int(m.group(1)) if m else 0
    sm = _SKIPPED.search(out)
    skipped = int(sm.group(1)) if sm else 0
    # 未收集到任何用例视为失败，防止空模块冒充成功。
    ok = proc.returncode == 0 and ran > 0
    return ok, ran, skipped, out


def main() -> int:
    ap = argparse.ArgumentParser(description="minas-mount-helper 离线测试入口")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="透传 unittest 详细输出（每个用例行）")
    ap.add_argument("--pattern", default="test_*.py",
                    help="文件名匹配模式（fnmatch，默认 test_*.py，必须生效）")
    ap.add_argument("--dynamic", action="store_true",
                    help="保留参数：回环动态端口用例默认已包含，不连真实 NAS")
    args = ap.parse_args()

    modules = discover_modules(args.pattern)
    if not modules:
        print(f"未发现匹配 {args.pattern!r} 的测试模块", file=sys.stderr)
        return 1

    passed, failed = [], []
    total_ran, total_skip = 0, 0
    for mod in modules:
        ok, ran, skipped, out = run_module(mod, args.verbose)
        total_ran += ran
        total_skip += skipped
        if ok:
            passed.append(mod)
            print(f"[OK]   {mod}  (Ran {ran}, skipped {skipped})")
        else:
            failed.append(mod)
            print(f"[FAIL] {mod}  (Ran {ran}, skipped {skipped})")
            if args.verbose:
                print(out)
            else:
                print("\n".join(out.splitlines()[-15:]))

    print(f"\n模块合计 {len(modules)}：通过 {len(passed)}，失败 {len(failed)}")
    print(f"用例合计：Ran {total_ran}，skipped {total_skip}")
    if failed:
        print("失败模块：\n  " + "\n  ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
