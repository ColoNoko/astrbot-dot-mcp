"""Proxy-only Google DoH regressions. Every socket stays on synthetic loopback."""

import asyncio
import importlib
import json
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
from unittest.mock import AsyncMock, Mock, patch

import aiohttp

ROOT = Path(__file__).resolve().parents[1]
if "astrbot_dot_plugin" not in sys.modules:
    package = types.ModuleType("astrbot_dot_plugin")
    package.__path__ = [str(ROOT)]
    sys.modules[package.__name__] = package
module = importlib.import_module("astrbot_dot_plugin.callback_dns")
transport_module = importlib.import_module("astrbot_dot_plugin.callback_transport")
ProxySettings = importlib.import_module("astrbot_dot_plugin.proxy_config").ProxySettings
CallbackTransport = transport_module.CallbackTransport
EventError = module.EventError
HOST = "callbacks.example.com"
CALLBACK_URL = "https://callbacks.example.com/private-path?token=PRIVATE_SENTINEL"
PROXY = ProxySettings(True, "http://127.0.0.1:3128", "proxy-user", "proxy-secret")


def record(name=HOST, kind=1, data="1.1.1.1"):
    return {"name": name + ".", "type": kind, "TTL": 60, "data": data}


def answer(records=None):
    return {"Status": 0, "TC": False, "RD": True, "RA": True, "AD": False,
            "CD": False, "Question": [{"name": HOST + ".", "type": 1}],
            "Answer": [record()] if records is None else records}


def encode(value):
    return json.dumps(value).encode()


def fake_session(body=None, *, status=200, headers=None, content_length=None,
                 chunks=None, error=None, stream_error=None):
    if body is None:
        body = encode(answer())
    class Content:
        async def iter_chunked(self, size):
            for chunk in chunks if chunks is not None else (body,):
                yield chunk
            if stream_error:
                raise stream_error
    response = types.SimpleNamespace(status=status, content_length=content_length,
        headers={"Content-Type": "application/dns-json"} if headers is None else headers,
        content=Content())
    response_context = AsyncMock()
    response_context.__aenter__.return_value = response
    response_context.__aenter__.side_effect = error
    session = types.SimpleNamespace(get=Mock(return_value=response_context), close=AsyncMock())
    return session, session, response_context


class GoogleAnswerTests(unittest.TestCase):
    def parse(self, value):
        return module.parse_google_answers(encode(value), HOST)

    def test_direct_a_answers_are_deduplicated_numeric_ipv4_entries(self):
        result = self.parse(answer([record(), record(), record(data="8.8.4.4")]))
        self.assertEqual(result, [{"hostname": HOST, "host": ip, "port": 443,
            "family": socket.AF_INET, "proto": socket.IPPROTO_TCP,
            "flags": socket.AI_NUMERICHOST} for ip in ("1.1.1.1", "8.8.4.4")])

    def test_out_of_order_cname_chain_and_case_trailing_dot_normalization(self):
        result = self.parse(answer([record("final.example.net", data="8.8.4.4"),
            record(HOST.upper(), 5, "EDGE.example.net."),
            record("edge.example.net", 5, "final.example.net.")]))
        self.assertEqual([item["host"] for item in result], ["8.8.4.4"])

    def test_recorded_google_callback_response_is_accepted_without_pinned_cdn_ip(self):
        # Public Google JSON observed 2026-10-14; a parser fixture only. These
        # CDN values never enter production resolver/bootstrap configuration.
        host = "connectors.api.openai.com"
        value = {"Status": 0, "TC": False, "RD": True, "RA": True, "AD": False,
            "CD": False, "Question": [{"name": host + ".", "type": 1}],
            "Answer": [{"name": host + ".", "type": 5, "TTL": 30,
                        "data": host + ".cdn.cloudflare.net."},
                       {"name": host + ".cdn.cloudflare.net.", "type": 1, "TTL": 300,
                        "data": "104.18.38.184"},
                       {"name": host + ".cdn.cloudflare.net.", "type": 1, "TTL": 300,
                        "data": "172.64.149.72"}]}
        result = module.parse_google_answers(encode(value), host)
        self.assertEqual([item["host"] for item in result], ["104.18.38.184", "172.64.149.72"])
        self.assertEqual(module.GOOGLE_BOOTSTRAP, "8.8.8.8")

    def test_exact_response_limit_accepts_valid_json_but_next_byte_rejects(self):
        body = encode(answer())
        body += b" " * (module.MAX_DNS_RESPONSE - len(body))
        self.assertEqual(len(module.parse_google_answers(body, HOST)), 1)
        with self.assertRaisesRegex(EventError, "^callback_dns_failed$"):
            module.parse_google_answers(body + b" ", HOST)

    def test_question_status_truncation_and_record_shapes_fail_closed(self):
        changes = [dict(Status=True), dict(Status=2), dict(Status="0"), dict(TC=True),
            dict(TC=0), dict(TC=None), dict(Question=[]), dict(Question=[{}, {}]),
            dict(Question=[{"name": "other.example.com", "type": 1}]),
            dict(Question=[{"name": HOST, "type": 28}]),
            dict(Question=[{"name": HOST, "type": True}]),
            dict(Answer=[]), dict(Answer=[{}]), dict(Answer=[None]),
            dict(Answer=[record()] * 17), dict(Answer=[dict(record(), type=True)]),
            dict(Answer=[dict(record(), type=28)]), dict(Answer=[dict(record(), TTL=True)]),
            dict(Answer=[dict(record(), TTL=-1)]), dict(Answer=[dict(record(), TTL=2**32)]),
            dict(Answer=[dict(record(), name=None)]), dict(Answer=[dict(record(), data=123)]),
            dict(Additional=[record("unrelated.example.net")]), dict(Authority={}),
            dict(RA="true")]
        for change in changes:
            with self.subTest(change=change), self.assertRaisesRegex(EventError, "^callback_dns_failed$"):
                self.parse(dict(answer(), **change))
        for key in ("Status", "TC", "Question", "Answer"):
            value = answer()
            del value[key]
            with self.subTest(missing=key), self.assertRaises(EventError):
                self.parse(value)

    def test_json_rejects_duplicate_keys_nonfinite_invalid_encoding_and_trailing_data(self):
        for body in (b'{"Status":0,"Status":2}', encode(answer()) + b'{}', b'null', b'[]',
                     b'{"Status":NaN}', b'\xff', b'{', b'[' * 2000 + b']' * 2000,
                     b' ' * (module.MAX_DNS_RESPONSE + 1)):
            with self.subTest(body=body[:50]), self.assertRaisesRegex(EventError, "^callback_dns_failed$"):
                module.parse_google_answers(body, HOST)

    def test_all_a_answers_must_be_public_ipv4_even_if_some_are_safe(self):
        for ip in ("127.0.0.1", "10.0.0.1", "192.168.1.1", "169.254.169.254", "100.64.0.1",
                   "192.0.2.1", "0.0.0.0", "224.0.0.1", "240.0.0.1", "::1",
                   "2606:4700:4700::1111", "::ffff:8.8.8.8", "8.8.8.8:443", "010.0.0.1"):
            with self.subTest(ip=ip), self.assertRaisesRegex(EventError, "^callback_dns_blocked$"):
                self.parse(answer([record(), record(data=ip)]))

    def test_rejects_unrelated_answers_conflicting_aliases_cycles_and_incomplete_chains(self):
        cases = [
            [record("unrelated.example.net")],
            [record(), record("unrelated.example.net")],
            [record(), record("unrelated.example.net", 5, "other.example.net.")],
            [record(HOST, 5, HOST + "."), record()],
            [record(HOST, 5, "edge.example.net."), record("edge.example.net", 5, HOST + ".")],
            [record(HOST, 5, "edge.example.net.")],
            [record(HOST, 5, "edge.example.net."), record()],
            [record(HOST, 5, "edge.example.net."), record(HOST, 5, "other.example.net."),
             record("edge.example.net")],
            [record(HOST, 5, "edge.example.net."), record("other.example.net")],
        ]
        for records in cases:
            with self.subTest(records=records), self.assertRaisesRegex(EventError, "^callback_dns_failed$"):
                self.parse(answer(records))

    def test_cname_hop_budget_is_bounded(self):
        def chain(length):
            names = [HOST] + ["edge" + str(i) + ".example.net" for i in range(length)]
            return [record(a, 5, b + ".") for a, b in zip(names, names[1:])] + [record(names[-1])]
        self.assertEqual(len(self.parse(answer(chain(module.MAX_CNAME_HOPS)))), 1)
        with self.assertRaisesRegex(EventError, "^callback_dns_failed$"):
            self.parse(answer(chain(module.MAX_CNAME_HOPS + 1)))

    def test_names_cannot_encode_urls_ips_or_non_dns_labels(self):
        for name in ("https://evil.example/", "8.8.8.8", "evil.example..", "-bad.example",
                     "foo_bar.example", "é.example", "a" * 64 + ".example", "evil.example?token=secret"):
            for key in ("name", "data"):
                row = record(HOST, 5, "edge.example.net.")
                row[key] = name
                with self.subTest(key=key, name=name), self.assertRaisesRegex(EventError, "^callback_dns_failed$"):
                    self.parse(answer([row, record("edge.example.net")]))


class GoogleTransportTests(unittest.IsolatedAsyncioTestCase):
    def transport(self, **options):
        transport = CallbackTransport([HOST], proxy=PROXY, callback_dns="google", **options)
        self.addAsyncCleanup(transport.close)
        return transport

    async def test_default_unchanged_and_bad_modes_or_proxy_disabled_fail_closed(self):
        transport = CallbackTransport([HOST])
        self.addAsyncCleanup(transport.close)
        self.assertEqual(transport.callback_dns, "system")
        for mode in (None, "Google", "https://dns.google", "", [], {}):
            with self.subTest(mode=mode), self.assertRaisesRegex(EventError, "^invalid_callback_dns$"):
                CallbackTransport([HOST], callback_dns=mode)
        for proxy in (None, ProxySettings(), ProxySettings(False, PROXY.url)):
            with self.subTest(proxy=proxy), self.assertRaisesRegex(EventError, "^callback_dns_requires_proxy$"):
                CallbackTransport([HOST], proxy=proxy, callback_dns="google")

    async def test_google_uses_only_fixed_numeric_url_tls_host_explicit_proxy_and_hostname_query(self):
        transport = self.transport()
        session, context, _response = fake_session()
        post = AsyncMock(return_value=(200, b"ok"))
        loop = asyncio.get_running_loop()
        with patch.object(module.aiohttp, "TCPConnector") as connector, \
             patch.object(module.aiohttp, "ClientSession", return_value=context) as client, \
             patch.object(loop, "getaddrinfo", AsyncMock(side_effect=AssertionError("system DNS forbidden"))) as dns, \
             patch.object(transport, "_post_once", post), \
             patch.dict(os.environ, {"HTTPS_PROXY": "http://ambient.invalid:99", "ALL_PROXY": "http://ambient.invalid:98"}):
            self.assertEqual(await transport.post(CALLBACK_URL, b"PRIVATE_BODY", {"Authorization": "Bearer PRIVATE_AUTH",
                "webhook-signature": "PRIVATE_SIGNATURE"}), (200, b"ok"))
        target = session.get.call_args.args[0]
        options = session.get.call_args.kwargs
        self.assertEqual(str(target), "https://8.8.8.8/resolve?name=callbacks.example.com&type=A")
        self.assertEqual(options["server_hostname"], "dns.google")
        self.assertEqual(options["proxy"], PROXY.url)
        self.assertEqual(options["proxy_auth"], aiohttp.BasicAuth(PROXY.username, PROXY.password))
        self.assertEqual(options["headers"], {"Host": "dns.google", "Accept": "application/dns-json",
                                             "Accept-Encoding": "identity"})
        self.assertFalse(options["allow_redirects"])
        self.assertNotIn("ssl", options)
        self.assertNotIn("auth", options)
        self.assertFalse(client.call_args.kwargs["trust_env"])
        self.assertFalse(client.call_args.kwargs["auto_decompress"])
        self.assertIsInstance(client.call_args.kwargs["cookie_jar"], aiohttp.DummyCookieJar)
        self.assertLessEqual(client.call_args.kwargs["timeout"].total, 10)
        self.assertFalse(connector.call_args.kwargs["use_dns_cache"])
        self.assertFalse(connector.call_args.kwargs["force_close"])
        self.assertEqual(connector.call_args.kwargs["limit"], 4)
        self.assertEqual(connector.call_args.kwargs["limit_per_host"], 4)
        self.assertEqual(connector.call_args.kwargs["keepalive_timeout"], 15)
        self.assertLessEqual(options["timeout"].total, 10)
        self.assertEqual(len(client.call_args.kwargs["trace_configs"]), 1)
        dns.assert_not_called()
        self.assertEqual(post.await_args.args[3][0]["host"], "1.1.1.1")
        self.assertEqual(transport.last_transport["dns"]["phase"], "complete")
        dns_detail = transport.last_transport["dns"]
        attempt = transport.last_transport["attempts"][0]
        for detail in (dns_detail, attempt):
            for field in ("started_utc_ms", "started_monotonic_ms", "completed_utc_ms", "completed_monotonic_ms"):
                self.assertIs(type(detail[field]), int)
                self.assertGreater(detail[field], 0)
            self.assertLessEqual(detail["started_monotonic_ms"], detail["completed_monotonic_ms"])
        self.assertLessEqual(dns_detail["completed_monotonic_ms"], attempt["started_monotonic_ms"])
        safe = str(transport.last_transport)
        for secret in (HOST, "1.1.1.1", "8.8.8.8", "PRIVATE_", PROXY.url, PROXY.username, PROXY.password):
            self.assertNotIn(secret, safe)

    async def test_authorization_precedes_query_and_google_never_uses_invalid_hostname(self):
        transport = self.transport()
        with patch.object(module.aiohttp, "ClientSession") as client:
            for url in ("https://evil.example/path", "http://" + HOST, "https://127.0.0.1/path",
                        "https://" + HOST + ":444/path", "https://user:pass@" + HOST,
                        "https://" + HOST + ".", "https://" + HOST + "/#fragment"):
                with self.subTest(url=url), self.assertRaises(EventError):
                    await transport.post(url, b"{}", {})
            with self.assertRaisesRegex(EventError, "^callback_not_allowed$"):
                await transport._addresses("evil.example")
            client.assert_not_called()

    async def test_no_proxy_auth_when_unconfigured(self):
        session, context, _ = fake_session()
        with patch.object(module.aiohttp, "TCPConnector"), \
             patch.object(module.aiohttp, "ClientSession", return_value=context):
            await module.resolve_google(HOST, ProxySettings(True, PROXY.url), timeout=1, detail={})
        self.assertNotIn("proxy_auth", session.get.call_args.kwargs)

    async def test_bad_http_encoding_type_and_size_never_post_or_retry(self):
        cases = [({"status": 302}, "redirect"), ({"status": 503}, "http_status"),
            ({"content_length": 65537}, "response_too_large"),
            ({"chunks": (b"x" * 40000, b"x" * 30000)}, "response_too_large"),
            ({"headers": {"Content-Type": "application/json", "Content-Encoding": "gzip"}}, "invalid_response"),
            ({"headers": {"Content-Type": "text/html"}}, "invalid_response"),
            ({"headers": {}}, "invalid_response"),
            ({"body": b"{"}, "invalid_response"),
            ({"stream_error": aiohttp.ClientPayloadError("PRIVATE_SENTINEL")}, "response_incomplete")]
        for options, error in cases:
            with self.subTest(options=options):
                transport = self.transport()
                session, context, _ = fake_session(**options)
                with patch.object(module.aiohttp, "TCPConnector"), \
                     patch.object(module.aiohttp, "ClientSession", return_value=context), \
                     patch.object(transport, "_post_once", AsyncMock()) as post, \
                     patch.object(transport, "_system_addresses", AsyncMock()) as system:
                    with self.assertRaisesRegex(EventError, "^callback_dns_failed$"):
                        await transport.post(CALLBACK_URL, b"{}", {})
                self.assertEqual(session.get.call_count, 1)
                self.assertEqual(transport.last_transport["dns"]["error"], error)
                self.assertEqual(transport.last_transport["attempts"], [])
                post.assert_not_called()
                system.assert_not_called()
                self.assertEqual(transport._dns_slots._value, 4)

    async def test_network_errors_are_fixed_safe_and_never_fallback_or_callback(self):
        cases = [(asyncio.TimeoutError("PRIVATE_SENTINEL"), "callback_timeout", "timeout"),
            (aiohttp.ClientSSLError(None, ssl.SSLError("PRIVATE_SENTINEL")), "callback_dns_failed", "tls_failed"),
            (aiohttp.ClientHttpProxyError(None, (), status=407, message="PRIVATE_SENTINEL"),
             "callback_dns_failed", "proxy_failed"),
            (aiohttp.ClientConnectionError("PRIVATE_SENTINEL"), "callback_dns_failed", "network_failed"),
            (OSError("PRIVATE_SENTINEL"), "callback_dns_failed", "network_failed")]
        for exception, code, error in cases:
            with self.subTest(code=code, error=error):
                transport = self.transport()
                session, context, _ = fake_session(error=exception)
                with patch.object(module.aiohttp, "TCPConnector"), \
                     patch.object(module.aiohttp, "ClientSession", return_value=context), \
                     patch.object(transport, "_post_once", AsyncMock()) as post:
                    with self.assertRaisesRegex(EventError, "^" + code + "$") as raised:
                        await transport.post(CALLBACK_URL, b"{}", {})
                self.assertTrue(raised.exception.__suppress_context__)
                self.assertEqual(transport.last_transport["dns"]["error"], error)
                self.assertNotIn("PRIVATE_SENTINEL", str(transport.last_transport))
                self.assertEqual(session.get.call_count, 1)
                post.assert_not_called()

    async def test_slot_wait_and_exchange_share_one_total_deadline(self):
        transport = self.transport()
        transport._dns_slots = asyncio.Semaphore(0)
        budgets = []
        async def slow(_host, *, timeout, detail):
            budgets.append(timeout)
            await asyncio.sleep(1)
        with patch.object(transport_module, "DNS_TIMEOUT", 0.3), \
             patch.object(transport._google_resolver, "resolve", side_effect=slow), \
             patch.object(transport, "_post_once", AsyncMock()) as post:
            started = asyncio.get_running_loop().time()
            task = asyncio.create_task(transport.post(CALLBACK_URL, b"{}", {}))
            await asyncio.sleep(0.05)
            transport._dns_slots.release()
            with self.assertRaisesRegex(EventError, "^callback_timeout$"):
                await task
            elapsed = asyncio.get_running_loop().time() - started
        self.assertEqual(len(budgets), 1)
        self.assertLess(budgets[0], 0.28)
        self.assertLess(elapsed, 0.6)
        self.assertEqual(transport.last_transport["dns"]["error"], "timeout")
        self.assertEqual(transport._dns_slots._value, 1)
        self.assertFalse(transport._lookups)
        post.assert_not_called()

    async def test_system_dns_has_separate_observed_timestamps_before_callback(self):
        transport = CallbackTransport([HOST])
        self.addAsyncCleanup(transport.close)
        system_answer = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("1.1.1.1", 443))]
        with patch.object(asyncio.get_running_loop(), "getaddrinfo", AsyncMock(return_value=system_answer)), \
             patch.object(transport, "_post_once", AsyncMock(return_value=(200, b"ok"))):
            await transport.post(CALLBACK_URL, b"{}", {})
        dns_detail = transport.last_transport["dns"]
        attempt = transport.last_transport["attempts"][0]
        self.assertEqual(dns_detail["mode"], "system")
        self.assertEqual(dns_detail["elapsed_ms"], dns_detail["completed_monotonic_ms"] - dns_detail["started_monotonic_ms"])
        self.assertLessEqual(dns_detail["completed_monotonic_ms"], attempt["started_monotonic_ms"])
        self.assertLessEqual(attempt["started_monotonic_ms"], attempt["completed_monotonic_ms"])

    async def test_queue_timeout_never_starts_network(self):
        transport = self.transport()
        transport._dns_slots = asyncio.Semaphore(0)
        with patch.object(transport_module, "DNS_TIMEOUT", 0.01), \
             patch.object(transport._google_resolver, "resolve", AsyncMock()) as resolve:
            with self.assertRaisesRegex(EventError, "^callback_timeout$"):
                await transport.post(CALLBACK_URL, b"{}", {})
        resolve.assert_not_called()
        self.assertEqual(transport._dns_slots._value, 0)
        self.assertFalse(transport._lookups)

    async def test_cancel_releases_slot_and_owner_closes_session_without_late_post(self):
        transport = self.transport()
        entered = asyncio.Event()
        session, context, response = fake_session()
        async def hold():
            entered.set()
            await asyncio.Future()
        response.__aenter__.side_effect = hold
        with patch.object(module.aiohttp, "TCPConnector"), \
             patch.object(module.aiohttp, "ClientSession", return_value=context), \
             patch.object(transport, "_post_once", AsyncMock()) as post:
            task = asyncio.create_task(transport.post(CALLBACK_URL, b"{}", {}))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await asyncio.sleep(0)
        session.close.assert_not_awaited()
        await transport.close()
        session.close.assert_awaited_once()
        self.assertEqual(transport._dns_slots._value, 4)
        self.assertFalse(transport._lookups)
        self.assertEqual(transport.last_transport["dns"]["error"], "cancelled")
        post.assert_not_called()

    async def test_close_cancels_queued_and_active_dns_with_four_reserved_slots(self):
        transport = self.transport()
        started, cancelled = [], []
        async def hold(host, **options):
            started.append(host)
            try:
                await asyncio.Future()
            finally:
                cancelled.append(host)
        with patch.object(transport._google_resolver, "resolve", side_effect=hold), \
             patch.object(transport, "_post_once", AsyncMock()) as post:
            tasks = [asyncio.create_task(transport.post(CALLBACK_URL, b"{}", {})) for _ in range(7)]
            for _ in range(4):
                await asyncio.sleep(0)
            self.assertEqual(len(started), 4)
            self.assertEqual(len(transport._lookups), 7)
            await transport.close()
            results = await asyncio.gather(*tasks, return_exceptions=True)
        self.assertTrue(all(isinstance(value, asyncio.CancelledError) for value in results))
        self.assertEqual(len(cancelled), 4)
        self.assertEqual(transport._dns_slots._value, 4)
        self.assertFalse(transport._lookups)
        post.assert_not_called()
        with self.assertRaisesRegex(EventError, "^callback_stopping$"):
            await transport._addresses(HOST)

    async def test_close_during_successful_dns_result_still_prevents_callback(self):
        transport = self.transport()
        async def closed_result(*args, **options):
            transport._closed = True
            return module.parse_google_answers(encode(answer()), HOST)
        with patch.object(transport._google_resolver, "resolve", side_effect=closed_result), \
             patch.object(transport, "_post_once", AsyncMock()) as post:
            with self.assertRaisesRegex(EventError, "^callback_stopping$"):
                await transport.post(CALLBACK_URL, b"{}", {})
        post.assert_not_called()
        self.assertEqual(transport._dns_slots._value, 4)
        self.assertEqual(transport.last_transport["dns"]["error"], "stopping")

    async def test_ignored_legacy_backend_keeps_google_dns_and_standard_callback(self):
        from test_callback_proxy import fake_session as fake_callback_session
        transport = self.transport(backend="wechat_httpcore")
        dns_session, dns_context, _ = fake_session()
        callback_session, callback_context = fake_callback_session()
        with patch.object(module.aiohttp, "TCPConnector"), \
             patch.object(module.aiohttp, "ClientSession", side_effect=[dns_context, callback_context]), \
             patch.dict(sys.modules, {"httpcore": None, "astrbot_dot_plugin.callback_httpcore": None}):
            self.assertEqual(await transport.post(CALLBACK_URL, b"{}", {}), (200, b"ok"))
        self.assertEqual(dns_session.get.call_count, 1)
        self.assertEqual(callback_session.post.call_count, 1)
        self.assertEqual(str(callback_session.post.call_args.args[0]),
                         CALLBACK_URL.replace(HOST, "1.1.1.1"))
        self.assertEqual(callback_session.post.call_args.kwargs["server_hostname"], HOST)
        self.assertEqual(transport.backend, "aiohttp")
        self.assertEqual(transport.last_transport["backend"], "aiohttp")
        self.assertEqual(transport.last_transport["backend_version"], aiohttp.__version__)


class SyntheticDnsProxy:
    """Terminate CONNECT locally using an ephemeral certificate, never forward."""
    def __init__(self, context=None, *, status=200, body=None, hold=False):
        self.context, self.status, self.hold = context, status, hold
        self.body = encode(answer()) if body is None else body
        self.connects, self.requests, self.errors = [], [], []
        self.tasks, self.writers = set(), set()
        self.request_seen = asyncio.Event()
        self.disconnected = asyncio.Event()

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
            self.connects.append(await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3))
            if self.context is None:
                writer.write(b"HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                return
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
            await writer.start_tls(self.context, ssl_handshake_timeout=3)
            self.requests.append(await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3))
            self.request_seen.set()
            if self.hold:
                await reader.read()
                self.disconnected.set()
                return
            writer.write((f"HTTP/1.1 {self.status} Response\r\nContent-Type: application/json; charset=UTF-8\r\n"
                          f"Content-Length: {len(self.body)}\r\nConnection: close\r\n\r\n").encode() + self.body)
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


class GoogleConnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_connect_is_fixed_numeric_target_auth_proxy_only_no_system_dns(self):
        proxy = await SyntheticDnsProxy().start()
        self.addAsyncCleanup(proxy.close)
        transport = CallbackTransport([HOST], proxy=ProxySettings(True, proxy.url, "test-user", "test-pass"), callback_dns="google")
        self.addAsyncCleanup(transport.close)
        loop = asyncio.get_running_loop()
        with patch.object(loop, "getaddrinfo", AsyncMock(side_effect=AssertionError("No system DNS"))) as dns:
            with self.assertRaisesRegex(EventError, "^callback_dns_failed$"):
                await transport.post(CALLBACK_URL, b"PRIVATE_BODY", {"Authorization": "PRIVATE_AUTH"})
        dns.assert_not_called()
        self.assertEqual(len(proxy.connects), 1)
        connect = proxy.connects[0]
        self.assertTrue(connect.startswith(b"CONNECT 8.8.8.8:443 HTTP/1.1\r\n"))
        self.assertIn(b"Host: dns.google\r\n", connect)
        self.assertIn(b"Proxy-Authorization: Basic dGVzdC11c2VyOnRlc3QtcGFzcw==\r\n", connect)
        self.assertNotIn(HOST.encode(), connect)
        self.assertNotIn(b"PRIVATE_", connect)
        self.assertEqual(proxy.requests, [])
        self.assertEqual(proxy.errors, [])
        self.assertEqual(transport.last_transport["dns"]["connect_status"], 407)


@unittest.skipUnless(shutil.which("openssl"), "OpenSSL needed for ephemeral loopback TLS certificate")
class GoogleTlsTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="google-dns-tls-")
        cls.addClassCleanup(cls.directory.cleanup)
        cls.certificates = {}
        for host in ("dns.google", "wrong.example"):
            cert = Path(cls.directory.name) / (host + ".pem")
            key = Path(cls.directory.name) / (host + ".key")
            subprocess.run([shutil.which("openssl"), "req", "-x509", "-newkey", "rsa:2048",
                "-nodes", "-keyout", str(key), "-out", str(cert), "-days", "1", "-subj", "/CN=" + host,
                "-addext", "subjectAltName=DNS:" + host], check=True, capture_output=True, timeout=20)
            cls.certificates[host] = cert, key

    async def proxy(self, host="dns.google", **options):
        cert, key = self.certificates[host]
        self.sni = []
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        context.set_servername_callback(lambda _socket, name, _context: self.sni.append(name))
        proxy = await SyntheticDnsProxy(context, **options).start()
        self.addAsyncCleanup(proxy.close)
        self.verified = ssl.create_default_context(cafile=str(cert))
        self.assertTrue(self.verified.check_hostname)
        self.assertEqual(self.verified.verify_mode, ssl.CERT_REQUIRED)
        transport = CallbackTransport([HOST], proxy=ProxySettings(True, proxy.url, "test-user", "test-pass"), callback_dns="google")
        self.addAsyncCleanup(transport.close)
        return proxy, transport

    async def test_real_dns_query_has_verified_sni_expected_get_and_no_callback_auth_or_body(self):
        proxy, transport = await self.proxy()
        with patch.object(aiohttp.connector, "_SSL_CONTEXT_VERIFIED", self.verified), \
             patch.object(transport, "_post_once", AsyncMock(return_value=(200, b"ok"))) as post, \
             patch.object(asyncio.get_running_loop(), "getaddrinfo", AsyncMock(side_effect=AssertionError("No DNS"))) as dns:
            self.assertEqual(await transport.post(CALLBACK_URL, b"PRIVATE_BODY", {"Authorization": "PRIVATE_AUTH",
                "webhook-signature": "PRIVATE_SIGNATURE"}), (200, b"ok"))
        dns.assert_not_called()
        self.assertEqual(self.sni, ["dns.google"])
        self.assertEqual(len(proxy.connects), 1)
        self.assertTrue(proxy.connects[0].startswith(b"CONNECT 8.8.8.8:443 HTTP/1.1\r\n"))
        self.assertEqual(len(proxy.requests), 1)
        request = proxy.requests[0]
        self.assertTrue(request.startswith(b"GET /resolve?name=callbacks.example.com&type=A HTTP/1.1\r\n"))
        self.assertIn(b"Host: dns.google\r\n", request)
        self.assertIn(b"Accept: application/dns-json\r\n", request)
        self.assertNotIn(b"Authorization:", request)
        self.assertNotIn(b"PRIVATE_", request)
        self.assertNotIn(b"private-path", request)
        self.assertNotIn(b"webhook", request)
        self.assertEqual(post.await_args.args[3][0]["host"], "1.1.1.1")
        self.assertEqual(proxy.errors, [])

    async def test_wrong_cert_hostname_fails_before_query_and_callback(self):
        proxy, transport = await self.proxy("wrong.example")
        with patch.object(aiohttp.connector, "_SSL_CONTEXT_VERIFIED", self.verified), \
             patch.object(transport, "_post_once", AsyncMock()) as post:
            with self.assertRaisesRegex(EventError, "^callback_dns_failed$"):
                await transport.post(CALLBACK_URL, b"{}", {})
        self.assertEqual(self.sni, ["dns.google"])
        self.assertEqual(proxy.requests, [])
        self.assertEqual(transport.last_transport["dns"]["error"], "tls_failed")
        post.assert_not_called()

    async def test_untrusted_certificate_is_not_accepted(self):
        proxy, transport = await self.proxy()
        with patch.object(transport, "_post_once", AsyncMock()) as post:
            with self.assertRaisesRegex(EventError, "^callback_dns_failed$"):
                await transport.post(CALLBACK_URL, b"{}", {})
        self.assertEqual(proxy.requests, [])
        self.assertEqual(transport.last_transport["dns"]["error"], "tls_failed")
        post.assert_not_called()

    async def test_tls_response_redirect_is_not_followed(self):
        proxy, transport = await self.proxy(status=302)
        with patch.object(aiohttp.connector, "_SSL_CONTEXT_VERIFIED", self.verified), \
             patch.object(transport, "_post_once", AsyncMock()) as post:
            with self.assertRaisesRegex(EventError, "^callback_dns_failed$"):
                await transport.post(CALLBACK_URL, b"{}", {})
        self.assertEqual(len(proxy.connects), 1)
        self.assertEqual(len(proxy.requests), 1)
        self.assertEqual(transport.last_transport["dns"]["error"], "redirect")
        post.assert_not_called()

    async def test_close_shuts_down_real_active_dns_socket_and_never_posts(self):
        proxy, transport = await self.proxy(hold=True)
        with patch.object(aiohttp.connector, "_SSL_CONTEXT_VERIFIED", self.verified), \
             patch.object(transport, "_post_once", AsyncMock()) as post:
            task = asyncio.create_task(transport.post(CALLBACK_URL, b"{}", {}))
            await asyncio.wait_for(proxy.request_seen.wait(), 2)
            await transport.close()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await asyncio.wait_for(proxy.disconnected.wait(), 2)
        self.assertEqual(transport._dns_slots._value, 4)
        self.assertFalse(transport._lookups)
        self.assertEqual(transport.last_transport["dns"]["error"], "stopping")
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
