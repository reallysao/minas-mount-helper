#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M01–M10：无 macOS 实机时的控制面/协议契约回归。"""
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
MOUNTAPP = ROOT / "mountapp"
sys.path.insert(0, str(MOUNTAPP))
sys.path.insert(0, str(HERE))

import app_core as ac
import client as ipc_client
import conn_manager as cm
import main as gui
import mwdav_proxy as proxy
import service as svcmod
import state_store
import tls_transport
from acceptance_service_fakes import RecordingMount, RecordingProxy, ScriptedDiscovery, view


def make_request(mod, method="GET", path="/fixture", headers=None, body=b""):
    import io
    handler = object.__new__(mod.Handler)
    handler.command, handler.path = method, path
    handler.headers = headers or {}
    handler.rfile = io.BytesIO(body)
    handler.wfile = io.BytesIO()
    handler.send_response = Mock()
    handler.send_header = Mock()
    handler.end_headers = Mock()
    handler.close_connection = False
    return handler


class MacosContractAudit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mac-contract-")
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _service(self, config=None, views=None, mount=None, **kw):
        proxy_rec = RecordingProxy(18445)
        service = svcmod.ResidentService(
            config,
            config_path=str(self.root / "config.json"),
            state_path=str(self.root / "state.json"),
            socket_dir=str(self.root / "run"),
            discovery=ScriptedDiscovery(views or [view(True, 9001)]),
            mount_registry=mount or RecordingMount(),
            proxy_factory=lambda _p: proxy_rec,
            adopt_target=proxy_rec.adopt,
            interval=0.05,
            **kw)
        return service

    def test_m01_saved_config_overrides_defaults(self):
        path = self.root / "config.json"
        path.write_text(json.dumps({
            "auto_mount": False,
            "auto_start": True,
            "mount_dir": "/Volumes/MiNAS",
            "tls_server_name": "nas.fixture.test",
        }))
        loaded = ac.load_app_config(str(path))
        self.assertFalse(loaded["auto_mount"])
        self.assertTrue(loaded["auto_start"])
        self.assertEqual(loaded["mount_dir"], "/Volumes/MiNAS")
        self.assertEqual(loaded["tls_server_name"], "nas.fixture.test")

    def test_m01_service_loads_and_persists_config_and_auto_start(self):
        path = self.root / "config.json"
        path.write_text(json.dumps({"auto_mount": False, "auto_start": False,
                                    "tls_server_name": "nas.fixture.test"}))
        calls = []
        service = self._service(
            None, auto_start_setter=lambda enabled: calls.append(enabled))
        self.assertFalse(service.config["auto_mount"])
        out = service._cmd_set_config({"auto_mount": True, "auto_start": True})
        saved = json.loads(path.read_text())
        self.assertTrue(saved["auto_mount"])
        self.assertTrue(saved["auto_start"])
        self.assertEqual(saved["tls_server_name"], "nas.fixture.test")
        self.assertEqual(calls, [True])
        self.assertTrue(out["auto_start"])

    def test_m01_set_config_rolls_back_when_auto_start_fails(self):
        path = self.root / "config.json"
        path.write_text(json.dumps({"auto_mount": False, "auto_start": False,
                                    "tls_server_name": "nas.fixture.test"}))
        calls = []

        def setter(enabled):
            calls.append(enabled)
            if enabled:
                raise RuntimeError("launchctl failed")

        service = self._service(None, auto_start_setter=setter)
        with self.assertRaises(RuntimeError):
            service._cmd_set_config({"auto_start": True})
        self.assertFalse(service.config["auto_start"])
        self.assertFalse(json.loads(path.read_text())["auto_start"])
        self.assertEqual(calls, [True, False])

    def test_m01_gui_initializes_config_from_status(self):
        client = Mock()
        client.call.return_value = {
            "auto_mount": False, "auto_start": True,
            "mount_dir": "/Volumes/MiNAS",
        }
        core = gui._ServiceCore(client)
        client.call.assert_called_once_with("get_config")
        self.assertEqual(core.config, {
            "auto_mount": False, "auto_start": True,
            "mount_dir": "/Volumes/MiNAS"})

    def test_m02_launcher_supports_source_and_bundle_layout_and_is_executable(self):
        launcher = MOUNTAPP / "launcher.sh"
        self.assertTrue(os.access(launcher, os.X_OK))
        text = launcher.read_text()
        self.assertIn('"$SCRIPT_DIR/main.py"', text)
        self.assertIn('"$SCRIPT_DIR/../Resources"', text)

    def test_m03_resident_service_cleans_dead_mount_and_recovers(self):
        mount = RecordingMount()
        mount._mounted = "/Volumes/127.0.0.1"
        mount.alive = lambda _expected: True
        service = self._service(views=[view(False, None, error="down")], mount=mount)
        service._start_proxy()
        service._mount_health(view(False, None, error="down"))
        service._mount_health(view(False, None, error="down"))
        self.assertEqual(mount.unmounts, ["/Volumes/127.0.0.1"])
        service._mount_health(view(True, 9001))
        self.assertEqual(len(mount.mounts), 1)

    def test_m04_success_without_dav_header_is_not_confirmed(self):
        response = Mock(status=200, headers={})
        response.read.return_value = b""
        connection = Mock()
        connection.getresponse.return_value = response
        cfg = cm.TlsConfig(ca_file="fixture", server_name="nas.fixture.test")
        with patch.object(cm, "build_strict_context", return_value=Mock()), \
             patch.object(cm, "_VerifiedConn", return_value=connection):
            outcome = cm._probe_once(9001, cfg, None, None, 1, present_cert=False)
        self.assertEqual(outcome.status, "not_dav")
        self.assertFalse(outcome.dav_capable)

    def test_m05_incomplete_target_precedes_auth_and_close_sets_handler_flag(self):
        empty = state_store.TargetSnapshot(port=0, username="", password="")
        proxy.UP = proxy.Upstream()
        handler = make_request(proxy, "GET", "/", {}, b"")
        with patch.object(proxy, "_reload_snapshot", return_value=empty), \
             patch.object(proxy, "_send_upstream") as upstream:
            handler._handle()
        handler.send_response.assert_called_with(503)
        upstream.assert_not_called()

        handler2 = make_request(proxy, "PUT", "/large", {
            "Authorization": "Basic ignored",
            "Content-Length": str(proxy.MAX_REQUEST_BODY + 1)}, b"")
        handler2.command = "PUT"
        handler2.close_connection = False
        handler2._send_simple(413, b"no", close=True)
        self.assertTrue(handler2.close_connection)

    def test_m06_pgrep_exact_process_name_contract(self):
        captured = []
        with patch.object(cm, "_run", side_effect=lambda cmd, timeout=5: captured.append(cmd) or ""):
            self.assertIsNone(cm.find_sso_login_pid())
        self.assertEqual(captured, [["pgrep", "-x", "sso_login"]])

    def test_m07_tls_lockout_short_circuits_upstream(self):
        original = proxy.UP
        proxy.UP = proxy.Upstream()
        try:
            for _ in range(3):
                proxy.UP.lockout.record_failure(ssl.SSLError("handshake_failure"))
            with patch.object(proxy, "_send_once") as send:
                with self.assertRaises(tls_transport.TlsHandshakeError):
                    proxy._send_upstream("GET", "/", {}, None)
            send.assert_not_called()
        finally:
            proxy.UP = original

    def test_m07_proxy_cli_has_no_credential_arguments(self):
        source = (MOUNTAPP / "mwdav_proxy.py").read_text()
        self.assertNotIn('add_argument("--username"', source)
        self.assertNotIn('add_argument("--password"', source)
        self.assertIn('add_argument("--upstream", type=int, default=None)', source)
        self.assertIn('port=args.upstream if args.upstream is not None else snap.port', source)

    def test_m09_env_server_name_is_used_for_sni(self):
        target = state_store.TlsTarget(ca_file="/tmp/ca.pem", server_name=None)
        up = {"host": "127.0.0.1", "port": 9443, "generation": 9, "tls": target}
        captured = {}

        class FakeConn:
            def __init__(self, host, port, timeout, context, server_name):
                captured["server_name"] = server_name

        with patch.dict(os.environ, {"MWDAV_TLS_SERVER_NAME": "nas.env.test"}), \
             patch.object(proxy, "_build_ctx", return_value=Mock()), \
             patch.object(proxy, "_ServerNameHTTPSConnection", FakeConn):
            proxy._thread_local = threading.local()
            proxy._obtain_conn(up, force_new=True)
        self.assertEqual(captured["server_name"], "nas.env.test")

    def test_m09_expect_continue_is_rejected_before_body(self):
        original = proxy.UP
        upstream = proxy.Upstream()
        upstream.port, upstream.username, upstream.password = 9443, "u", "p"
        proxy.UP = upstream
        handler = make_request(proxy, "PUT", "/huge", {
            "Expect": "100-continue",
            "Authorization": "Basic " + __import__("base64").b64encode(b"u:p").decode(),
            "Content-Length": str(proxy.MAX_REQUEST_BODY + 1),
        })
        try:
            result = handler.handle_expect_100()
        finally:
            proxy.UP = original
        self.assertFalse(result)
        handler.send_response.assert_called_with(413)
        self.assertTrue(handler.close_connection)

    def test_m09_upstream_adopt_uses_lock(self):
        upstream = proxy.Upstream()

        class Guard:
            def __init__(self): self.entered = False
            def __enter__(self): self.entered = True
            def __exit__(self, *exc): pass

        guard = Guard()
        upstream._lock = guard
        upstream.adopt(state_store.TargetSnapshot(
            host="127.0.0.1", port=9443, username="u", password="p",
            tls=state_store.TlsTarget(server_name="nas.test")))
        self.assertTrue(guard.entered)

    def test_m09_request_gate_is_acquired_before_handle_reads_body(self):
        handler = make_request(proxy, "PUT", "/upload", {"Content-Length": "1"}, b"x")
        events = []

        class Gate:
            def acquire(self): events.append("acquire")
            def release(self): events.append("release")

        handler._handle = lambda: events.append("handle")
        with patch.object(proxy, "_request_semaphore", Gate()):
            handler._handle_guarded()
        self.assertEqual(events, ["acquire", "handle", "release"])

    def test_m09_ipc_client_reuses_and_closes_reader(self):
        fake_sock = Mock()
        fake_reader = Mock()
        # 严格匹配请求 id：两次请求分别回 id=1、id=2。
        fake_reader.readline.side_effect = [
            '{"ok":true,"id":1,"data":{}}\n',
            '{"ok":true,"id":2,"data":{}}\n',
        ]
        fake_sock.makefile.return_value = fake_reader
        c = ipc_client.IpcClient("/tmp/fake")
        c._sock = fake_sock
        c.call("status")
        c.call("status")
        self.assertEqual(fake_sock.makefile.call_count, 1)
        c.close()
        fake_reader.close.assert_called_once()
        fake_sock.close.assert_called_once()

    def test_m09_start_failure_releases_lock_and_ipc(self):
        service = self._service()
        failing = Mock()
        failing.start.side_effect = RuntimeError("ipc failed")
        with patch("ipc.IpcServer", return_value=failing):
            with self.assertRaises(RuntimeError):
                service.start()
        self.assertIsNone(service._lock_fd)
        failing.stop.assert_called_once()

    def test_m10_manual_mount_resets_service_unmounted_intent_only_on_success(self):
        mount = RecordingMount()
        service = self._service(views=[view(True, 9001)], mount=mount)
        service._view = view(True, 9001)
        service._user_unmounted = True
        service._cmd_mount({})
        self.assertFalse(service._user_unmounted)

        class FailingMount(RecordingMount):
            def mount(self, *args):
                return False, "fixture failure"

        failed = self._service(views=[view(True, 9001)], mount=FailingMount())
        failed._view = view(True, 9001)
        failed._user_unmounted = True
        with self.assertRaises(ValueError):
            failed._cmd_mount({})
        self.assertTrue(failed._user_unmounted)

    def test_m10_query_is_preserved_on_first_attempt_and_retry(self):
        paths = []
        original = proxy.UP
        proxy.UP = proxy.Upstream()
        proxy.UP.host = "127.0.0.1"
        proxy.UP.port = 9443
        proxy.UP.base = "/pool0/data"
        proxy.UP.username = "u"
        proxy.UP.password = "p"
        proxy.UP.tls = state_store.TlsTarget(server_name="nas.test")
        try:
            calls = [ConnectionResetError("retry"), (200, {}, b"ok")]

            def capture(_up, _method, full, _headers, _body):
                paths.append(full)
                outcome = calls.pop(0)
                if isinstance(outcome, BaseException):
                    raise outcome
                return outcome

            snap = state_store.TargetSnapshot(
                host="127.0.0.1", port=9443, base="/pool0/data",
                username="u", password="p",
                tls=state_store.TlsTarget(server_name="nas.test"))
            with patch.object(proxy, "_send_once", side_effect=capture), \
                 patch.object(proxy, "_reload_snapshot", return_value=snap):
                proxy._send_upstream("GET", "/folder name/file?x=1&name=a%20b", {}, None)
        finally:
            proxy.UP = original
        self.assertEqual(paths, [
            "/pool0/data/folder%20name/file?x=1&name=a%20b",
            "/pool0/data/folder%20name/file?x=1&name=a%20b",
        ])

    def test_m10_native_ssl_error_is_classified_and_records_lockout(self):
        original = proxy.UP
        proxy.UP = proxy.Upstream()
        proxy.UP.host = "127.0.0.1"
        proxy.UP.port = 9443
        proxy.UP.base = "/pool0/data"
        proxy.UP.username = "u"
        proxy.UP.password = "p"
        proxy.UP.tls = state_store.TlsTarget(server_name="nas.test")
        try:
            err = ssl.SSLCertVerificationError(1, "certificate verify failed")
            with patch.object(proxy, "_send_once", side_effect=err):
                with self.assertRaises(tls_transport.TlsVerifyError):
                    proxy._send_upstream("GET", "/", {}, None)
            status = proxy.UP.lockout.status()
            self.assertEqual(status["attempts"], 1)
            self.assertEqual(status["reason_class"], "verify")
        finally:
            proxy.UP = original

    def test_m10_netfs_timeout_kills_and_reaps_child(self):
        core = ac.AppCore({"mount_dir": ""})
        proc = Mock()
        proc.communicate.side_effect = [
            subprocess.TimeoutExpired("netfs_mount", 40),
            (b"", b"killed"),
        ]
        with patch.object(core, "is_mounted", return_value=False), \
             patch.object(core, "_helper_supported", return_value=True), \
             patch.object(ac.subprocess, "Popen", return_value=proc):
            ok, message = core.mount_with_credentials(
                "http://127.0.0.1:18445/", "/Volumes/127.0.0.1", "u", "p")
        self.assertFalse(ok)
        proc.kill.assert_called_once()
        self.assertEqual(proc.communicate.call_count, 2)
        self.assertIn("超时", message)

    def test_m10_netfs_stdin_parser_preserves_credential_spaces(self):
        source = (MOUNTAPP / "netfs_mount.m").read_text()
        self.assertNotIn("whitespaceCharacterSet", source)
        self.assertIn("componentsSeparatedByString:@\"\\n\"", source)
        self.assertIn("hasSuffix:@\"\\r\"", source)

    def test_m08_first_confirmed_does_not_wait_for_slow_loser(self):
        slow_release = threading.Event()

        def probe(port):
            if port == 1:
                return cm.ProbeOutcome("dav", "confirmed", 200, True, False, "ok", "")
            slow_release.wait(2)
            return cm.ProbeOutcome("connect", "unreachable", None, False, False, "timeout", "")

        start = time.monotonic()
        try:
            found = cm._parallel_first_confirmed([1, 2], probe, max_workers=2)
        finally:
            elapsed = time.monotonic() - start
            slow_release.set()
        self.assertEqual(found[0], 1)
        self.assertLess(elapsed, 0.5, f"first-confirmed 被慢探针拖住 {elapsed:.3f}s")


if __name__ == "__main__":
    unittest.main(verbosity=2)
