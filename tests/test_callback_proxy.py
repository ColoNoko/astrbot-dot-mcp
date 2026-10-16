"""Explicit callback proxy regressions; all socket tests stay on loopback."""
import asyncio
import base64
import importlib
import json
from pathlib import Path
import shutil
import socket
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

import aiohttp

ROOT = Path(__file__).resolve().parents[1]
if "astrbot_dot_plugin" not in sys.modules:
    package = types.ModuleType("astrbot_dot_plugin")
    package.__path__ = [str(ROOT)]
    sys.modules[package.__name__] = package
transport_module = importlib.import_module("astrbot_dot_plugin.callback_transport")
proxy_module = importlib.import_module("astrbot_dot_plugin.proxy_config")
CallbackTransport = transport_module.CallbackTransport
EventError = transport_module.EventError
ProxySettings = proxy_module.ProxySettings
HOST = "callbacks.example.com"
URL = "https://callbacks.example.com/a%2fb?q=x%2Fy"
PROXY = ProxySettings(True, "http://127.0.0.1:3128", "proxy-user", "proxy-password")


def entry(ip="8.8.8.8"):
    return {"hostname": HOST, "host": ip, "port": 443,
            "family": socket.AF_INET6 if ":" in ip else socket.AF_INET,
            "proto": socket.IPPROTO_TCP, "flags": socket.AI_NUMERICHOST}


def dns_entry(ip="8.8.8.8"):
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, 443))


def fake_session(status=200, chunks=(b"ok",), error=None):
    class Content:
        async def iter_chunked(self, size):
            for chunk in chunks:
                yield chunk
    response = types.SimpleNamespace(status=status, content_length=None, content=Content())
    response_context = AsyncMock()
    response_context.__aenter__.return_value = response
    response_context.__aenter__.side_effect = error
    session = types.SimpleNamespace(post=Mock(return_value=response_context))
    session_context = AsyncMock()
    session_context.__aenter__.return_value = session
    return session, session_context


class CallbackProxyTests(unittest.IsolatedAsyncioTestCase):
    def transport(self, proxy=PROXY):
        transport = CallbackTransport([HOST], proxy=proxy)
        self.addAsyncCleanup(transport.close)
        return transport

    async def test_proxy_pins_numeric_target_preserves_tls_host_and_encoded_path(self):
        transport = self.transport()
        session, context = fake_session()
        original_headers = {"hOsT": "wrong.example", "webhook-signature": "signed-bytes"}
        with patch.object(transport, "_addresses", AsyncMock(return_value=[entry()])) as addresses, \
             patch.object(transport_module.aiohttp, "TCPConnector") as connector, \
             patch.object(transport_module.aiohttp, "ClientSession", return_value=context) as client:
            self.assertEqual(await transport.post(URL, b"{}", original_headers), (200, b"ok"))
        addresses.assert_awaited_once_with(HOST)
        request_url = session.post.call_args.args[0]
        options = session.post.call_args.kwargs
        self.assertEqual(str(request_url), "https://8.8.8.8/a%2fb?q=x%2Fy")
        self.assertEqual(options["server_hostname"], HOST)
        self.assertEqual(options["headers"].getall("Host"), [HOST])
        self.assertEqual(options["headers"]["webhook-signature"], "signed-bytes")
        self.assertEqual(original_headers["hOsT"], "wrong.example")
        self.assertEqual(options["proxy"], PROXY.url)
        self.assertEqual(options["proxy_auth"], aiohttp.BasicAuth("proxy-user", "proxy-password"))
        self.assertNotIn("Proxy-Authorization", options["headers"])
        self.assertNotIn("ssl", options)
        self.assertFalse(options["allow_redirects"])
        self.assertIsNone(connector.call_args.kwargs["resolver"])
        self.assertFalse(connector.call_args.kwargs["use_dns_cache"])
        self.assertTrue(connector.call_args.kwargs["force_close"])
        self.assertFalse(client.call_args.kwargs["trust_env"])
        self.assertIsInstance(client.call_args.kwargs["cookie_jar"], aiohttp.DummyCookieJar)
        self.assertFalse(client.call_args.kwargs["auto_decompress"])
        self.assertEqual(session.post.call_count, 1)

    async def test_ipv6_connect_target_is_bracketed_without_proxy_auth(self):
        transport = self.transport(ProxySettings(True, "http://127.0.0.1:3128"))
        session, context = fake_session()
        with patch.object(transport, "_addresses", AsyncMock(return_value=[entry("2606:4700:4700::1111")])), \
             patch.object(transport_module.aiohttp, "TCPConnector"), \
             patch.object(transport_module.aiohttp, "ClientSession", return_value=context):
            await transport.post(URL, b"{}", {})
        self.assertEqual(str(session.post.call_args.args[0]),
                         "https://[2606:4700:4700::1111]/a%2fb?q=x%2Fy")
        self.assertNotIn("proxy_auth", session.post.call_args.kwargs)

    async def test_disabled_proxy_keeps_direct_pinned_resolver(self):
        transport = self.transport(ProxySettings(False, PROXY.url, PROXY.username, PROXY.password))
        session, context = fake_session()
        with patch.object(transport, "_addresses", AsyncMock(return_value=[entry()])), \
             patch.object(transport_module.aiohttp, "TCPConnector") as connector, \
             patch.object(transport_module.aiohttp, "ClientSession", return_value=context) as client:
            await transport.post(URL, b"{}", {})
        self.assertEqual(str(session.post.call_args.args[0]), URL)
        self.assertIsInstance(connector.call_args.kwargs["resolver"], transport_module._PinnedResolver)
        self.assertEqual(connector.call_args.kwargs["resolver"].entries, [entry()])
        self.assertFalse(client.call_args.kwargs["trust_env"])
        for option in ("proxy", "proxy_auth", "server_hostname"):
            self.assertNotIn(option, session.post.call_args.kwargs)

    async def test_explicit_none_proxy_retains_disabled_default(self):
        transport = self.transport(None)
        self.assertEqual(transport.proxy, ProxySettings())

    async def test_proxy_failures_are_redacted_and_never_retry_direct(self):
        cases = ((aiohttp.ClientConnectionError("proxy-user:proxy-password@secret-proxy"),
                  "callback_network_failed"),
                 (OSError("proxy-password"), "callback_network_failed"),
                 (asyncio.TimeoutError("proxy-password"), "callback_timeout"),
                 (aiohttp.ClientSSLError(None, ssl.SSLError("proxy-password")), "callback_tls_failed"))
        for failure, code in cases:
            with self.subTest(code=code, kind=type(failure).__name__):
                transport = self.transport()
                session, context = fake_session(error=failure)
                with patch.object(transport, "_addresses", AsyncMock(return_value=[entry()])), \
                     patch.object(transport_module.aiohttp, "TCPConnector"), \
                     patch.object(transport_module.aiohttp, "ClientSession", return_value=context):
                    with self.assertRaises(EventError) as raised:
                        await transport.post(URL, b"{}", {})
                self.assertEqual(str(raised.exception), code)
                self.assertTrue(raised.exception.__suppress_context__)
                self.assertEqual(session.post.call_count, 1)
                self.assertEqual(session.post.call_args.kwargs["proxy"], PROXY.url)

    async def test_proxy_does_not_allow_private_or_mixed_callback_dns(self):
        transport = self.transport()
        loop = asyncio.get_running_loop()
        for values in ([dns_entry("127.0.0.1")],
                       [dns_entry(), dns_entry("192.168.1.2")],
                       [dns_entry("::1")], [dns_entry("169.254.169.254")]):
            with self.subTest(values=values), \
                 patch.object(loop, "getaddrinfo", AsyncMock(return_value=values)), \
                 patch.object(transport_module.aiohttp, "ClientSession") as client:
                with self.assertRaisesRegex(EventError, "^callback_dns_blocked$"):
                    await transport.post(URL, b"{}", {})
                client.assert_not_called()

    async def test_proxy_preserves_url_allowlist_and_https_only(self):
        transport = self.transport()
        for value in ("https://evil.example/events", "http://callbacks.example.com/events",
                      "https://127.0.0.1/events", "https://callbacks.example.com:444/events",
                      "https://user:password@callbacks.example.com/events"):
            with self.subTest(url=value), \
                 patch.object(transport, "_addresses", AsyncMock()) as addresses, \
                 patch.object(transport_module.aiohttp, "ClientSession") as client:
                with self.assertRaises(EventError):
                    await transport.post(value, b"{}", {})
                addresses.assert_not_called()
                client.assert_not_called()

    async def test_proxy_still_blocks_redirect_and_oversized_response(self):
        transport = self.transport()
        for status, chunks, code in ((302, (), "callback_redirect"),
                                     (200, (b"x" * (transport_module.MAX_RESPONSE + 1),),
                                      "callback_response_too_large")):
            with self.subTest(code=code):
                session, context = fake_session(status=status, chunks=chunks)
                with patch.object(transport, "_addresses", AsyncMock(return_value=[entry()])), \
                     patch.object(transport_module.aiohttp, "TCPConnector"), \
                     patch.object(transport_module.aiohttp, "ClientSession", return_value=context):
                    with self.assertRaisesRegex(EventError, "^" + code + "$"):
                        await transport.post(URL, b"{}", {})
                self.assertEqual(session.post.call_count, 1)
                self.assertFalse(session.post.call_args.kwargs["allow_redirects"])

    async def test_proxy_and_loopback_test_origin_cannot_silently_use_direct_mode(self):
        with self.assertRaisesRegex(EventError, "^invalid_test_callback_origin$"):
            CallbackTransport(test_callback_origin="http://127.0.0.1:8080", proxy=PROXY)

    async def test_invalid_proxy_is_rejected_before_network(self):
        with self.assertRaisesRegex(proxy_module.ProxyError, "^invalid_proxy_settings$"):
            CallbackTransport([HOST], proxy=ProxySettings(True, "socks5://127.0.0.1:1080"))

    async def test_event_challenge_and_delivery_both_use_configured_proxy(self):
        events_module = importlib.import_module("astrbot_dot_plugin.events")
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        events = events_module.Events(db, "test-owner", [HOST], proxy=PROXY)
        self.addAsyncCleanup(events.transport.close)
        session, context = fake_session()
        bodies = []

        def respond(_url, **options):
            body = json.loads(options["data"])
            bodies.append(body)
            response_body = (json.dumps({"challenge": body["challenge"]}).encode()
                             if body.get("type") == "verification" else b"")
            response_session, _context = fake_session(chunks=(response_body,))
            return response_session.post()

        session.post.side_effect = respond
        secret = "whsec_" + base64.b64encode(b"s" * 32).decode()
        with patch.object(events.transport, "_addresses", AsyncMock(return_value=[entry()])), \
             patch.object(transport_module.aiohttp, "TCPConnector"), \
             patch.object(transport_module.aiohttp, "ClientSession", return_value=context):
            await events.subscribe({"name": "qq.message", "arguments": {},
                                    "delivery": {"mode": "webhook", "url": URL, "secret": secret}})
            db.execute("BEGIN IMMEDIATE")
            self.assertEqual(events.enqueue("msg_test", int(time.time())), 1)
            db.commit()
            self.assertTrue(await events.deliver_once())
        self.assertEqual(bodies[0]["type"], "verification")
        self.assertEqual(bodies[1]["name"], "qq.message")
        self.assertEqual(events.status()["deliveries"], {"acknowledged": 1})
        self.assertEqual(session.post.call_count, 2)
        for request in session.post.call_args_list:
            self.assertEqual(request.args[0].host, "8.8.8.8")
            self.assertEqual(request.kwargs["proxy"], PROXY.url)
            self.assertEqual(request.kwargs["proxy_auth"].login, PROXY.username)
            self.assertEqual(request.kwargs["server_hostname"], HOST)
            self.assertIn("webhook-signature", request.kwargs["headers"])


class LoopbackConnectProxy:
    """Fake proxy terminating test TLS locally; it never opens a remote socket."""
    def __init__(self, tls_context=None, connect_status=200, response_status=200):
        self.tls_context = tls_context
        self.connect_status = connect_status
        self.response_status = response_status
        self.connect_requests = []
        self.callback_requests = []
        self.tasks = set()
        self.writers = set()
        self.errors = []

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
            connect = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3)
            self.connect_requests.append(connect)
            if self.connect_status != 200:
                writer.write((f"HTTP/1.1 {self.connect_status} Proxy rejected\r\n"
                              "Content-Length: 0\r\nConnection: close\r\n\r\n").encode())
                await writer.drain()
                return
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
            await writer.start_tls(self.tls_context, ssl_handshake_timeout=3)
            request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3)
            length = next(int(line.split(b":", 1)[1]) for line in request.split(b"\r\n")
                          if line.lower().startswith(b"content-length:"))
            body = await reader.readexactly(length)
            self.callback_requests.append((request, body))
            writer.write((f"HTTP/1.1 {self.response_status} Response\r\n"
                          "Content-Length: 2\r\nConnection: close\r\n\r\nok").encode())
            await writer.drain()
        except (ssl.SSLError, ConnectionError, asyncio.IncompleteReadError):
            # Expected when testing certificate rejection and disconnects.
            pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.errors.append(exc)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ssl.SSLError, ConnectionError):
                pass
            self.writers.discard(writer)
            self.tasks.discard(task)


class CallbackProxySocketTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_connect_uses_numeric_authority_and_auth_without_direct_fallback(self):
        proxy = await LoopbackConnectProxy(connect_status=407).start()
        self.addAsyncCleanup(proxy.close)
        transport = CallbackTransport([HOST], proxy=ProxySettings(True, proxy.url, "test-user", "test-pass"))
        self.addAsyncCleanup(transport.close)
        loop = asyncio.get_running_loop()
        with patch.object(loop, "getaddrinfo", AsyncMock(return_value=[dns_entry()])) as dns:
            with self.assertRaisesRegex(EventError, "^callback_proxy_failed$") as raised:
                await transport.post(URL, b"{}", {})
            self.assertEqual(raised.exception.http_status, 407)
        dns.assert_awaited_once()
        self.assertEqual(len(proxy.connect_requests), 1)
        connect = proxy.connect_requests[0]
        self.assertTrue(connect.startswith(b"CONNECT 8.8.8.8:443 HTTP/1.1\r\n"))
        self.assertIn(b"Host: callbacks.example.com\r\n", connect)
        self.assertIn(b"Proxy-Authorization: Basic dGVzdC11c2VyOnRlc3QtcGFzcw==\r\n", connect)
        self.assertEqual(proxy.callback_requests, [])
        self.assertEqual(proxy.errors, [])


@unittest.skipUnless(shutil.which("openssl"), "OpenSSL needed for ephemeral loopback TLS certificate")
class CallbackProxyTlsTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="callback-proxy-tls-")
        cls.addClassCleanup(cls.directory.cleanup)
        cls.cert = Path(cls.directory.name) / "cert.pem"
        cls.key = Path(cls.directory.name) / "key.pem"
        subprocess.run([shutil.which("openssl"), "req", "-x509", "-newkey", "rsa:2048",
                        "-nodes", "-keyout", str(cls.key), "-out", str(cls.cert), "-days", "1",
                        "-subj", "/CN=" + HOST, "-addext", "subjectAltName=DNS:" + HOST],
                       check=True, capture_output=True, timeout=20)

    async def tls_proxy(self, response_status=200):
        self.sni = []
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(self.cert, self.key)
        server_context.set_servername_callback(lambda _socket, name, _context: self.sni.append(name))
        proxy = await LoopbackConnectProxy(server_context, response_status=response_status).start()
        self.addAsyncCleanup(proxy.close)
        return proxy

    async def post(self, proxy, host=HOST):
        transport = CallbackTransport([host], proxy=ProxySettings(True, proxy.url, "test-user", "test-pass"))
        self.addAsyncCleanup(transport.close)
        verified_context = ssl.create_default_context(cafile=str(self.cert))
        self.assertTrue(verified_context.check_hostname)
        self.assertEqual(verified_context.verify_mode, ssl.CERT_REQUIRED)
        loop = asyncio.get_running_loop()
        with patch.object(loop, "getaddrinfo", AsyncMock(return_value=[dns_entry()])) as dns, \
             patch.object(aiohttp.connector, "_SSL_CONTEXT_VERIFIED", verified_context):
            result = await transport.post(URL.replace(HOST, host), b'{"type":"verification"}',
                                          {"webhook-signature": "test-signature"})
        dns.assert_awaited_once()
        return result

    async def test_real_connect_tls_sni_host_signature_body_and_no_auth_leak(self):
        proxy = await self.tls_proxy()
        self.assertEqual(await self.post(proxy), (200, b"ok"))
        self.assertEqual(self.sni, [HOST])
        self.assertEqual(len(proxy.connect_requests), 1)
        self.assertTrue(proxy.connect_requests[0].startswith(b"CONNECT 8.8.8.8:443 HTTP/1.1\r\n"))
        request, body = proxy.callback_requests[0]
        self.assertTrue(request.startswith(b"POST /a%2fb?q=x%2Fy HTTP/1.1\r\n"))
        self.assertIn(b"Host: callbacks.example.com\r\n", request)
        self.assertIn(b"webhook-signature: test-signature\r\n", request)
        self.assertNotIn(b"Proxy-Authorization", request)
        self.assertNotIn(b"test-pass", request)
        self.assertNotIn(b"test-signature", proxy.connect_requests[0])
        self.assertEqual(body, b'{"type":"verification"}')
        self.assertEqual(proxy.errors, [])

    async def test_real_connect_rejects_wrong_target_certificate_hostname(self):
        proxy = await self.tls_proxy()
        with self.assertRaisesRegex(EventError, "^callback_tls_failed$"):
            await self.post(proxy, "different.example.com")
        self.assertEqual(self.sni, ["different.example.com"])
        self.assertEqual(len(proxy.connect_requests), 1)
        self.assertEqual(proxy.callback_requests, [])
        self.assertEqual(proxy.errors, [])

    async def test_real_connect_redirect_is_not_followed(self):
        proxy = await self.tls_proxy(response_status=302)
        with self.assertRaisesRegex(EventError, "^callback_redirect$"):
            await self.post(proxy)
        self.assertEqual(len(proxy.connect_requests), 1)
        self.assertEqual(len(proxy.callback_requests), 1)
        self.assertEqual(proxy.errors, [])


if __name__ == "__main__":
    unittest.main()
