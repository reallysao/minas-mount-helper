#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T10：存储非空时界面刷新不得因 human_size 未定义抛 NameError。

human_size 直接从 main.py 源码 AST 提取（而非测试自带副本），保证 main 删除
该定义时测试能抓到回归。隔离 AST 执行，不启动 Tkinter、不触网、不挂载。
"""
import ast
import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

HERE = os.path.dirname(os.path.abspath(__file__))
MOUNTAPP = os.path.abspath(os.path.join(HERE, "..", "..", "mountapp"))
MAIN_PY = os.path.join(MOUNTAPP, "main.py")


def _module_fns():
    """从 main.py 提取模块级 human_size 与 App.refresh 的源码节点。"""
    with open(MAIN_PY, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    human = next(n for n in tree.body
                 if isinstance(n, ast.FunctionDef) and n.name == "human_size")
    cls = next(n for n in tree.body
               if isinstance(n, ast.ClassDef) and n.name == "App")
    refresh = next(n for n in cls.body
                   if isinstance(n, ast.FunctionDef) and n.name == "refresh")
    return human, refresh


class GuiRefresh(unittest.TestCase):
    def _load_globals(self):
        human, refresh = _module_fns()
        g = {"ORANGE": "o", "GREEN": "g", "RED": "r", "GRAY": "y",
             "SUBTEXT": "s", "BLUE": "b"}
        exec(compile(ast.Module(body=[human, refresh], type_ignores=[]),
                     "<main>", "exec"), g)
        return g

    def test_human_size_formatting_from_main(self):
        g = self._load_globals()
        hs = g["human_size"]
        self.assertEqual(hs(0), "0 B")
        self.assertEqual(hs(1024), "1.00 KB")
        self.assertEqual(hs(1024 * 1024), "1.00 MB")
        self.assertEqual(hs(1024 ** 3), "1.00 GB")

    def test_refresh_with_storage_does_not_nameerror(self):
        g = self._load_globals()
        obj = SimpleNamespace(
            core=SimpleNamespace(
                conn=SimpleNamespace(snapshot=lambda: {
                    "connected": True, "error": "", "mode": "p2p",
                    "webdav_port": 12345,
                    "storage": [{"total": 1024 * 1024, "used": 512 * 1024}]}),
                is_mounted=lambda: False, mount_path=None, status_text=""),
            status_label=Mock(), status_dot=Mock(), dot=1,
            conn_text=Mock(), open_official_btn=Mock(),
            storage_bar={}, storage_text=Mock(),
            mount_text=Mock(), mount_btn=Mock(), unmount_btn=Mock(),
            open_finder_btn=Mock(), log_label=Mock())
        # 不得抛 NameError（human_size 取自 main.py 真实定义）
        g["refresh"](obj)


if __name__ == "__main__":
    unittest.main(verbosity=2)
