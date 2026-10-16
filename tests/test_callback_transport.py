"""Standard aiohttp callback and old-setting regressions; sockets stay on loopback."""
import asyncio
import importlib
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, patch

import aiohttp

ROOT = Path(__file__).resolve().parents[1]
if "astrbot_dot_plugin" not in sys.modules:
    package = types.ModuleType("astrbot_dot_plugin")
    package.__path__ = [str(ROOT)]
    sys.modules[package.__name__] = package
transport_module = importlib.import_module("astrbot_dot_plugin.callback_transport")
CallbackTransport = transport_module.CallbackTransport
EventError = transport_module.EventError
ProxySettings = transport_module.ProxySettings
HOST = "callbacks.example.com"
URL = "https://callbacks.example.com/a%2fb?q=x%2Fy"


def entry(ip="8.8.8.8", host=HOST):
    return {"hostname": host, "host": ip, "port": 443,
            "family": socket.AF_INET6 if ":" in ip else socket.AF_INET,
            "proto": socket.IPPROTO_TCP, "flags": socket.AI_NUMERICHOST}


def detail():
    return {"errno": None, "exception_category": None, "cause_category": None}


class RetiredCallbackBackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_or_unknown_backend_is_ignored_without_loading_httpcore(self):
        from test_callback_proxy import fake_session
        for legacy in ("wechat_httpcore", "aiohttp", "unknown", None, [], {}, True):
            with self.subTest(legacy=legacy):
                session, context = fake_session()
                with patch.dict(sys.modules, {"httpcore": None, "astrbot_dot_plugin.callback_httpcore": None}):
                    transport = CallbackTransport([HOST], backend=legacy)
                    self.addAsyncCleanup(transport.close)
                    with patch.object(transport, "_addresses", AsyncMock(return_value=[entry()])), \
                         patch.object(aiohttp, "TCPConnector"), \
                         patch.object(aiohttp, "ClientSession", return_value=context):
                        self.assertEqual(await transport.post(URL, b"{}", {}), (200, b"ok"))
                self.assertEqual(transport.backend, "aiohttp")
                self.assertEqual(transport.last_transport["backend"], "aiohttp")
                self.assertEqual(transport.last_transport["backend_version"], aiohttp.__version__)
                self.assertEqual(session.post.call_count, 1)
                self.assertNotIn("wechat_httpcore", str(transport.last_transport))

    async def test_closed_transport_cannot_post_with_ignored_legacy_setting(self):
        transport = CallbackTransport([HOST], backend="wechat_httpcore")
        await transport.close()
        with patch.object(aiohttp, "ClientSession") as client:
            with self.assertRaisesRegex(EventError, "^callback_stopping$"):
                await transport.post(URL, b"{}", {})
        client.assert_not_called()

    def test_removed_backend_is_not_shipped_or_declared_as_dependency(self):
        self.assertFalse((ROOT / "callback_httpcore.py").exists())
        requirements = (ROOT / "requirements.txt").read_text().lower()
        self.assertNotIn("httpcore", requirements)


class FakeConnectProxy:
    """Records CONNECT, terminates TLS locally, and never dials the origin."""
    def __init__(self, context=None, status=200, response=None, stall=None):
        self.context, self.status, self.response, self.stall = context, status, response, stall
        self.connect_requests = []
        self.requests = []
        self.tasks = set()
        self.writers = set()
        self.errors = []
        self.reached = asyncio.Event()
        self.peer_closed = asyncio.Event()

    async def start(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        self.url = "http://127.0.0.1:" + str(self.server.sockets[0].getsockname()[1])
        return self

    async def close(self):
        self.server.close()
        await self.server.wait_closed()
        for writer in tuple(self.writers):
            writer.close()
        pending = tuple(self.tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        self.writers.add(writer)
        try:
            connect = await reader.readuntil(b"\r\n\r\n")
            self.connect_requests.append(connect)
            if self.stall == "connect":
                self.reached.set()
                await reader.read()
                self.peer_closed.set()
                return
            if self.status != 200:
                writer.write((f"HTTP/1.1 {self.status} private-reason-secret\r\n"
                              "Content-Length: 0\r\nConnection: close\r\n\r\n").encode())
                await writer.drain()
                return
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
            if self.stall == "tls":
                self.reached.set()
                await reader.read()
                self.peer_closed.set()
                return
            await writer.start_tls(self.context, ssl_handshake_timeout=3)
            head = await reader.readuntil(b"\r\n\r\n")
            length = next(int(line.split(b":", 1)[1]) for line in head.split(b"\r\n")
                          if line.lower().startswith(b"content-length:"))
            body = await reader.readexactly(length)
            self.requests.append((head, body))
            if self.stall == "response":
                self.reached.set()
                await reader.read()
                self.peer_closed.set()
                return
            if self.stall == "body":
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 20\r\n\r\nx")
                await writer.drain()
                self.reached.set()
                await reader.read()
                self.peer_closed.set()
                return
            writer.write(self.response or b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            await writer.drain()
        except (ssl.SSLError, ConnectionError, asyncio.IncompleteReadError):
            pass
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.errors.append(error)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ssl.SSLError, ConnectionError):
                pass
            self.writers.discard(writer)
            self.tasks.discard(task)



@unittest.skipUnless(shutil.which("openssl"), "OpenSSL needed for ephemeral loopback TLS certificate")
class CallbackTransportSocketTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="aiohttp-proxy-tls-")
        cls.addClassCleanup(cls.directory.cleanup)
        cls.cert = Path(cls.directory.name) / "cert.pem"
        key = Path(cls.directory.name) / "key.pem"
        subprocess.run([shutil.which("openssl"), "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-keyout", str(key), "-out", str(cls.cert), "-days", "1", "-subj", "/CN=" + HOST,
                        "-addext", "subjectAltName=DNS:" + HOST], check=True, capture_output=True, timeout=20)
        cls.server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        cls.server_context.load_cert_chain(cls.cert, key)

    async def proxy(self, **options):
        self.sni = []
        self.server_context.set_servername_callback(lambda _socket, name, _context: self.sni.append(name))
        proxy = await FakeConnectProxy(self.server_context, **options).start()
        self.addAsyncCleanup(proxy.close)
        return proxy


    def transport(self, proxy, host=HOST):
        transport = CallbackTransport([host], proxy=ProxySettings(True, proxy.url, "test-user", "test-pass"))
        self.addAsyncCleanup(transport.close)
        return transport

    async def post(self, transport, host=HOST, ip="8.8.8.8", timeout=2, diagnostic=None):
        context = ssl.create_default_context(cafile=str(self.cert))
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        with patch.object(aiohttp.connector, "_SSL_CONTEXT_VERIFIED", context):
            return await transport._post_once(URL.replace(HOST, host), b'{"type":"verification"}',
                {"webhook-signature": "private-signature"}, [entry(ip, host=host)],
                False, timeout, detail() if diagnostic is None else diagnostic)

    async def test_numeric_connect_original_sni_host_body_auth_and_encoded_target(self):
        proxy = await self.proxy()
        transport = self.transport(proxy)
        # A bogus environment proxy must have no effect on the explicit route.
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://127.0.0.1:1", "ALL_PROXY": "socks5://127.0.0.1:1"}):
            self.assertEqual(await self.post(transport), (200, b"ok"))
        connect = proxy.connect_requests[0]
        self.assertTrue(connect.startswith(b"CONNECT 8.8.8.8:443 HTTP/1.1\r\n"))
        self.assertIn(b"Host: callbacks.example.com\r\n", connect)
        self.assertIn(b"Proxy-Authorization: Basic dGVzdC11c2VyOnRlc3QtcGFzcw==\r\n", connect)
        self.assertNotIn(b"private-signature", connect)
        self.assertEqual(self.sni, [HOST])
        request, body = proxy.requests[0]
        self.assertTrue(request.startswith(b"POST /a%2fb?q=x%2Fy HTTP/1.1\r\n"))
        self.assertIn(b"Host: callbacks.example.com\r\n", request)
        self.assertIn(b"webhook-signature: private-signature\r\n", request)
        self.assertNotIn(b"Proxy-Authorization", request)
        self.assertNotIn(b"test-pass", request)
        self.assertEqual(body, b'{"type":"verification"}')
        self.assertEqual(proxy.errors, [])

    async def test_ipv6_connect_bracketed_and_407_status_redacted(self):
        proxy = await self.proxy(status=407)
        with self.assertRaisesRegex(EventError, "^callback_proxy_failed$") as raised:
            await self.post(self.transport(proxy), ip="2606:4700:4700::1111")
        self.assertEqual(raised.exception.http_status, 407)
        self.assertTrue(proxy.connect_requests[0].startswith(b"CONNECT [2606:4700:4700::1111]:443 HTTP/1.1\r\n"))
        self.assertEqual(proxy.requests, [])
        self.assertEqual(len(proxy.connect_requests), 1)
        self.assertEqual(proxy.errors, [])

    async def test_connect_rejection_preserves_safe_failover_status(self):
        for status in (502, 503, 504):
            with self.subTest(status=status):
                proxy = await self.proxy(status=status)
                with self.assertRaises(EventError) as raised:
                    await self.post(self.transport(proxy))
                self.assertEqual((raised.exception.code, raised.exception.http_status), ("callback_proxy_failed", status))
                self.assertEqual(proxy.requests, [])

    async def test_wrong_certificate_hostname_is_rejected_before_post(self):
        proxy = await self.proxy()
        diagnostic=detail()
        with self.assertRaisesRegex(EventError, "^callback_tls_failed$"):
            await self.post(self.transport(proxy), host="different.example.com", diagnostic=diagnostic)
        self.assertEqual(diagnostic["tls_category"],"certificate_verification")
        # aiohttp may expose only its SSL wrapper, without the inner code.
        self.assertIn(diagnostic["tls_verify_code"], (None, 62))
        self.assertIn(diagnostic["tls_reason"], ("other", "certificate_verify_failed"))
        self.assertEqual(self.sni, ["different.example.com"])
        self.assertEqual(proxy.requests, [])
        self.assertEqual(len(proxy.connect_requests), 1)

    async def test_untrusted_certificate_is_rejected_before_post(self):
        proxy = await self.proxy()
        diagnostic=detail()
        with self.assertRaisesRegex(EventError, "^callback_tls_failed$"):
            await self.transport(proxy)._post_once(URL, b"{}", {}, [entry()], False, 2, diagnostic)
        self.assertEqual(diagnostic["tls_category"],"certificate_verification")
        self.assertTrue(diagnostic["tls_verify_code"] is None or type(diagnostic["tls_verify_code"]) is int)
        self.assertIn(diagnostic["tls_reason"], ("other", "certificate_verify_failed"))
        self.assertEqual(proxy.requests, [])

    async def test_redirect_not_followed(self):
        proxy = await self.proxy(response=b"HTTP/1.1 302 Found\r\nLocation: https://evil.example/path\r\nContent-Length: 0\r\n\r\n")
        with self.assertRaisesRegex(EventError, "^callback_redirect$"):
            await self.post(self.transport(proxy))
        self.assertEqual(len(proxy.connect_requests), 1)
        self.assertEqual(len(proxy.requests), 1)

    async def test_content_length_and_streamed_response_limits(self):
        responses = [b"HTTP/1.1 200 OK\r\nContent-Length: 65537\r\n\r\n",
                     b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n10001\r\n" + b"x" * 65537 + b"\r\n0\r\n\r\n"]
        for response in responses:
            proxy = await self.proxy(response=response)
            with self.assertRaisesRegex(EventError, "^callback_response_too_large$"):
                await self.post(self.transport(proxy))
            self.assertEqual(len(proxy.requests), 1)

    async def test_timeout_before_connect_or_during_tls_closes_socket_without_late_post(self):
        for phase in ("connect", "tls"):
            with self.subTest(phase=phase):
                proxy = await self.proxy(stall=phase)
                transport = self.transport(proxy)
                with self.assertRaisesRegex(EventError, "^callback_timeout$"):
                    await self.post(transport, timeout=0.1)
                await asyncio.wait_for(proxy.peer_closed.wait(), 2)
                self.assertEqual(proxy.requests, [])
                self.assertEqual(len(proxy.connect_requests), 1)

    async def test_cancellation_closes_pre_post_and_post_send_connections(self):
        for phase in ("connect", "tls", "response"):
            with self.subTest(phase=phase):
                proxy = await self.proxy(stall=phase)
                transport = self.transport(proxy)
                task = asyncio.create_task(self.post(transport))
                await asyncio.wait_for(proxy.reached.wait(), 2)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                await asyncio.wait_for(proxy.peer_closed.wait(), 2)
                self.assertEqual(len(proxy.requests), 1 if phase == "response" else 0)
                self.assertEqual(len(proxy.connect_requests), 1)

    async def test_anonymous_proxy_sends_no_proxy_authorization(self):
        proxy = await self.proxy()
        transport = CallbackTransport([HOST], proxy=ProxySettings(True, proxy.url))
        self.addAsyncCleanup(transport.close)
        self.assertEqual(await self.post(transport), (200, b"ok"))
        self.assertNotIn(b"Proxy-Authorization", proxy.connect_requests[0])
        self.assertNotIn(b"Proxy-Authorization", proxy.requests[0][0])

    async def test_proxy_hostname_is_resolved_but_callback_hostname_is_not(self):
        proxy = await self.proxy()
        transport = CallbackTransport([HOST], proxy=ProxySettings(True, proxy.url.replace("127.0.0.1", "localhost")))
        self.addAsyncCleanup(transport.close)
        loop = asyncio.get_running_loop()
        original = loop.getaddrinfo
        with patch.object(loop, "getaddrinfo", wraps=original) as dns:
            self.assertEqual(await self.post(transport), (200, b"ok"))
        hosts = [call.args[0] for call in dns.call_args_list]
        self.assertNotIn(HOST, hosts)
        self.assertTrue(all(host in ("localhost", b"localhost") for host in hosts))
        self.assertTrue(proxy.connect_requests[0].startswith(b"CONNECT 8.8.8.8:443 "))

    async def test_incomplete_response_maps_fixed_error_without_retry(self):
        proxy = await self.proxy(response=b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nx")
        with self.assertRaisesRegex(EventError, "^callback_response_incomplete$"):
            await self.post(self.transport(proxy))
        self.assertEqual(len(proxy.requests), 1)

    async def test_exact_response_limit_is_accepted(self):
        proxy = await self.proxy(response=b"HTTP/1.1 200 OK\r\nContent-Length: 65536\r\n\r\n" + b"x" * 65536)
        status, data = await self.post(self.transport(proxy))
        self.assertEqual(status, 200)
        self.assertEqual(len(data), 65536)

    async def test_parent_safe_failover_only_after_explicit_connect_rejection(self):
        for status, attempts in ((407, 1), (502, 2)):
            with self.subTest(status=status):
                proxy = await self.proxy(status=status)
                transport = transport_module.CallbackTransport([HOST],
                    proxy=ProxySettings(True, proxy.url), backend="wechat_httpcore")
                self.addAsyncCleanup(transport.close)
                entries = [entry(), entry("2606:4700:4700::1111")]
                with patch.object(transport, "_addresses", AsyncMock(return_value=entries)):
                    with self.assertRaisesRegex(EventError, "^callback_proxy_failed$"):
                        await transport.post(URL, b"{}", {})
                self.assertEqual(len(proxy.connect_requests), attempts)
                self.assertEqual(len(transport.last_transport["attempts"]), attempts)
                self.assertEqual(transport.last_transport["backend_version"], aiohttp.__version__)
                self.assertEqual(proxy.requests, [])

    async def test_parent_post_send_timeout_never_replays_alternate_address(self):
        proxy = await self.proxy(stall="response")
        transport = transport_module.CallbackTransport([HOST],
            proxy=ProxySettings(True, proxy.url), backend="wechat_httpcore")
        self.addAsyncCleanup(transport.close)
        context = ssl.create_default_context(cafile=str(self.cert))
        entries = [entry(), entry("2606:4700:4700::1111")]
        with patch.object(transport, "_addresses", AsyncMock(return_value=entries)), \
             patch.object(aiohttp.connector, "_SSL_CONTEXT_VERIFIED", context), \
             patch.object(transport_module, "NETWORK_TIMEOUT", 0.2):
            with self.assertRaisesRegex(EventError, "^callback_timeout$"):
                await transport.post(URL, b"{}", {})
        await asyncio.wait_for(proxy.peer_closed.wait(), 2)
        self.assertEqual(len(proxy.connect_requests), 1)
        self.assertEqual(len(proxy.requests), 1)
        self.assertEqual(len(transport.last_transport["attempts"]), 1)

    async def test_parent_retains_allowlist_and_all_dns_answer_checks(self):
        proxy = await self.proxy()
        transport = transport_module.CallbackTransport([HOST],
            proxy=ProxySettings(True, proxy.url), backend="wechat_httpcore")
        self.addAsyncCleanup(transport.close)
        for url in ("https://evil.example/callback", "http://callbacks.example.com/callback",
                    "https://127.0.0.1/callback", "https://callbacks.example.com:444/callback"):
            with self.subTest(url=url), self.assertRaises(EventError):
                await transport.post(url, b"{}", {})
        entries = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 443)),
                   (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("192.168.1.2", 443))]
        with patch.object(asyncio.get_running_loop(), "getaddrinfo", AsyncMock(return_value=entries)):
            with self.assertRaisesRegex(EventError, "^callback_dns_blocked$"):
                await transport.post(URL, b"{}", {})
        self.assertEqual(proxy.connect_requests, [])

    async def test_timeout_diagnostics_report_only_known_standard_transport_state(self):
        for stall in ("connect", "tls", "response", "body"):
            with self.subTest(stall=stall):
                proxy = await self.proxy(stall=stall)
                transport = self.transport(proxy)
                context = ssl.create_default_context(cafile=str(self.cert))
                with patch.object(transport, "_addresses", AsyncMock(return_value=[entry()])), \
                     patch.object(aiohttp.connector, "_SSL_CONTEXT_VERIFIED", context), \
                     patch.object(transport_module, "NETWORK_TIMEOUT", 0.15):
                    with self.assertRaisesRegex(EventError, "^callback_timeout$"):
                        await transport.post(URL, b"{}", {"webhook-signature": "SECRET_SENTINEL"})
                attempt = transport.last_transport["attempts"][0]
                self.assertEqual(attempt["phase"], "response_body" if stall == "body" else "connection_or_request")
                self.assertIsNone(attempt["connect_status"])
                self.assertIsNone(attempt["request_body_complete"])
                self.assertEqual(attempt["response_status"], 200 if stall == "body" else None)
                self.assertGreaterEqual(attempt["elapsed_ms"], 100)
                self.assertNotIn("SECRET_SENTINEL", str(transport.last_transport))
                self.assertNotIn(HOST, str(transport.last_transport))
                self.assertEqual(len(proxy.connect_requests), 1)
                await asyncio.wait_for(proxy.peer_closed.wait(), 2)


class CallbackDirectLoopbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_test_origin_works_without_proxy_or_ambient_environment(self):
        received = []
        finished = asyncio.Event()

        async def handle(reader, writer):
            try:
                head = await reader.readuntil(b"\r\n\r\n")
                received.append((head, await reader.readexactly(2)))
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
                finished.set()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        origin = "http://127.0.0.1:" + str(server.sockets[0].getsockname()[1])
        self.addAsyncCleanup(server.wait_closed)
        self.addCleanup(server.close)
        transport = transport_module.CallbackTransport(test_callback_origin=origin, backend="wechat_httpcore")
        self.addAsyncCleanup(transport.close)
        with patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:1", "ALL_PROXY": "http://127.0.0.1:1"}):
            self.assertEqual(await transport.post(origin + "/test", b"{}", {}), (200, b"ok"))
        await asyncio.wait_for(finished.wait(), 2)
        self.assertEqual(len(received), 1)
        self.assertTrue(received[0][0].startswith(b"POST /test HTTP/1.1\r\n"))
        self.assertEqual(received[0][1], b"{}")



if __name__ == "__main__":
    unittest.main()
