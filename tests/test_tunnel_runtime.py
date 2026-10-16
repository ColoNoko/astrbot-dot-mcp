"""Mock-process supervision tests. No binary execution, API key, or cloud Tunnel."""
import asyncio
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import stat
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp import web


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SPEC = importlib.util.spec_from_file_location("dot_tunnel_runtime_test", ROOT / "tunnel_runtime.py")
runtime = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runtime
SPEC.loader.exec_module(runtime)
TOKEN = "synthetic-local-mcp-token-" + "x" * 40
KEY = "synthetic-runtime-key-do-not-send"
TUNNEL_ID = "tunnel_" + "a" * 32


class FakeProcess:
    def __init__(self, *, ignores_term=False, ignores_kill=False, exit_code=None):
        self.returncode = None
        self.exited = asyncio.Event()
        self.ignores_term, self.ignores_kill = ignores_term, ignores_kill
        self.terminate_calls = self.kill_calls = 0
        if exit_code is not None:
            self.exit(exit_code)

    async def wait(self):
        await self.exited.wait()
        return self.returncode

    def exit(self, code):
        self.returncode = code
        self.exited.set()

    def terminate(self):
        self.terminate_calls += 1
        if not self.ignores_term:
            self.exit(-15)

    def kill(self):
        self.kill_calls += 1
        if not self.ignores_kill:
            self.exit(-9)


@unittest.skipUnless(platform.system() == "Linux", "private Linux runtime contract")
class TunnelRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = self.root / "plugin_data"
        self.data.mkdir(mode=0o700)
        self.key_file = self.data / "tunnel-runtime-key"
        self.key_file.write_text(KEY)
        self.key_file.chmod(0o600)
        self.binary = self.root / "vendor" / "tunnel-client-runtime"
        self.binary.parent.mkdir()
        header = bytearray(64)
        header[:6] = b"\x7fELF\x02\x01"
        header[18:20] = (62 if runtime.runtime_platform() == "linux-amd64" else 183).to_bytes(2, "little")
        self.binary.write_bytes(header + b"SYNTHETIC-NOT-EXECUTABLE")
        self.binary.chmod(0o644)  # Ordinary Python ZIP extraction drops execute bits.
        self.manifest = self.binary.with_name(self.binary.name + ".manifest.json")
        self.write_manifest()
        self.settings = runtime.TunnelSettings(True, TUNNEL_ID, self.key_file)
        self.endpoint_url = "http://127.0.0.1:8769/mcp"
        self.health_status = self.ready_status = 200
        app = web.Application()
        async def health(request):
            return web.Response(status=self.health_status, text=KEY + TOKEN)
        async def ready(request):
            return web.Response(status=self.ready_status, text=KEY + TOKEN)
        app.router.add_get("/healthz", health)
        app.router.add_get("/readyz", ready)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.health_url = "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1])
        self.processes = []
        self.calls = []
        self.write_health = True
        self.process_factory = FakeProcess
        async def spawn(*args, **kwargs):
            self.calls.append((args, kwargs))
            config = json.loads(Path(args[3]).read_text())
            if self.write_health:
                Path(config["health"]["url_file"]).write_text(self.health_url)
            process = self.process_factory()
            self.processes.append(process)
            return process
        self.spawn = AsyncMock(side_effect=spawn)
        self.patcher = patch.object(runtime.asyncio, "create_subprocess_exec", self.spawn)
        self.patcher.start()
        self.supervisors = []
        self.supervisor = self.make_supervisor()

    def make_supervisor(self, **kwargs):
        supervisor = runtime.TunnelSupervisor(self.data, self.binary, startup_timeout=1,
                                             stop_timeout=0.03, health_timeout=0.2,
                                             poll_interval=0.01, **kwargs)
        self.supervisors.append(supervisor)
        return supervisor

    def write_manifest(self, **overrides):
        value = {"version": runtime.PINNED_VERSION, "platform": runtime.runtime_platform(),
                 "sha256": hashlib.sha256(self.binary.read_bytes()).hexdigest()}
        value.update(overrides)
        self.manifest.write_text(json.dumps(value))
        self.manifest.chmod(0o644)

    async def asyncTearDown(self):
        for process in self.processes:
            if process.returncode is None:
                process.exit(0)
        for supervisor in self.supervisors:
            await supervisor.close()
        self.patcher.stop()
        await self.runner.cleanup()
        self.temp.cleanup()

    async def start(self, supervisor=None, settings=None, **kwargs):
        return await (supervisor or self.supervisor).start(
            settings or self.settings, endpoint_url=kwargs.get("endpoint_url", self.endpoint_url),
            mcp_token=kwargs.get("mcp_token", TOKEN), proxy=kwargs.get("proxy", runtime.ProxySettings()))

    async def wait_state(self, supervisor, state):
        async with asyncio.timeout(1):
            while supervisor.status()["state"] != state:
                await asyncio.sleep(0.005)

    async def test_disabled_default_has_no_files_process_or_network(self):
        directory = self.root / "not-created"
        supervisor = runtime.TunnelSupervisor(directory, self.root / "missing")
        self.supervisors.append(supervisor)
        with patch.object(runtime.aiohttp, "ClientSession") as session:
            result = await supervisor.start()
        self.assertEqual(result["state"], "disabled")
        self.assertFalse(result["enabled"])
        self.assertFalse(directory.exists())
        self.spawn.assert_not_called()
        session.assert_not_called()

    async def test_private_file_headers_no_secret_argv_env_config_status_or_repr(self):
        with patch.dict(os.environ, {"CONTROL_PLANE_API_KEY": KEY, "MCP_EXTRA_HEADERS": TOKEN,
                                     "MCP_COMMAND": "untrusted", "LOG_HTTP_RAW_UNSAFE": "true",
                                     "HTTPS_PROXY": "https://secret@example.invalid"}):
            result = await self.start()
        args, options = self.calls[0]
        self.assertEqual(args[1:3], ("run", "--config"))
        executable = Path(args[0])
        self.assertEqual(executable.parent, self.supervisor._run_dir)
        self.assertEqual(executable.read_bytes(), self.binary.read_bytes())
        self.assertEqual(stat.S_IMODE(executable.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.binary.stat().st_mode), 0o644)
        self.assertNotIn("shell", options)
        self.assertTrue(options["start_new_session"])
        self.assertTrue(options["close_fds"])
        self.assertEqual(options["pass_fds"], (self.supervisor._file_lock,))
        for key in ("stdin", "stdout", "stderr"):
            self.assertEqual(options[key], asyncio.subprocess.DEVNULL)
        config_path = Path(args[3])
        config = json.loads(config_path.read_text())
        self.assertEqual(config["mcp"]["server_urls"], [{"channel": "main", "url": self.endpoint_url}])
        self.assertEqual(config["control_plane"]["api_key"], "file:" + str(self.key_file))
        auth_file = Path(config["mcp"]["extra_headers"]["Authorization"][5:])
        self.assertEqual(auth_file.read_text(), "Bearer " + TOKEN)
        self.assertEqual(stat.S_IMODE(auth_file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(config_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(config_path.parent.stat().st_mode), 0o700)
        rendered = repr(args) + repr(options) + config_path.read_text() + json.dumps(result) + repr(self.settings)
        for secret in (KEY, TOKEN):
            self.assertNotIn(secret, rendered)
        for variable in ("CONTROL_PLANE_API_KEY", "MCP_COMMAND", "MCP_EXTRA_HEADERS", "LOG_HTTP_RAW_UNSAFE", "HTTPS_PROXY"):
            self.assertNotIn(variable, options["env"])
        self.assertTrue(result["binary_available"])
        await self.supervisor.close()
        self.assertFalse(config_path.parent.exists())
        self.assertEqual(self.key_file.read_text(), KEY)
        self.assertEqual(self.processes[0].terminate_calls, 1)

    async def test_health_readiness_and_roundtrip_are_separate(self):
        self.ready_status = 503
        result = await self.start()
        self.assertEqual(result["state"], "running")
        self.assertTrue(result["process_running"])
        self.assertTrue(result["healthy"])
        self.assertFalse(result["ready"])
        self.assertIsNone(result["connected"])
        self.assertFalse(result["roundtrip_verified"])
        self.ready_status = 200
        await self.wait_state(self.supervisor, "ready")
        self.health_status = 503
        await self.wait_state(self.supervisor, "running")
        self.assertFalse(self.supervisor.status()["healthy"])
        self.assertFalse(self.supervisor.status()["ready"])
        self.assertEqual(self.supervisor.status()["last_error_code"], "health_unavailable")

    async def test_proxy_is_control_plane_only_and_parent_environment_is_unchanged(self):
        proxy = runtime.ProxySettings(True, "http://proxy.example:3128", "synthetic-user@", "synthetic-pass:/%")
        ambient = {name: "http://ambient-secret:password@untrusted.invalid:3128" for name in (
            "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy",
            "TUNNEL_CLIENT_HTTP_PROXY", "CONTROL_PLANE_HTTP_PROXY", "MCP_HTTP_PROXY", "HARPOON_HTTP_PROXY")}
        ambient.update({"NO_PROXY": "*", "no_proxy": "api.openai.com"})
        for endpoint in (self.endpoint_url, "http://[::1]:8769/mcp"):
            with self.subTest(endpoint=endpoint), patch.dict(os.environ, ambient):
                before = dict(os.environ)
                result = await self.start(endpoint_url=endpoint, proxy=proxy)
                self.assertEqual(dict(os.environ), before)
                args, options = self.calls[-1]
                env = options["env"]
                self.assertEqual(env["CONTROL_PLANE_HTTP_PROXY"],
                                 "http://synthetic-user%40:synthetic-pass%3A%2F%25@proxy.example:3128")
                self.assertEqual(set(env["NO_PROXY"].split(",")), {"localhost", "127.0.0.1", "::1"})
                for name in ambient:
                    if name not in ("CONTROL_PLANE_HTTP_PROXY", "NO_PROXY"):
                        self.assertNotIn(name, env)
                config_path = Path(args[3])
                config = json.loads(config_path.read_text())
                self.assertEqual(config["mcp"]["server_urls"], [{"channel": "main", "url": endpoint}])
                self.assertNotIn("http_proxy", config)
                self.assertNotIn("http_proxy", config["mcp"])
                # No proxy URL/credentials in argv, generated config, status, or
                # settings repr. Only the private child environment receives them.
                public = repr(args) + config_path.read_text() + json.dumps(result) + repr(proxy)
                for secret in (proxy.username, proxy.password, "synthetic-user%40", "synthetic-pass%3A%2F%25"):
                    self.assertNotIn(secret, public)
                self.assertNotIn("proxy.example", config_path.read_text())
                self.assertIsNone(result["connected"])
                self.assertFalse(result["roundtrip_verified"])
                await self.supervisor.close()
                self.assertFalse(config_path.parent.exists())
                self.assertEqual(dict(os.environ), before)

    async def test_disabled_proxy_ignores_saved_credentials_and_all_ambient_proxies(self):
        proxy = runtime.ProxySettings(False, "http://proxy.example:3128", "unused-user", "unused-password")
        with patch.dict(os.environ, {"CONTROL_PLANE_HTTP_PROXY": proxy.authenticated_url(),
                                     "HTTP_PROXY": proxy.authenticated_url(),
                                     "HTTPS_PROXY": proxy.authenticated_url(),
                                     "https_proxy": proxy.authenticated_url(), "NO_PROXY": "*"}):
            before = dict(os.environ)
            await self.start(proxy=proxy)
            self.assertEqual(dict(os.environ), before)
        args, options = self.calls[0]
        self.assertEqual({name for name in options["env"] if "proxy" in name.lower()}, {"NO_PROXY"})
        rendered = repr(args) + repr(options) + Path(args[3]).read_text() + json.dumps(self.supervisor.status())
        for secret in (proxy.url, proxy.username, proxy.password):
            self.assertNotIn(secret, rendered)

    async def test_enabled_proxy_has_no_effect_on_direct_health_probe(self):
        proxy = runtime.ProxySettings(True, "http://127.0.0.1:1", "fake-user", "fake-password")
        # The proxy is deliberately unusable. Only the mock process is started;
        # a successful loopback health probe says nothing about control-plane access.
        with patch.dict(os.environ, {"HTTP_PROXY": proxy.authenticated_url(),
                                     "HTTPS_PROXY": proxy.authenticated_url(), "NO_PROXY": ""}), \
                patch.object(runtime.aiohttp, "ClientSession", wraps=runtime.aiohttp.ClientSession) as factory:
            result = await self.start(proxy=proxy)
        self.assertTrue(result["healthy"])
        factory.assert_called_once()
        self.assertFalse(factory.call_args.kwargs["trust_env"])
        with patch.object(self.supervisor._session, "get", wraps=self.supervisor._session.get) as get:
            await self.supervisor._check_health()
        self.assertEqual(get.call_count, 2)
        for call in get.call_args_list:
            self.assertTrue(call.args[0].startswith(self.health_url + "/"))
            self.assertIsNone(call.kwargs["proxy"])
            self.assertFalse(call.kwargs["allow_redirects"])

    async def test_invalid_proxy_fails_closed_before_launch_with_fixed_error(self):
        for proxy in (None, {"enabled": True}, runtime.ProxySettings(True, "socks5://proxy.example:1080"),
                      runtime.ProxySettings(True, "http://secret-user:secret-pass@proxy.example:3128"),
                      runtime.ProxySettings(True, "http://proxy.example:3128", "user", "secret\npassword")):
            with self.subTest(proxy_type=type(proxy).__name__), \
                    self.assertRaisesRegex(runtime.TunnelError, "^invalid_proxy_settings$"):
                await self.start(proxy=proxy)
            self.assertEqual(self.supervisor.status()["last_error_code"], "invalid_proxy_settings")
            self.assertIsNone(self.supervisor._run_dir)
        self.spawn.assert_not_called()

    async def test_proxy_launch_failure_is_redacted_and_never_retried_direct(self):
        proxy = runtime.ProxySettings(True, "http://proxy.example:3128", "proxy-user", "proxy-password")
        self.spawn.side_effect = OSError("407 Proxy Authentication Required " + proxy.authenticated_url())
        with self.assertRaisesRegex(runtime.TunnelError, "^start_failed$") as caught:
            await self.start(proxy=proxy)
        self.assertEqual(self.spawn.await_count, 1)
        self.assertEqual(self.spawn.call_args.kwargs["env"]["CONTROL_PLANE_HTTP_PROXY"], proxy.authenticated_url())
        public = str(caught.exception) + repr(caught.exception) + json.dumps(self.supervisor.status())
        for secret in (proxy.url, proxy.username, proxy.password, "407", "Proxy Authentication Required"):
            self.assertNotIn(secret, public)
        self.assertIsNone(self.supervisor._run_dir)
        self.assertIsNone(self.supervisor._file_lock)

    async def test_proxy_process_exit_has_fixed_error_without_direct_fallback(self):
        proxy = runtime.ProxySettings(True, "http://proxy.example:3128")
        self.process_factory = lambda: FakeProcess(exit_code=2)
        with self.assertRaisesRegex(runtime.TunnelError, "^process_exited$"):
            await self.start(proxy=proxy)
        self.assertEqual(self.spawn.await_count, 1)
        self.assertEqual(self.calls[0][1]["env"]["CONTROL_PLANE_HTTP_PROXY"], proxy.url)
        self.assertEqual(self.supervisor.status()["last_error_code"], "process_exited")
        self.assertIsNone(self.supervisor._run_dir)

    async def test_proxy_startup_timeout_never_retries_direct(self):
        proxy = runtime.ProxySettings(True, "http://proxy.example:3128")
        self.write_health = False
        self.supervisor.startup_timeout = 0.15
        with self.assertRaisesRegex(runtime.TunnelError, "^startup_timeout$"):
            await self.start(proxy=proxy)
        self.assertEqual(self.spawn.await_count, 1)
        self.assertEqual(self.calls[0][1]["env"]["CONTROL_PLANE_HTTP_PROXY"], proxy.url)
        self.assertEqual(self.processes[0].terminate_calls, 1)
        self.assertIsNone(self.supervisor._run_dir)

    async def test_proxy_not_ready_keeps_same_child_without_direct_fallback(self):
        proxy = runtime.ProxySettings(True, "http://proxy.example:3128")
        self.ready_status = 503
        result = await self.start(proxy=proxy)
        self.assertEqual(result["last_error_code"], "not_ready")
        self.assertTrue(result["healthy"])
        self.assertFalse(result["ready"])
        await asyncio.sleep(0.035)
        self.assertEqual(self.spawn.await_count, 1)
        self.assertEqual(self.calls[0][1]["env"]["CONTROL_PLANE_HTTP_PROXY"], proxy.url)

    async def test_reload_changes_proxy_only_on_replacement_child_and_disable_clears_it(self):
        first_proxy = runtime.ProxySettings(True, "http://first-proxy.example:3128", "user", "first-password")
        next_proxy = runtime.ProxySettings(True, "http://second-proxy.example:3128", "user", "next-password")
        await self.start(proxy=first_proxy)
        first_directory = self.supervisor._run_dir
        await self.supervisor.reload(self.settings, endpoint_url=self.endpoint_url, mcp_token=TOKEN, proxy=next_proxy)
        self.assertIsNotNone(self.processes[0].returncode)
        self.assertFalse(first_directory.exists())
        self.assertEqual(self.calls[0][1]["env"]["CONTROL_PLANE_HTTP_PROXY"], first_proxy.authenticated_url())
        self.assertEqual(self.calls[1][1]["env"]["CONTROL_PLANE_HTTP_PROXY"], next_proxy.authenticated_url())
        await self.supervisor.reload(self.settings, endpoint_url=self.endpoint_url, mcp_token=TOKEN)
        self.assertIsNotNone(self.processes[1].returncode)
        self.assertNotIn("CONTROL_PLANE_HTTP_PROXY", self.calls[2][1]["env"])

    async def test_unexpected_exit_cleans_generated_secret_and_never_restarts(self):
        await self.start()
        directory = self.supervisor._run_dir
        self.processes[0].exit(17)
        await self.wait_state(self.supervisor, "failed")
        self.assertEqual(self.supervisor.status()["last_error_code"], "process_exited")
        self.assertFalse(self.supervisor.status()["process_running"])
        self.assertFalse(directory.exists())
        self.assertEqual(self.spawn.await_count, 1)
        self.assertIsNone(self.supervisor._file_lock)

    async def test_startup_timeout_terminates_then_kills_and_cleans(self):
        self.write_health = False
        self.supervisor.startup_timeout = 0.15
        self.process_factory = lambda: FakeProcess(ignores_term=True)
        with self.assertRaisesRegex(runtime.TunnelError, "^startup_timeout$"):
            await self.start()
        self.assertEqual(self.processes[0].terminate_calls, 1)
        self.assertEqual(self.processes[0].kill_calls, 1)
        self.assertIsNone(self.supervisor._run_dir)
        self.assertIsNone(self.supervisor._file_lock)
        self.assertEqual(self.supervisor.status()["last_error_code"], "startup_timeout")

    async def test_exit_during_startup_is_not_success(self):
        self.process_factory = lambda: FakeProcess(exit_code=2)
        with self.assertRaisesRegex(runtime.TunnelError, "^process_exited$"):
            await self.start()
        self.assertFalse(self.supervisor.status()["healthy"])
        self.assertIsNone(self.supervisor._run_dir)

    async def test_cancelled_start_reaps_process_and_removes_secrets(self):
        self.write_health = False
        self.supervisor.startup_timeout = 0.15
        task = asyncio.create_task(self.start())
        while not self.processes:
            await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIsNotNone(self.processes[0].returncode)
        self.assertIsNone(self.supervisor._run_dir)
        self.assertIsNone(self.supervisor._session)

    async def test_cancelled_close_finishes_bounded_kill_and_cleanup(self):
        self.process_factory = lambda: FakeProcess(ignores_term=True)
        await self.start()
        task = asyncio.create_task(self.supervisor.close())
        while not self.processes[0].terminate_calls:
            await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()  # Repeated unload cancellation must still finish cleanup.
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.processes[0].kill_calls, 1)
        self.assertIsNotNone(self.processes[0].returncode)
        self.assertIsNone(self.supervisor._run_dir)
        self.assertIsNone(self.supervisor._file_lock)
        self.assertEqual(self.supervisor.status()["state"], "stopped")

    async def test_replaced_source_before_staging_cannot_launch(self):
        original = self.supervisor._prepare_run
        def replace_after_verify(*args):
            value = original(*args)
            self.binary.write_bytes(b"replacement" * 10)
            return value
        with patch.object(self.supervisor, "_prepare_run", side_effect=replace_after_verify):
            with self.assertRaisesRegex(runtime.TunnelError, "^binary_checksum_mismatch$"):
                await self.start()
        self.spawn.assert_not_called()
        self.assertIsNone(self.supervisor._run_dir)

    async def test_reload_stops_old_before_new_and_disable_stops(self):
        await self.start()
        first = self.processes[0]
        old_directory = self.supervisor._run_dir
        await self.supervisor.reload(self.settings, endpoint_url=self.endpoint_url, mcp_token=TOKEN)
        self.assertIsNotNone(first.returncode)
        self.assertFalse(old_directory.exists())
        self.assertEqual(len(self.processes), 2)
        await self.supervisor.reload(runtime.TunnelSettings())
        self.assertEqual(self.supervisor.status()["state"], "disabled")
        self.assertIsNotNone(self.processes[1].returncode)
        await self.supervisor.close()
        self.assertEqual(self.processes[1].terminate_calls, 1)

    async def test_shared_data_directory_lock_prevents_duplicate_process(self):
        await self.start()
        second = self.make_supervisor()
        with self.assertRaisesRegex(runtime.TunnelError, "^runtime_in_use$"):
            await self.start(second)
        self.assertEqual(len(self.processes), 1)
        self.assertTrue(self.supervisor.status()["process_running"])
        await self.supervisor.close()
        await self.start(second)
        self.assertEqual(len(self.processes), 2)

    async def test_unreaped_process_blocks_restart_until_owner_retries_stop(self):
        self.process_factory = lambda: FakeProcess(ignores_term=True, ignores_kill=True)
        await self.start()
        with self.assertRaisesRegex(runtime.TunnelError, "^stop_failed$"):
            await self.supervisor.close()
        self.assertTrue(self.supervisor.status()["process_running"])
        self.assertIsNotNone(self.supervisor._file_lock)
        with self.assertRaisesRegex(runtime.TunnelError, "^already_running$"):
            await self.start()
        self.processes[0].exit(0)
        await self.supervisor.close()
        self.assertIsNone(self.supervisor._run_dir)

    async def test_signal_failure_is_redacted_and_eventual_exit_is_cleaned(self):
        await self.start()
        with patch.object(self.processes[0], "terminate", side_effect=OSError(KEY + TOKEN)):
            with self.assertRaisesRegex(runtime.TunnelError, "^stop_failed$"):
                await self.supervisor.close()
        self.assertNotIn(KEY, json.dumps(self.supervisor.status()))
        self.processes[0].exit(0)
        async with asyncio.timeout(1):
            while self.supervisor._run_dir is not None:
                await asyncio.sleep(0.005)
        self.assertEqual(self.supervisor.status()["last_error_code"], "process_exited")

    async def test_concurrent_starts_never_launch_a_duplicate(self):
        results = await asyncio.gather(self.start(), self.start(), return_exceptions=True)
        self.assertEqual(self.spawn.await_count, 1)
        self.assertEqual(sum(isinstance(result, runtime.TunnelError) for result in results), 1)
        self.assertTrue(self.supervisor.status()["process_running"])

    async def test_cleanup_error_is_sanitized_and_retry_preserves_owned_lock(self):
        await self.start()
        with patch.object(runtime.shutil, "rmtree", side_effect=OSError(KEY + TOKEN)):
            with self.assertRaisesRegex(runtime.TunnelError, "^cleanup_failed$"):
                await self.supervisor.close()
        self.assertIsNotNone(self.supervisor._file_lock)
        await self.supervisor.close()
        self.assertIsNone(self.supervisor._file_lock)
        self.assertIsNone(self.supervisor._run_dir)

    async def test_launch_exception_and_status_redact_all_exception_text(self):
        self.spawn.side_effect = OSError(KEY + TOKEN + str(self.key_file))
        with self.assertRaisesRegex(runtime.TunnelError, "^start_failed$") as caught:
            await self.start()
        rendered = str(caught.exception) + json.dumps(self.supervisor.status())
        for secret in (KEY, TOKEN, str(self.key_file), TUNNEL_ID):
            self.assertNotIn(secret, rendered)
        self.assertIsNone(self.supervisor._run_dir)

    async def test_only_exact_loopback_http_mcp_target_is_allowed(self):
        for url in ("http://example.com:80/mcp", "http://0.0.0.0:80/mcp", "http://localhost:80/mcp",
                    "https://127.0.0.1:80/mcp", "http://127.0.0.1/mcp", "http://127.0.0.1:80/mcp?token=x",
                    "http://x:y@127.0.0.1:80/mcp", "http://127.0.0.1:80/other", "http://127.0.0.1:80/mcp#x"):
            with self.subTest(url=url), self.assertRaisesRegex(runtime.TunnelError, "^invalid_endpoint$"):
                await self.start(endpoint_url=url)
        self.spawn.assert_not_called()
        await self.start(endpoint_url="http://[::1]:8769/mcp")

    async def test_secret_paths_permissions_symlinks_hardlinks_and_empty_rejected(self):
        link = self.data / "symlink"
        link.symlink_to(self.key_file)
        hardlink = self.data / "hardlink"
        os.link(self.key_file, hardlink)
        with self.assertRaisesRegex(runtime.TunnelError, "^invalid_key_file$"):
            await self.start(settings=runtime.TunnelSettings(True, TUNNEL_ID, hardlink))
        hardlink.unlink()
        for path in (link, self.data / ".." / "plugin_data" / "tunnel-runtime-key", self.root / "outside"):
            with self.subTest(path=str(path)), self.assertRaisesRegex(runtime.TunnelError, "^invalid_key_file$"):
                await self.start(settings=runtime.TunnelSettings(True, TUNNEL_ID, path))
        self.key_file.chmod(0o644)
        with self.assertRaisesRegex(runtime.TunnelError, "^invalid_key_file$"):
            await self.start()
        self.key_file.chmod(0o600)
        self.key_file.write_text("")
        with self.assertRaisesRegex(runtime.TunnelError, "^invalid_key_file$"):
            await self.start()
        self.spawn.assert_not_called()

    async def test_binary_must_be_pinned_native_and_checksum_verified(self):
        self.write_manifest(platform="linux-other")
        with self.assertRaisesRegex(runtime.TunnelError, "^binary_platform_mismatch$"):
            await self.start()
        self.write_manifest(sha256="0" * 64)
        with self.assertRaisesRegex(runtime.TunnelError, "^binary_checksum_mismatch$"):
            await self.start()
        self.write_manifest(version="v0.0.1")
        with self.assertRaisesRegex(runtime.TunnelError, "^binary_untrusted$"):
            await self.start()
        self.binary.write_bytes(b"#!/bin/sh\necho do-not-run\n")
        self.write_manifest()
        with self.assertRaisesRegex(runtime.TunnelError, "^binary_platform_mismatch$"):
            await self.start()
        self.binary.unlink()
        with self.assertRaisesRegex(runtime.TunnelError, "^binary_missing$"):
            await self.start()
        self.spawn.assert_not_called()

    async def test_health_url_file_cannot_redirect_to_external_or_accept_symlink(self):
        await self.start()
        for url in ("http://example.com/", "http://127.0.0.1:1234/other", "http://u:p@127.0.0.1:1234",
                    "http://127.0.0.1:1234?token=" + KEY):
            self.supervisor._health_url_file.write_text(url)
            self.assertIsNone(self.supervisor._health_base())
        path = self.supervisor._health_url_file
        path.unlink()
        path.symlink_to(self.key_file)
        self.assertIsNone(self.supervisor._health_base())

    async def test_private_data_directory_and_unsupported_platform_fail_closed(self):
        self.data.chmod(0o755)
        with self.assertRaisesRegex(runtime.TunnelError, "^private_directory_required$"):
            await self.start()
        self.data.chmod(0o700)
        with patch.object(runtime.platform, "system", return_value="Windows"):
            with self.assertRaisesRegex(runtime.TunnelError, "^unsupported_platform$"):
                await self.start()
        self.spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
