"""Opt-in supervision of an already packaged, pinned official Tunnel runtime.

No download, shell, provisioning, account access, or background installation.
The caller owns the trusted executable path and the local MCP listener lifecycle.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import tempfile
from urllib.parse import urlsplit

import aiohttp

if __package__:
    from .proxy_config import ProxyError, ProxySettings, validate_proxy
else:
    from proxy_config import ProxyError, ProxySettings, validate_proxy


PINNED_VERSION = "v0.0.16"
_TUNNEL_ID = re.compile(r"tunnel_(?:[a-z0-9]{4}_)?[a-z0-9]{32}\Z")
_ERRORS = frozenset({
    "invalid_settings", "invalid_endpoint", "invalid_mcp_token", "invalid_key_file",
    "unsupported_platform", "binary_missing", "binary_untrusted", "binary_platform_mismatch",
    "binary_checksum_mismatch", "private_directory_required", "already_running",
    "runtime_in_use", "start_failed", "startup_timeout", "process_exited",
    "health_unavailable", "not_ready", "stop_failed", "cleanup_failed", "start_cancelled",
    "invalid_proxy_settings",
})


class TunnelError(RuntimeError):
    """Only fixed, non-sensitive reason codes may escape this module."""

    def __init__(self, code):
        self.code = code if code in _ERRORS else "start_failed"
        super().__init__(self.code)


@dataclass(frozen=True)
class TunnelSettings:
    enabled: bool = False
    tunnel_id: str = field(default="", repr=False)
    runtime_key_file: str | Path = field(default="", repr=False)


def runtime_platform():
    """This plugin release supports native Linux NAS/container runtimes only."""
    machine = platform.machine().lower()
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(machine)
    if platform.system() != "Linux" or arch is None:
        raise TunnelError("unsupported_platform")
    return "linux-" + arch


def _bounded_time(value):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not 0 < value <= 120:
        raise ValueError("invalid_runtime_timeout")
    return float(value)


def _private_file(path, *, max_bytes=16384):
    """Check metadata without ever loading the user-supplied runtime API key."""
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
            info.st_uid != os.getuid() or info.st_mode & 0o077 or
            not 0 < info.st_size <= max_bytes):
        raise TunnelError("invalid_key_file")


def _write_private(path, text):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)


class TunnelSupervisor:
    """One foreground subprocess, serialized start/reload/stop, no auto-restart.

    ``binary_path`` is fixed by plugin code, never accepted from an HTTP request.
    Its sibling ``<binary-name>.manifest.json`` must be shipped by the trusted
    packager with version, platform, and extracted-binary sha256 fields.
    """

    def __init__(self, data_dir, binary_path, *, startup_timeout=15, stop_timeout=3,
                 health_timeout=1, poll_interval=5):
        self.data_dir = Path(data_dir).absolute()
        self.binary_path = Path(binary_path).absolute()
        self.startup_timeout = _bounded_time(startup_timeout)
        self.stop_timeout = _bounded_time(stop_timeout)
        self.health_timeout = _bounded_time(health_timeout)
        self.poll_interval = _bounded_time(poll_interval)
        self._lock = asyncio.Lock()
        self._process = None
        self._exit_task = self._monitor_task = None
        self._session = None
        self._run_dir = self._health_url_file = None
        self._file_lock = None
        self._enabled = False
        self._state = "disabled"
        self._healthy = self._ready = self._binary_available = False
        self._verified_digest = None
        self._last_error_code = None

    def status(self):
        running = self._process is not None and self._process.returncode is None
        return {"enabled": self._enabled, "state": self._state,
                "process_running": running, "healthy": bool(running and self._healthy),
                "ready": bool(running and self._ready), "binary_available": self._binary_available,
                "connected": None, "roundtrip_verified": False,
                "last_error_code": self._last_error_code}

    async def start(self, settings=TunnelSettings(), *, endpoint_url="", mcp_token="",
                    proxy=ProxySettings()):
        async with self._lock:
            return await self._start_locked(settings, endpoint_url, mcp_token, proxy)

    async def reload(self, settings=TunnelSettings(), *, endpoint_url="", mcp_token="",
                     proxy=ProxySettings()):
        async with self._lock:
            await self._finish_stop_locked()
            return await self._start_locked(settings, endpoint_url, mcp_token, proxy)

    async def close(self):
        async with self._lock:
            await self._finish_stop_locked()
            self._state = "stopped" if self._enabled else "disabled"
            self._last_error_code = None

    async def _finish_stop_locked(self):
        # Cancellation of plugin unload must not orphan a subprocess or its
        # transient Bearer file. Stop itself remains bounded by two timeouts.
        task = asyncio.create_task(self._stop_locked())
        cancelled = False
        while True:
            try:
                await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                if task.cancelled():
                    raise
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError

    def _validate_settings(self, settings, endpoint_url, mcp_token):
        if (not isinstance(settings, TunnelSettings) or type(settings.enabled) is not bool or
                not isinstance(settings.tunnel_id, str) or not _TUNNEL_ID.fullmatch(settings.tunnel_id)):
            raise TunnelError("invalid_settings")
        try:
            parsed = urlsplit(endpoint_url)
            if (parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "::1") or
                    parsed.username is not None or parsed.password is not None or
                    not parsed.port or parsed.path != "/mcp" or parsed.query or parsed.fragment or
                    endpoint_url != parsed.geturl()):
                raise ValueError()
        except (TypeError, ValueError, AttributeError):
            raise TunnelError("invalid_endpoint") from None
        if (not isinstance(mcp_token, str) or not 32 <= len(mcp_token) <= 1024 or
                not mcp_token.isascii() or any(ord(c) <= 32 or ord(c) >= 127 for c in mcp_token)):
            raise TunnelError("invalid_mcp_token")
        try:
            path = Path(settings.runtime_key_file)
            if not path.is_absolute():
                path = self.data_dir / path
            # No traversal or symlinks, including ancestors below the trusted root.
            relative = path.relative_to(self.data_dir)
            if ".." in relative.parts or not relative.parts:
                raise ValueError()
            cursor = self.data_dir
            for part in relative.parts:
                cursor = cursor / part
                if cursor.is_symlink():
                    raise ValueError()
            _private_file(path)
            return path
        except (OSError, TypeError, ValueError):
            raise TunnelError("invalid_key_file") from None

    def _verify_binary(self):
        self._binary_available = False
        self._verified_digest = None
        target_platform = runtime_platform()
        try:
            info = self.binary_path.lstat()
        except FileNotFoundError:
            raise TunnelError("binary_missing") from None
        try:
            if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022 or
                    info.st_uid not in (0, os.getuid()) or
                    not 20 <= info.st_size <= 128 * 1024 * 1024):
                raise TunnelError("binary_untrusted")
            manifest_path = self.binary_path.with_name(self.binary_path.name + ".manifest.json")
            manifest_info = manifest_path.lstat()
            if (not stat.S_ISREG(manifest_info.st_mode) or manifest_info.st_mode & 0o022 or
                    manifest_info.st_uid not in (0, os.getuid()) or manifest_info.st_size > 4096):
                raise TunnelError("binary_untrusted")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if (not isinstance(manifest, dict) or manifest.get("version") != PINNED_VERSION or
                    not isinstance(manifest.get("sha256"), str) or
                    not re.fullmatch(r"[0-9a-f]{64}", manifest["sha256"])):
                raise TunnelError("binary_untrusted")
            if manifest.get("platform") != target_platform:
                raise TunnelError("binary_platform_mismatch")
            digest = hashlib.sha256()
            with self.binary_path.open("rb") as handle:
                header = handle.read(20)
                # Reject scripts, foreign-OS executables, and a mismatched ELF architecture.
                architecture = 62 if target_platform == "linux-amd64" else 183
                if (header[:6] != b"\x7fELF\x02\x01" or
                        int.from_bytes(header[18:20], "little") != architecture):
                    raise TunnelError("binary_platform_mismatch")
                digest.update(header)
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
            if digest.hexdigest() != manifest["sha256"]:
                raise TunnelError("binary_checksum_mismatch")
        except TunnelError:
            raise
        except (OSError, UnicodeError, ValueError, TypeError):
            raise TunnelError("binary_untrusted") from None
        self._binary_available = True
        self._verified_digest = digest.hexdigest()

    def _stage_binary(self):
        """ZIP installers may discard executable bits; never chmod the input.

        Only an explicitly enabled start reaches here, after native platform,
        pinned manifest and source hash validation. Rehash the private copy to
        reject a package file replaced between verification and copying.
        """
        destination = self._run_dir / "tunnel-client-runtime"
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o700)
        digest = hashlib.sha256()
        total = 0
        with os.fdopen(descriptor, "wb") as output:
            descriptor = os.open(self.binary_path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as source:
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    raise TunnelError("binary_untrusted")
                while chunk := source.read(1024 * 1024):
                    total += len(chunk)
                    if total > 128 * 1024 * 1024:
                        raise TunnelError("binary_untrusted")
                    digest.update(chunk)
                    output.write(chunk)
        if digest.hexdigest() != self._verified_digest:
            raise TunnelError("binary_checksum_mismatch")
        return destination

    def _prepare_run(self, settings, endpoint_url, mcp_token, key_file):
        import fcntl  # Linux only; kept lazy so a disabled plugin imports elsewhere.
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        root = self.data_dir / "tunnel"
        root.mkdir(mode=0o700, exist_ok=True)
        for directory in (self.data_dir, root):
            info = directory.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or
                    info.st_mode & 0o077):
                raise TunnelError("private_directory_required")
        descriptor = os.open(root / "runtime.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise TunnelError("private_directory_required")
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            raise TunnelError("runtime_in_use") from None
        except BaseException:
            os.close(descriptor)
            raise
        self._file_lock = descriptor
        self._run_dir = Path(tempfile.mkdtemp(prefix="run-", dir=root))
        self._health_url_file = self._run_dir / "health-url"
        header_file = self._run_dir / "mcp-authorization"
        _write_private(header_file, "Bearer " + mcp_token)
        config = {
            "config_version": 1,
            "control_plane": {"base_url": "https://api.openai.com", "tunnel_id": settings.tunnel_id,
                              "api_key": "file:" + str(key_file)},
            "mcp": {"server_urls": [{"channel": "main", "url": endpoint_url}],
                    "extra_headers": {"Authorization": "file:" + str(header_file)},
                    "startup_wait_timeout": "5s"},
            "health": {"listen_addr": "127.0.0.1:0", "url_file": str(self._health_url_file)},
            "log": {"level": "warn", "format": "json"},
        }
        config_file = self._run_dir / "runtime.yaml"
        # JSON is a YAML subset. Quoting is deterministic; no YAML/string interpolation.
        _write_private(config_file, json.dumps(config, ensure_ascii=True))
        return config_file

    async def _start_locked(self, settings, endpoint_url, mcp_token, proxy):
        if not isinstance(settings, TunnelSettings) or type(settings.enabled) is not bool:
            raise TunnelError("invalid_settings")
        if settings.enabled is False:
            await self._finish_stop_locked()
            self._enabled, self._state, self._last_error_code = False, "disabled", None
            return self.status()
        if self._process is not None:
            raise TunnelError("already_running")
        self._enabled, self._state, self._last_error_code = True, "starting", None
        try:
            try:
                proxy = validate_proxy(proxy)
            except ProxyError:
                raise TunnelError("invalid_proxy_settings") from None
            # Validate root before resolving any explicitly supplied credential path.
            runtime_platform()
            key_file = self._validate_settings(settings, endpoint_url, mcp_token)
            self._verify_binary()
            config_file = self._prepare_run(settings, endpoint_url, mcp_token, key_file)
            executable = self._stage_binary()
            # Allowlist, rather than inherit: tunnel environment overrides YAML, and
            # ambient API keys/proxies/debug flags must not change this configuration.
            env = {"PATH": "/usr/bin:/bin", "HOME": str(self._run_dir),
                   "TMPDIR": str(self._run_dir), "LANG": "C.UTF-8",
                   "NO_PROXY": "localhost,127.0.0.1,::1"}
            if proxy.enabled:
                # Official v0.0.16 supports URL/env: proxy values, not file:.
                # Use its control-plane-only setting: explicit proxies ignore
                # NO_PROXY, so a global proxy could intercept local MCP/Bearer.
                # No global/MCP/ambient proxy is passed; failure never retries
                # direct. Proxy Basic credentials exist only in the child env.
                env["CONTROL_PLANE_HTTP_PROXY"] = proxy.authenticated_url()
            async with asyncio.timeout(self.startup_timeout):
                self._process = await asyncio.create_subprocess_exec(
                    str(executable), "run", "--config", str(config_file),
                    stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL, cwd=str(self._run_dir), env=env,
                    start_new_session=True, close_fds=True, pass_fds=(self._file_lock,))
                self._exit_task = asyncio.create_task(self._process.wait())
                self._session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=self.health_timeout), trust_env=False)
                while True:
                    if self._process.returncode is not None:
                        raise TunnelError("process_exited")
                    await self._check_health()
                    if self._healthy:
                        # Liveness permits serving a separate readiness status, even
                        # if the optional startup probes are still pending.
                        break
                    await asyncio.sleep(min(0.1, self.poll_interval))
            self._state = "ready" if self._ready else "running"
            self._last_error_code = None if self._ready else "not_ready"
            self._monitor_task = asyncio.create_task(self._monitor())
            return self.status()
        except asyncio.CancelledError:
            await self._finish_stop_locked()
            self._state, self._last_error_code = "failed", "start_cancelled"
            raise
        except Exception as error:
            code = (error.code if isinstance(error, TunnelError) else
                    "startup_timeout" if isinstance(error, TimeoutError) else "start_failed")
            try:
                await self._finish_stop_locked()
            except TunnelError as cleanup_error:
                code = cleanup_error.code
            self._state, self._last_error_code = "failed", code
            raise TunnelError(code) from None

    def _health_base(self):
        if self._health_url_file is None:
            return None
        try:
            info = self._health_url_file.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_size > 1024 or
                    info.st_uid != os.getuid()):
                return None
            value = self._health_url_file.read_text(encoding="utf-8").strip()
            url = urlsplit(value)
            if (url.scheme != "http" or url.hostname != "127.0.0.1" or not url.port or
                    url.username is not None or url.password is not None or
                    url.path not in ("", "/", "/healthz", "/readyz") or url.query or url.fragment):
                return None
            return "http://127.0.0.1:" + str(url.port)
        except (OSError, ValueError, UnicodeError):
            return None

    async def _check_health(self):
        self._healthy = self._ready = False
        base = self._health_base()
        if base is None or self._session is None:
            return
        for route, attribute in (("/healthz", "_healthy"), ("/readyz", "_ready")):
            try:
                async with self._session.get(base + route, allow_redirects=False, proxy=None) as response:
                    # Only status codes are inspected; no untrusted body is retained.
                    setattr(self, attribute, response.status == 200)
            except (aiohttp.ClientError, TimeoutError, OSError):
                pass
        self._ready = self._healthy and self._ready

    async def _monitor(self):
        try:
            while True:
                try:
                    await asyncio.wait_for(asyncio.shield(self._exit_task), self.poll_interval)
                except TimeoutError:
                    async with self._lock:
                        if self._last_error_code == "stop_failed":
                            # Still own this child after failed TERM/KILL. Wait
                            # for eventual exit without presenting it as ready.
                            continue
                        await self._check_health()
                        self._state = "ready" if self._ready else "running"
                        self._last_error_code = (None if self._ready else
                                                 "not_ready" if self._healthy else "health_unavailable")
                    continue
                async with self._lock:
                    self._process = self._exit_task = None
                    self._healthy = self._ready = False
                    await self._cleanup()
                    self._state, self._last_error_code = "failed", "process_exited"
                return
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # Background failures are sanitized, never logged with raw exceptions.
            self._healthy = self._ready = False
            self._state = "failed"
            self._last_error_code = error.code if isinstance(error, TunnelError) else "health_unavailable"

    async def _stop_locked(self):
        task, self._monitor_task = self._monitor_task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._healthy = self._ready = False
        process = self._process
        if process is not None and process.returncode is None:
            self._state = "stopping"
            try:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(asyncio.shield(self._exit_task), self.stop_timeout)
                except TimeoutError:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    await asyncio.wait_for(asyncio.shield(self._exit_task), self.stop_timeout)
            except (OSError, TimeoutError):
                self._state, self._last_error_code = "failed", "stop_failed"
                # Keep ownership/lock and observe eventual exit. A replacement
                # must never overlap a child which could not be reaped.
                self._monitor_task = asyncio.create_task(self._monitor())
                raise TunnelError("stop_failed") from None
        self._process = self._exit_task = None
        try:
            await self._cleanup()
        except TunnelError as error:
            self._state, self._last_error_code = "failed", error.code
            raise
        self._state = "stopped" if self._enabled else "disabled"
        self._last_error_code = None

    async def _cleanup(self):
        if self._session is not None:
            session, self._session = self._session, None
            await session.close()
        if self._run_dir is not None:
            directory = self._run_dir
            try:
                shutil.rmtree(directory)
            except FileNotFoundError:
                pass
            except OSError:
                raise TunnelError("cleanup_failed") from None
            self._run_dir = self._health_url_file = None
        if self._file_lock is not None:
            descriptor, self._file_lock = self._file_lock, None
            os.close(descriptor)
