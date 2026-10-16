"""Real loopback CONNECT/TLS tests for the Google-only reusable DNS pool.

No connection is forwarded, and all certificate/proxy credentials are synthetic.
"""
import asyncio
from pathlib import Path
import shutil
import ssl
import subprocess
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import aiohttp

import test_callback_dns as fixtures

module = fixtures.module
CallbackTransport = fixtures.CallbackTransport
ProxySettings = fixtures.ProxySettings
EventError = fixtures.EventError
HOST = fixtures.HOST
CALLBACK_URL = fixtures.CALLBACK_URL


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.001)


class KeepaliveDnsProxy:
    """Local TLS terminator: fixed responses only, never an actual forwarder."""
    def __init__(self, context, *, bodies=(), disconnect=(), reject_after=None,
                 handshake_delay=0, hold=False, first_response=None):
        self.context = context
        self.bodies = bodies
        self.disconnect = frozenset(disconnect)
        self.reject_after = reject_after
        self.handshake_delay = handshake_delay
        self.first_response = first_response
        self.connects, self.requests, self.errors = [], [], []
        self.tls_count = self.closed_count = self.active = self.max_active = 0
        self.tasks, self.writers = set(), set()
        self.gate = asyncio.Event()
        if not hold:
            self.gate.set()

    async def start(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        self.url = "http://127.0.0.1:" + str(self.server.sockets[0].getsockname()[1])
        return self

    async def close(self):
        self.server.close()
        await self.server.wait_closed()
        for writer in tuple(self.writers):
            writer.close()
        tasks = tuple(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        self.writers.add(writer)
        try:
            self.connects.append(await reader.readuntil(b"\r\n\r\n"))
            if self.reject_after is not None and len(self.connects) > self.reject_after:
                writer.write(b"HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                return
            await asyncio.sleep(self.handshake_delay)
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
            await writer.start_tls(self.context, ssl_handshake_timeout=3)
            self.tls_count += 1
            while True:
                request = await reader.readuntil(b"\r\n\r\n")
                self.requests.append(request)
                index = len(self.requests)
                if index in self.disconnect:
                    return
                if index == 1 and self.first_response is not None:
                    if self.first_response == "oversized_length":
                        wire = b"Content-Length: 65537\r\n\r\n{}"
                    elif self.first_response == "oversized_stream":
                        wire = (b"Transfer-Encoding: chunked\r\n\r\n" +
                                b"9c40\r\n" + b"x" * 40000 + b"\r\n" +
                                b"7530\r\n" + b"x" * 30000 + b"\r\n")
                    elif self.first_response == "truncated":
                        wire = b"Content-Length: 100\r\n\r\n{"
                    elif self.first_response == "bad_json":
                        wire = b"Content-Length: 1\r\n\r\n{"
                    else:
                        raise AssertionError("Unknown fixture response")
                    writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n" + wire)
                    await writer.drain()
                    if self.first_response == "truncated":
                        return
                    if self.first_response != "bad_json":
                        # Keep the malformed/incomplete wire response open until
                        # the client proves that it discarded the whole socket.
                        await reader.read()
                        return
                    continue
                self.active += 1
                self.max_active = max(self.active, self.max_active)
                try:
                    await self.gate.wait()
                    body = (self.bodies[index - 1] if index <= len(self.bodies)
                            else fixtures.encode(fixtures.answer()))
                    writer.write(("HTTP/1.1 200 OK\r\nContent-Type: application/dns-json\r\n"
                                  f"Content-Length: {len(body)}\r\nConnection: keep-alive\r\n"
                                  "Set-Cookie: private-cookie=SECRET; Secure\r\n\r\n").encode() + body)
                    await writer.drain()
                finally:
                    self.active -= 1
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
            self.closed_count += 1
            self.writers.discard(writer)
            self.tasks.discard(task)


@unittest.skipUnless(shutil.which("openssl"), "OpenSSL needed for loopback TLS")
class GoogleDnsReuseTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="google-dns-reuse-")
        cls.addClassCleanup(cls.directory.cleanup)
        cls.cert = Path(cls.directory.name) / "dns.pem"
        cls.key = Path(cls.directory.name) / "dns.key"
        subprocess.run([shutil.which("openssl"), "req", "-x509", "-newkey", "rsa:2048",
            "-nodes", "-keyout", str(cls.key), "-out", str(cls.cert), "-days", "1",
            "-subj", "/CN=dns.google", "-addext", "subjectAltName=DNS:dns.google"],
            check=True, capture_output=True, timeout=20)

    async def setup_proxy(self, **options):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.cert, self.key)
        self.sni = []
        context.set_servername_callback(lambda _socket, name, _context: self.sni.append(name))
        proxy = await KeepaliveDnsProxy(context, **options).start()
        self.addAsyncCleanup(proxy.close)
        verified = ssl.create_default_context(cafile=str(self.cert))
        self.assertTrue(verified.check_hostname)
        self.assertEqual(verified.verify_mode, ssl.CERT_REQUIRED)
        self.enterContext(patch.object(aiohttp.connector, "_SSL_CONTEXT_VERIFIED", verified))
        self.enterContext(patch.object(asyncio.get_running_loop(), "getaddrinfo",
            AsyncMock(side_effect=AssertionError("No external/system DNS"))))
        return proxy, self.transport(proxy)

    def transport(self, proxy, username="fake-user", password="fake-password"):
        transport = CallbackTransport([HOST], callback_dns="google",
            proxy=ProxySettings(True, proxy.url, username, password))
        self.addAsyncCleanup(transport.close)
        return transport

    async def test_cold_warm_uses_one_connect_tls_but_two_gets_and_two_validations(self):
        bodies = [fixtures.encode(fixtures.answer([fixtures.record(data=address)]))
                  for address in ("1.1.1.1", "8.8.4.4")]
        proxy, transport = await self.setup_proxy(bodies=bodies, handshake_delay=0.04)
        details, elapsed = [], []
        with patch.object(transport, "_post_once", AsyncMock(return_value=(200, b"ok"))) as post, \
             patch.object(module, "parse_google_answers", wraps=module.parse_google_answers) as validate:
            for _ in range(2):
                started = asyncio.get_running_loop().time()
                self.assertEqual(await transport.post(CALLBACK_URL, b"PRIVATE_BODY",
                    {"Authorization": "PRIVATE_AUTH", "webhook-signature": "PRIVATE_SIGNATURE"}), (200, b"ok"))
                elapsed.append(round((asyncio.get_running_loop().time() - started) * 1000, 2))
                details.append(dict(transport.last_transport["dns"]))
        self.assertEqual(validate.call_count, 2)
        self.assertEqual([call.args[3][0]["host"] for call in post.await_args_list], ["1.1.1.1", "8.8.4.4"])
        self.assertEqual((len(proxy.connects), proxy.tls_count, len(proxy.requests)), (1, 1, 2))
        self.assertEqual(self.sni, ["dns.google"])
        self.assertEqual([d["connection_reused"] for d in details], [False, True])
        self.assertEqual([d["connections_created"] for d in details], [1, 0])
        self.assertEqual([d["connections_reused"] for d in details], [0, 1])
        self.assertTrue(proxy.connects[0].startswith(b"CONNECT 8.8.8.8:443 HTTP/1.1\r\n"))
        for request in proxy.requests:
            self.assertTrue(request.startswith(b"GET /resolve?name=callbacks.example.com&type=A HTTP/1.1\r\n"))
            self.assertIn(b"Host: dns.google\r\n", request)
            for forbidden in (b"PRIVATE_", b"Authorization:", b"Cookie:", b"private-path", b"webhook"):
                self.assertNotIn(forbidden, request)
        for secret in (HOST, "8.8.8.8", "1.1.1.1", "8.8.4.4", "PRIVATE_", "fake-user", "fake-password", proxy.url):
            self.assertNotIn(secret, str(details))
        self.assertEqual(proxy.errors, [])
        print(f"DoH loopback cold/warm: {elapsed[0]}/{elapsed[1]} ms; CONNECT=1 TLS=1 GET=2 validated=2")

    async def test_reused_connection_never_reuses_old_public_dns_answer(self):
        bodies = [fixtures.encode(fixtures.answer()), fixtures.encode(fixtures.answer([
            fixtures.record(), fixtures.record(data="127.0.0.1")]))]
        proxy, transport = await self.setup_proxy(bodies=bodies)
        with patch.object(transport, "_post_once", AsyncMock(return_value=(200, b"ok"))) as post:
            await transport.post(CALLBACK_URL, b"{}", {})
            with self.assertRaisesRegex(EventError, "^callback_dns_blocked$"):
                await transport.post(CALLBACK_URL, b"{}", {})
        self.assertEqual((len(proxy.connects), proxy.tls_count, len(proxy.requests)), (1, 1, 2))
        self.assertEqual(post.await_count, 1)
        self.assertEqual(transport.last_transport["dns"]["error"], "blocked")
        self.assertTrue(transport.last_transport["dns"]["connection_reused"])

    async def test_close_drops_idle_pool_and_replacement_cannot_reuse_old_credentials(self):
        proxy, transport = await self.setup_proxy()
        self.assertIsNone(transport._google_resolver._session)
        await transport._addresses(HOST)
        session = transport._google_resolver._session
        connector = session.connector
        self.assertTrue(connector._conns)
        self.assertIsNotNone(connector._cleanup_handle)
        with self.assertRaises(AttributeError):
            transport.proxy = ProxySettings(True, proxy.url, "changed", "changed")
        with self.assertRaises(AttributeError):
            transport._google_resolver.proxy = ProxySettings(True, proxy.url)
        await transport.close()
        await until(lambda: proxy.closed_count == 1)
        self.assertTrue(session.closed)
        self.assertTrue(connector.closed)
        self.assertFalse(connector._conns)
        self.assertIsNone(connector._cleanup_handle)
        self.assertFalse(transport._lookups)
        with self.assertRaisesRegex(EventError, "^callback_stopping$"):
            await transport._addresses(HOST)
        replacement = self.transport(proxy, username="replacement-user", password="replacement-password")
        await replacement._addresses(HOST)
        self.assertEqual((len(proxy.connects), proxy.tls_count, len(proxy.requests)), (2, 2, 2))
        self.assertNotEqual(proxy.connects[0], proxy.connects[1])
        self.assertIsNot(replacement._google_resolver._session, session)

    async def test_stale_pooled_tcp_safely_reconnects_only_the_get(self):
        proxy, transport = await self.setup_proxy(disconnect=(2,))
        with patch.object(transport, "_post_once", AsyncMock(return_value=(200, b"ok"))) as post:
            await transport.post(CALLBACK_URL, b"{}", {})
            await transport.post(CALLBACK_URL, b"{}", {})
        self.assertEqual((len(proxy.connects), proxy.tls_count, len(proxy.requests)), (2, 2, 3))
        self.assertEqual(post.await_count, 2)
        detail = transport.last_transport["dns"]
        self.assertTrue(detail["connection_reused"])
        self.assertEqual((detail["connections_created"], detail["connections_reused"]), (1, 1))
        self.assertEqual(detail["error"], None)

    async def test_stale_tcp_and_reconnect_failure_never_fallback_or_send_callback(self):
        proxy, transport = await self.setup_proxy(disconnect=(2,), reject_after=1)
        with patch.object(transport, "_post_once", AsyncMock(return_value=(200, b"ok"))) as post, \
             patch.object(transport, "_system_addresses", AsyncMock(side_effect=AssertionError("No fallback"))) as system:
            await transport.post(CALLBACK_URL, b"{}", {})
            with self.assertRaisesRegex(EventError, "^callback_dns_failed$"):
                await transport.post(CALLBACK_URL, b"{}", {})
        self.assertEqual((len(proxy.connects), proxy.tls_count, len(proxy.requests)), (2, 1, 2))
        self.assertEqual(post.await_count, 1)
        system.assert_not_called()
        self.assertEqual(transport.last_transport["dns"]["error"], "proxy_failed")
        self.assertEqual(transport.last_transport["dns"]["connect_status"], 407)
        self.assertEqual(transport.last_transport["attempts"], [])

    async def test_concurrent_queries_use_at_most_four_live_tls_tunnels(self):
        proxy, transport = await self.setup_proxy(hold=True)
        tasks = [asyncio.create_task(transport._addresses(HOST)) for _ in range(9)]
        try:
            await until(lambda: len(proxy.requests) == 4)
            self.assertEqual((len(proxy.connects), proxy.tls_count, proxy.active), (4, 4, 4))
            self.assertEqual(len(transport._lookups), 9)
            proxy.gate.set()
            results = await asyncio.gather(*tasks)
            self.assertTrue(all(rows[0]["host"] == "1.1.1.1" for rows in results))
            self.assertEqual((len(proxy.connects), proxy.tls_count, len(proxy.requests)), (4, 4, 9))
            self.assertEqual(proxy.max_active, 4)
            self.assertEqual(transport._dns_slots._value, 4)
            self.assertFalse(transport._lookups)
        finally:
            await transport.close()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def test_close_cancels_all_queued_active_queries_and_real_sockets(self):
        proxy, transport = await self.setup_proxy(hold=True)
        with patch.object(transport, "_post_once", AsyncMock()) as post:
            tasks = [asyncio.create_task(transport.post(CALLBACK_URL, b"{}", {})) for _ in range(9)]
            await until(lambda: len(proxy.requests) == 4)
            session = transport._google_resolver._session
            connector = session.connector
            await transport.close()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            self.assertTrue(all(isinstance(result, asyncio.CancelledError) for result in results))
            proxy.gate.set()
            await until(lambda: proxy.closed_count == 4)
        self.assertTrue(session.closed)
        self.assertTrue(connector.closed)
        self.assertFalse(connector._acquired)
        self.assertFalse(connector._conns)
        self.assertFalse(transport._lookups)
        self.assertEqual(transport._dns_slots._value, 4)
        post.assert_not_called()

    async def test_idle_expiry_closes_socket_and_next_query_establishes_new_tls(self):
        proxy, transport = await self.setup_proxy()
        with patch.object(module, "DNS_KEEPALIVE_SECONDS", 0.03):
            await transport._addresses(HOST)
        await until(lambda: proxy.closed_count == 1)
        await transport._addresses(HOST)
        self.assertEqual((len(proxy.connects), proxy.tls_count, len(proxy.requests)), (2, 2, 2))

    async def test_cancelled_query_discards_inflight_socket_and_allows_next_query(self):
        proxy, transport = await self.setup_proxy(hold=True)
        task = asyncio.create_task(transport._addresses(HOST))
        await until(lambda: len(proxy.requests) == 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        session = transport._google_resolver._session
        self.assertFalse(session.closed)
        self.assertFalse(session.connector._acquired)
        self.assertFalse(session.connector._conns)
        self.assertFalse(transport._lookups)
        self.assertEqual(transport._dns_slots._value, 4)
        proxy.gate.set()
        await until(lambda: proxy.closed_count == 1)
        await transport._addresses(HOST)
        self.assertEqual((len(proxy.connects), proxy.tls_count, len(proxy.requests)), (2, 2, 2))

    async def test_partial_oversized_or_truncated_response_discards_entire_stream(self):
        for kind, error in (("oversized_length", "response_too_large"),
                            ("oversized_stream", "response_too_large"),
                            ("truncated", "response_incomplete")):
            with self.subTest(kind=kind):
                proxy, transport = await self.setup_proxy(first_response=kind)
                with patch.object(transport, "_post_once", AsyncMock(return_value=(200, b"ok"))) as post:
                    with self.assertRaisesRegex(EventError, "^callback_dns_failed$"):
                        await transport.post(CALLBACK_URL, b"{}", {})
                    self.assertEqual(transport.last_transport["dns"]["error"], error)
                    post.assert_not_called()
                    await until(lambda: proxy.closed_count == 1)
                    session = transport._google_resolver._session
                    self.assertFalse(session.connector._conns)
                    self.assertFalse(session.connector._acquired)
                    await transport.post(CALLBACK_URL, b"{}", {})
                    post.assert_awaited_once()
                self.assertEqual((len(proxy.connects), proxy.tls_count, len(proxy.requests)), (2, 2, 2))
                self.assertEqual(proxy.errors, [])
                await transport.close()

    async def test_invalid_json_is_rejected_then_next_complete_reply_is_parsed_fresh(self):
        proxy, transport = await self.setup_proxy(first_response="bad_json")
        with patch.object(transport, "_post_once", AsyncMock(return_value=(200, b"ok"))) as post:
            with self.assertRaisesRegex(EventError, "^callback_dns_failed$"):
                await transport.post(CALLBACK_URL, b"{}", {})
            self.assertEqual(transport.last_transport["dns"]["error"], "invalid_response")
            post.assert_not_called()
            # The first response has complete HTTP framing despite invalid JSON.
            # Whether aiohttp retains that socket or not, no bytes may leak into
            # the next response and no earlier DNS result may bypass validation.
            await transport.post(CALLBACK_URL, b"{}", {})
            post.assert_awaited_once()
        self.assertEqual(len(proxy.requests), 2)
        self.assertEqual(transport.last_transport["dns"]["phase"], "complete")

    async def test_cancelled_owner_close_still_closes_existing_idle_tls_pool(self):
        proxy, transport = await self.setup_proxy()
        await transport._addresses(HOST)
        session = transport._google_resolver._session
        connector = session.connector
        cancellation_seen = asyncio.Event()
        async def slow_cancel(*args, **kwargs):
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancellation_seen.set()
                await asyncio.Future()
        with patch.object(transport._google_resolver, "resolve", side_effect=slow_cancel):
            request = asyncio.create_task(transport._addresses(HOST))
            await until(lambda: len(transport._lookups) == 1)
            close = asyncio.create_task(transport.close())
            await asyncio.wait_for(cancellation_seen.wait(), 2)
            close.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await close
            with self.assertRaises(asyncio.CancelledError):
                await request
        await until(lambda: proxy.closed_count == 1)
        self.assertTrue(session.closed)
        self.assertTrue(connector.closed)
        self.assertFalse(connector._conns)
        self.assertIsNone(connector._cleanup_handle)
        self.assertFalse(transport._lookups)

    async def test_standalone_wrapper_closes_its_one_shot_pool(self):
        proxy, _transport = await self.setup_proxy()
        detail = {}
        await module.resolve_google(HOST, ProxySettings(True, proxy.url), timeout=1, detail=detail)
        await until(lambda: proxy.closed_count == 1)
        self.assertEqual(detail["connections_created"], 1)
        self.assertFalse(detail["connection_reused"])


if __name__ == "__main__":
    unittest.main()
