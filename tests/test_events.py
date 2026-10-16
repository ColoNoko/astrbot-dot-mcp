"""Offline Events and callback transport regressions; only loopback HTTP is used."""
import asyncio
import base64
import hashlib
import hmac
import importlib
import json
from pathlib import Path
import socket
import sqlite3
import sys
import time
import types
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp import web
from standardwebhooks import Webhook

ROOT = Path(__file__).resolve().parents[1]
if "astrbot_dot_plugin" not in sys.modules:
    package = types.ModuleType("astrbot_dot_plugin")
    package.__path__ = [str(ROOT)]
    sys.modules[package.__name__] = package
events_module = importlib.import_module("astrbot_dot_plugin.events")
transport_module = importlib.import_module("astrbot_dot_plugin.callback_transport")
signing_module = importlib.import_module("astrbot_dot_plugin.webhook_signing")
Events, EventError = events_module.Events, events_module.EventError
CallbackTransport = transport_module.CallbackTransport
SECRET = "whsec_" + base64.b64encode(b"s" * 32).decode()
OTHER_SECRET = "whsec_" + base64.b64encode(b"t" * 32).decode()
URL = "https://callbacks.example.com/events"
MID = "msg_" + "1" * 32


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.status = 204
        self.challenge_mode = "echo"
        self.error = None
        self.block = None
        self.started = asyncio.Event()

    def validate(self, url):
        return CallbackTransport(["callbacks.example.com"]).validate(url)

    async def post(self, url, body, headers):
        self.calls.append((url, body, headers))
        value = json.loads(body)
        self.started.set()
        if self.block:
            await self.block.wait()
        if self.error:
            raise EventError(self.error)
        if value.get("type") == "verification":
            challenge = value["challenge"] if self.challenge_mode == "echo" else "wrong"
            return 200, json.dumps({"challenge": challenge}).encode()
        return self.status, b""

    async def close(self):
        pass


def subscribe_params(secret=SECRET, **extra):
    return {"name": "qq.message", "arguments": {},
            "delivery": {"mode": "webhook", "url": URL, "secret": secret}, **extra}


def unsubscribe_params():
    return {"name": "qq.message", "arguments": {},
            "delivery": {"mode": "webhook", "url": URL}}


class EventsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.events = Events(self.db, "owner", ["callbacks.example.com"])
        await self.events.transport.close()
        self.transport = FakeTransport()
        self.events.transport = self.transport
        self.now = int(time.time())
        self.events.clock = lambda: self.now

    async def asyncTearDown(self):
        await self.events.transport.close()
        self.db.close()

    def enqueue(self, mid=MID):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            count = self.events.enqueue(mid, self.now)
            self.db.commit()
            return count
        except BaseException:
            self.db.rollback()
            raise

    async def test_documented_flow_exact_signature_idempotence_and_no_message_text(self):
        result = await self.events.subscribe(subscribe_params())
        self.assertEqual(set(result), {"id", "refreshBefore", "cursor", "truncated"})
        self.assertIsNone(result["cursor"])
        self.assertFalse(result["truncated"])
        url, body, headers = self.transport.calls[0]
        verification = Webhook(SECRET).verify(body, headers)
        self.assertEqual(verification["type"], "verification")
        self.assertEqual(headers["X-MCP-Subscription-Id"], result["id"])
        repeat = await self.events.subscribe(subscribe_params())
        self.assertEqual(result["id"], repeat["id"])
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.enqueue(), 1)
        self.assertEqual(self.enqueue(), 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM event_deliveries").fetchone()[0], 1)
        self.assertTrue(await self.events.deliver_once())
        self.assertFalse(await self.events.deliver_once())
        _, body, headers = self.transport.calls[-1]
        event = Webhook(SECRET).verify(body, headers)
        self.assertEqual(set(event), {"eventId", "name", "timestamp", "data", "cursor"})
        self.assertEqual(event["data"], {"message_id": MID})
        self.assertEqual(event["eventId"], headers["webhook-id"])
        self.assertEqual(event["name"], "qq.message")
        self.assertEqual(self.events.status()["deliveries"], {"acknowledged": 1})

    async def test_failed_challenge_never_activates_subscription(self):
        self.transport.challenge_mode = "wrong"
        with self.assertRaisesRegex(EventError, "challenge_failed"):
            await self.events.subscribe(subscribe_params())
        self.assertEqual(self.events.status()["active_subscriptions"], 0)
        first = json.loads(self.transport.calls[0][1])["challenge"]
        with self.assertRaises(EventError):
            await self.events.subscribe(subscribe_params())
        self.assertNotEqual(first, json.loads(self.transport.calls[1][1])["challenge"])

    async def test_challenge_response_cannot_be_replayed_after_cache_expiry(self):
        await self.events.subscribe(subscribe_params(ttlMs=None))
        saved = json.loads(self.transport.calls[0][1])["challenge"]
        self.now += events_module.VERIFICATION_SECONDS + 1
        async def replay(url, body, headers):
            return 200, json.dumps({"challenge": saved}).encode()
        self.transport.post = replay
        with self.assertRaisesRegex(EventError, "challenge_failed"):
            await self.events.subscribe(subscribe_params(ttlMs=None))

    async def test_cached_refresh_does_not_extend_verification_forever(self):
        await self.events.subscribe(subscribe_params(ttlMs=None))
        for _ in range(2):
            self.now += 100
            await self.events.subscribe(subscribe_params(ttlMs=None))
        self.assertEqual(len(self.transport.calls), 1)
        self.now += 101
        await self.events.subscribe(subscribe_params(ttlMs=None))
        self.assertEqual(len(self.transport.calls), 2)

    async def test_rotation_signs_with_both_keys_then_drops_old_secret(self):
        sid = (await self.events.subscribe(subscribe_params(ttlMs=None)))["id"]
        result = await self.events.subscribe(subscribe_params(OTHER_SECRET, ttlMs=None))
        self.assertEqual(result["id"], sid)
        self.enqueue()
        await self.events.deliver_once()
        _, body, headers = self.transport.calls[-1]
        self.assertEqual(len(headers["webhook-signature"].split()), 2)
        Webhook(SECRET).verify(body, headers)
        Webhook(OTHER_SECRET).verify(body, headers)
        self.now += events_module.ROTATION_SECONDS + 1
        self.enqueue("msg_" + "2" * 32)
        await self.events.deliver_once()
        self.assertEqual(len(self.transport.calls[-1][2]["webhook-signature"].split()), 1)
        self.assertIsNone(self.db.execute("SELECT old_secret FROM event_subscriptions").fetchone()[0])

    async def test_transient_retries_preserve_bytes_and_id_with_fresh_signature(self):
        await self.events.subscribe(subscribe_params())
        self.enqueue()
        self.transport.status = 503
        await self.events.deliver_once()
        first = self.transport.calls[-1]
        self.assertFalse(await self.events.deliver_once())
        self.now += 3
        self.transport.status = 200
        await self.events.deliver_once()
        second = self.transport.calls[-1]
        self.assertEqual(first[1], second[1])
        self.assertEqual(first[2]["webhook-id"], second[2]["webhook-id"])
        self.assertNotEqual(first[2]["webhook-timestamp"], second[2]["webhook-timestamp"])
        self.assertNotEqual(first[2]["webhook-signature"], second[2]["webhook-signature"])
        self.assertEqual(self.events.status()["deliveries"], {"acknowledged": 1})

    async def test_new_network_categories_preserve_bounded_delivery_queue_retries(self):
        await self.events.subscribe(subscribe_params(ttlMs=None))
        self.enqueue()
        for code in ('callback_proxy_connect_failed','callback_server_disconnected',
                     'callback_connection_reset','callback_response_incomplete'):
            self.transport.error=code
            self.assertTrue(await self.events.deliver_once())
            row=self.db.execute('SELECT status,error FROM event_deliveries').fetchone()
            self.assertEqual(row,('retry',code))
            self.now += 301
        self.transport.error=None
        self.transport.status=200
        self.assertTrue(await self.events.deliver_once())
        self.assertEqual(self.events.status()['deliveries'],{'acknowledged':1})

    async def test_active_subscription_limit_preserves_existing(self):
        from copy import deepcopy
        for number in range(events_module.MAX_ACTIVE_SUBSCRIPTIONS):
            params=subscribe_params(ttlMs=None);params['delivery']['url']+=str(number)
            await self.events.subscribe(params)
        params=subscribe_params(ttlMs=None);params['delivery']['url']+='extra'
        with self.assertRaisesRegex(EventError,'subscription_limit_reached'):
            await self.events.subscribe(params)
        self.assertEqual(self.events.status()['active_subscriptions'],8)

    async def test_retry_exhaustion_and_terminal_statuses(self):
        await self.events.subscribe(subscribe_params(ttlMs=None))
        self.enqueue()
        self.transport.status = 429
        for attempt in range(events_module.MAX_ATTEMPTS):
            self.assertTrue(await self.events.deliver_once())
            self.now += 301
        row = self.db.execute("SELECT status,attempts,error FROM event_deliveries").fetchone()
        self.assertEqual(row, ("failed", events_module.MAX_ATTEMPTS, "retry_exhausted"))
        self.assertFalse(await self.events.deliver_once())
        for index, status in enumerate((400, 413, 410)):
            self.enqueue("msg_" + str(index + 3) * 32)
            self.transport.status = status
            await self.events.deliver_once()
            self.assertFalse(await self.events.deliver_once())
        self.assertEqual(self.events.status()["active_subscriptions"], 0)

    async def test_expiration_unsubscribe_and_no_replay(self):
        result = await self.events.subscribe(subscribe_params(ttlMs=1))
        self.assertIsNotNone(result["refreshBefore"])
        self.enqueue()
        self.now += 1
        self.assertFalse(await self.events.deliver_once())
        self.assertEqual(self.events.status()["deliveries"], {"cancelled": 1})
        with self.assertRaisesRegex(EventError, "replay_not_supported"):
            await self.events.subscribe(subscribe_params(cursor="old"))
        result = await self.events.subscribe(subscribe_params(ttlMs=None))
        self.assertIsNone(result["refreshBefore"])
        self.enqueue("msg_" + "2" * 32)
        self.assertEqual(await self.events.unsubscribe(unsubscribe_params()), {})
        self.assertEqual(await self.events.unsubscribe(unsubscribe_params()), {})
        self.assertFalse(await self.events.deliver_once())
        self.assertEqual(self.db.execute("SELECT secret,old_secret FROM event_subscriptions").fetchone(), ("", None))

    async def test_cancelled_delivery_lease_recovery_is_bounded(self):
        await self.events.subscribe(subscribe_params())
        self.enqueue()
        self.transport.started.clear()
        self.transport.block = asyncio.Event()
        pending = asyncio.create_task(self.events.deliver_once())
        await self.transport.started.wait()
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        first = self.transport.calls[-1]
        self.assertEqual(self.events.status()["deliveries"], {"sending": 1})
        self.now += events_module.LEASE_SECONDS + 1
        self.transport.block = None
        self.assertTrue(await self.events.deliver_once())
        self.assertEqual(first[1], self.transport.calls[-1][1])
        self.assertEqual(self.events.status()["deliveries"], {"acknowledged": 1})

    async def test_transaction_atomicity_and_principal_isolation(self):
        await self.events.subscribe(subscribe_params())
        with self.assertRaisesRegex(EventError, "event_transaction_required"):
            self.events.enqueue(MID, self.now)
        self.db.execute("BEGIN")
        self.events.enqueue(MID, self.now)
        self.db.rollback()
        self.assertEqual(self.db.execute("SELECT count(*) FROM event_deliveries").fetchone()[0], 0)
        other = Events(self.db, "other-owner", ["callbacks.example.com"])
        await other.unsubscribe(unsubscribe_params())
        self.assertEqual(other.status()["active_subscriptions"], 0)
        self.assertEqual(self.events.status()["active_subscriptions"], 1)
        await other.transport.close()

    async def test_invalid_params_do_not_trigger_network(self):
        for change in ({"arguments": {"recipient": "someone"}}, {"ttlMs": True},
                       {"ttlMs": 0}, {"ttlMs": -1}, {"ttlMs": "100"},
                       {"extra": "bad"}, {"name": "other.event"}):
            with self.subTest(change=change), self.assertRaises(EventError):
                await self.events.subscribe(subscribe_params(**change))
        for secret in ("plain", "whsec_!", "whsec_" + base64.b64encode(b"short").decode()):
            with self.assertRaises(EventError):
                await self.events.subscribe(subscribe_params(secret))
        self.assertEqual(self.transport.calls, [])


class CallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_url_ssrf_allowlist_and_special_addresses(self):
        transport = CallbackTransport(["callbacks.example.com"])
        for url in ("http://callbacks.example.com/", "https://127.0.0.1/", "https://[::1]/",
                    "https://callbacks.example.com:444/", "https://evil.example.com/",
                    "https://callbacks.example.com.evil.test/", "https://user@callbacks.example.com/",
                    "https://callbacks.example.com/#fragment", "https://callbacks.example.com/\n",
                    "https://callbacks.example.com\\@127.0.0.1/", "https://callbacks%2eexample.com/",
                    "https://callbacks.example.com./", "https://callbacks.example.com/\x7f"):
            with self.subTest(url=url), self.assertRaises(EventError):
                transport.validate(url)
        transport.validate(URL)
        for value in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "100.64.0.1", "0.0.0.0",
                      "192.168.1.1", "::1", "fe80::1", "fc00::1", "::ffff:8.8.8.8", "ff02::1",
                      "2002:0808:0808::", "192.0.2.1"):
            with self.subTest(value=value), self.assertRaises(EventError):
                transport_module._public_ip(value)
        self.assertEqual(str(transport_module._public_ip("8.8.8.8")), "8.8.8.8")
        for values in ([[]], [None], "callbacks.example.com", ["*.example.com"]):
            with self.assertRaises(EventError):
                CallbackTransport(values)
        await transport.close()

    async def test_dns_pinning_rejects_mixed_public_private_and_wrong_port(self):
        transport = CallbackTransport(["callbacks.example.com"])
        loop = asyncio.get_running_loop()
        good = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 443))]
        with patch.object(loop, "getaddrinfo", AsyncMock(return_value=good)) as dns:
            entries = await transport._addresses("callbacks.example.com")
            self.assertEqual(dns.await_count, 1)
        resolver = transport_module._PinnedResolver("callbacks.example.com", entries)
        self.assertEqual((await resolver.resolve("callbacks.example.com", 443))[0]["host"], "8.8.8.8")
        for host, port in (("evil.example.com", 443), ("callbacks.example.com", 80)):
            with self.assertRaises(EventError):
                await resolver.resolve(host, port)
        for response in (good + [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))],
                         [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 80))]):
            with patch.object(loop, "getaddrinfo", AsyncMock(return_value=response)):
                with self.assertRaisesRegex(EventError, "callback_dns_blocked"):
                    await transport._addresses("callbacks.example.com")
        await transport.close()

    async def test_transport_uses_validated_resolver_no_proxy_redirect_or_cookies(self):
        transport = CallbackTransport(["callbacks.example.com"])
        entries = [{"hostname": "callbacks.example.com", "host": "8.8.8.8", "port": 443,
                    "family": socket.AF_INET, "proto": 6, "flags": socket.AI_NUMERICHOST}]
        class Content:
            async def iter_chunked(self, size):
                yield b"ok"
        response = types.SimpleNamespace(status=200, content_length=2, content=Content())
        response_context = AsyncMock()
        response_context.__aenter__.return_value = response
        session = types.SimpleNamespace(post=unittest.mock.Mock(return_value=response_context))
        session_context = AsyncMock()
        session_context.__aenter__.return_value = session
        with patch.object(transport, "_addresses", AsyncMock(return_value=entries)) as addresses, \
             patch.object(transport_module.aiohttp, "TCPConnector") as connector, \
             patch.object(transport_module.aiohttp, "ClientSession", return_value=session_context) as client:
            self.assertEqual(await transport.post(URL, b"{}", {}), (200, b"ok"))
            self.assertEqual(addresses.await_count, 1)
            options = connector.call_args.kwargs
            self.assertFalse(options["use_dns_cache"])
            self.assertTrue(options["force_close"])
            self.assertEqual(options["resolver"].entries, entries)
            self.assertFalse(client.call_args.kwargs["trust_env"])
            self.assertFalse(session.post.call_args.kwargs["allow_redirects"])
            self.assertEqual(str(session.post.call_args.args[0]), URL)
        await transport.close()

    async def test_closed_transport_cannot_start_queued_dns_or_post_after_dns(self):
        transport = CallbackTransport(["callbacks.example.com"])
        transport._dns_slots = asyncio.Semaphore(0)
        loop = asyncio.get_running_loop()
        with patch.object(loop, "getaddrinfo", AsyncMock()) as dns:
            pending = asyncio.create_task(transport._addresses("callbacks.example.com"))
            await asyncio.sleep(0)
            await transport.close()
            transport._dns_slots.release()
            with self.assertRaisesRegex(EventError, "callback_stopping"):
                await pending
            dns.assert_not_called()
        transport = CallbackTransport(["callbacks.example.com"])
        async def closed_dns(_host):
            await transport.close()
            return []
        with patch.object(transport, "_addresses", closed_dns), \
             patch.object(transport_module.aiohttp, "ClientSession") as client:
            with self.assertRaisesRegex(EventError, "callback_stopping"):
                await transport.post(URL, b"{}", {})
            client.assert_not_called()

    async def test_loopback_test_transport_redirect_limit_and_oversize(self):
        redirected = []
        async def handler(request):
            if request.path == "/redirect":
                raise web.HTTPFound("/target")
            if request.path == "/target":
                redirected.append(True)
            if request.path == "/large":
                return web.Response(body=b"x" * (transport_module.MAX_RESPONSE + 1))
            return web.Response(body=b"ok")
        app = web.Application()
        app.router.add_post("/{tail:.*}", handler)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        origin = "http://127.0.0.1:" + str(runner.addresses[0][1])
        transport = CallbackTransport(test_callback_origin=origin)
        try:
            self.assertEqual(await transport.post(origin + "/ok", b"{}", {}), (200, b"ok"))
            with self.assertRaisesRegex(EventError, "callback_redirect"):
                await transport.post(origin + "/redirect", b"{}", {})
            self.assertEqual(redirected, [])
            with self.assertRaisesRegex(EventError, "callback_response_too_large"):
                await transport.post(origin + "/large", b"{}", {})
            with self.assertRaisesRegex(EventError, "event_too_large"):
                await transport.post(origin + "/ok", b"x" * (transport_module.MAX_BODY + 1), {})
            with self.assertRaises(EventError):
                transport.validate("http://127.0.0.1:1/no")
        finally:
            await transport.close()
            await runner.cleanup()

    async def test_signing_exact_bytes_and_replay_timestamp_rejected_by_verifier(self):
        body = '{"text":"你好"}'.encode()
        now = int(time.time())
        headers = signing_module.signed_headers("evt_test", body, SECRET, now, "sub_test")
        expected = hmac.new(b"s" * 32, b"evt_test." + str(now).encode() + b"." + body,
                            hashlib.sha256).digest()
        self.assertEqual(headers["webhook-signature"], "v1," + base64.b64encode(expected).decode())
        Webhook(SECRET).verify(body, headers)
        old_headers = signing_module.signed_headers("evt_test", body, SECRET, now - 301, "sub_test")
        with self.assertRaises(Exception):
            Webhook(SECRET).verify(body, old_headers)


if __name__ == "__main__":
    unittest.main()
