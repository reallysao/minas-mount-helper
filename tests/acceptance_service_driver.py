#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真实进程驱动（审查者独占）：构造注入 fake 后调用 ResidentService.serve()。"""
import importlib.util
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import acceptance_service_fakes as F  # noqa: E402
from acceptance_service_fakes import (  # noqa: E402
    ScriptedDiscovery, RecordingMount, RecordingProxy, view)

_spec = importlib.util.spec_from_file_location("acc_service", F._MOUNTAPP / "service.py")
service = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(service)

svc = service.ResidentService(
    state_path=os.environ["ACC_STATE_PATH"],
    socket_dir=os.environ["ACC_SOCKET_DIR"],
    discovery=ScriptedDiscovery([view(True, 9001), view(True, 9002)]),
    mount_registry=RecordingMount(),
    proxy_factory=lambda p: RecordingProxy(p),
    interval=float(os.environ.get("ACC_INTERVAL", "0.3")),
    stop_timeout=2.0,
)
raise SystemExit(svc.serve())
