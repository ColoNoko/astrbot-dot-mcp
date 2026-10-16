"""Authenticated endpoint tests. No AstrBot adapter, QQ, or external callback."""
import asyncio
import base64
import importlib
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, patch

import aiohttp
from aiohttp import web
from standardwebhooks import Webhook

ROOT = Path(__file__).resolve().parents[1]
if "astrbot_dot_plugin" not in sys.modules:
    package = types.ModuleType("astrbot_dot_plugin")
    package.__path__ = [str(ROOT)]
    sys.modules[package.__name__] = package
endpoint_module = importlib.import_module("astrbot_dot_plugin.endpoint")
host_module = importlib.import_module("astrbot_dot_plugin.host")
transport_module = importlib.import_module("astrbot_dot_plugin.callback_transport")
Endpoint, EndpointError = endpoint_module.Endpoint, endpoint_module.EndpointError
TOKEN = "synthetic-test-token-" + "x" * 32
SECRET = "whsec_" + base64.b64encode(b"s" * 32).decode()


class EndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        binding = host_module.Binding("platform-1", "adapter-1", "self-1", "owner-1")
        self.store = host_module.Store(Path(self.temp.name) / "messages.db", binding)
        self.sent = []
        async def preflight(row):
            return "verified-owner-target"
        async def send(target, row, text):
            self.sent.append((target, row["id"], text))
            return True
        self.host = host_module.Host(self.store, preflight, send)
        self.endpoint = Endpoint(self.host, TOKEN, ["callbacks.example.com"], port=0)
        await self.endpoint.start()
        self.url = "http://127.0.0.1:" + str(self.endpoint.port) + "/mcp"
        self.client = aiohttp.ClientSession()

    async def asyncTearDown(self):
        await self.client.close()
        await self.endpoint.close()
        await self.host.close()
        self.store.close()
        self.temp.cleanup()

    def ingest(self, source="source-1", text="hello owner"):
        return self.store.ingest({"source_id": source, "umo": "immutable-private-origin", "text": text})[0]

    async def rpc(self, method, params=None, headers=None):
        combined = {"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json",
                    "MCP-Protocol-Version": endpoint_module.PROTOCOL_VERSION, "Mcp-Method": method}
        if headers:
            combined.update(headers)
        async with self.client.post(self.url, headers=combined,
                                   json={"jsonrpc": "2.0", "id": 1, "method": method,
                                         "params": {} if params is None else params}) as response:
            return response.status, await response.json()

    async def raw(self, body, headers=None, method="POST", url=None):
        combined = {"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"}
        if headers:
            combined.update(headers)
        async with self.client.request(method, url or self.url, headers=combined, data=body) as response:
            return response.status, await response.json()

    async def test_attachment_api_http_shape_is_returned_over_authenticated_rpc(self):
        from astrbot_dot_plugin.attachments import AttachmentError
        async def reader(*_):
            error = AttachmentError('attachment_https_required')
            error.note_http_api_source('http://SECRET_USER:SECRET_PASS@files.example.com:8080/SECRET_PATH?key=SECRET_QUERY')
            raise error
        self.host.read_attachment = reader
        status, result = await self.rpc('tools/call', {'name': 'qq_read_attachment', 'arguments': {
            'message_id': 'msg_' + '1' * 32, 'attachment_id': 'att_' + '2' * 32}})
        self.assertEqual(status, 200)
        self.assertTrue(result['result']['isError'])
        value = json.loads(result['result']['content'][0]['text'])
        self.assertEqual(value, {'error': 'attachment_https_required', 'source_diagnostic': {
            'source': 'adapter_api', 'scheme': 'http', 'hostname': 'files.example.com', 'port': 8080}})
        self.assertNotIn('SECRET_', json.dumps(result))

    async def test_discovery_tools_schema_read_reply_and_safe_status(self):
        _, value = await self.rpc("server/discover")
        result = value["result"]
        self.assertEqual(result["resultType"], "complete")
        self.assertEqual(result["supportedVersions"], ["2026-07-28"])
        self.assertEqual(result["capabilities"], {"tools": {}, "events": {}})
        _, value = await self.rpc("tools/list")
        self.assertEqual(value["result"]["cacheScope"], "private")
        self.assertEqual(value["result"]["ttlMs"], 0)
        self.assertEqual([tool["name"] for tool in value["result"]["tools"]],
                         ["qq_read_message", "qq_reply", "qq_reply_attachment", "qq_read_attachment", "qq_complete_message", "qq_list_pending", "qq_status"])
        for tool in value["result"]["tools"]:
            self.assertFalse(tool["inputSchema"]["additionalProperties"])
            self.assertNotIn("recipient", tool["inputSchema"]["properties"])
        mid = self.ingest()
        _, value = await self.rpc("tools/call", {"name": "qq_read_message", "arguments": {"message_id": mid}})
        self.assertEqual(value["result"]["structuredContent"]["text"], "hello owner")
        arguments = {"message_id": mid, "text": "reply", "reply_key": "same-key"}
        for _ in range(2):
            _, value = await self.rpc("tools/call", {"name": "qq_reply", "arguments": arguments})
            self.assertEqual(value["result"]["structuredContent"]["state"], "adapter_completed")
        self.assertEqual(len(self.sent), 1)
        _, value = await self.rpc("tools/call", {"name": "qq_status", "arguments": {}})
        rendered = json.dumps(value)
        for secret in (TOKEN, SECRET, "owner-1", "self-1", "immutable-private-origin", "hello owner"):
            self.assertNotIn(secret, rendered)
        self.assertEqual(value["result"]["structuredContent"]["messages"], 1)
        self.assertEqual(value["result"]["structuredContent"]["message_latency"]["recent"][0]["message_id"], mid)

    async def test_subscription_http_diagnostics_cover_early_metadata_rejection(self):
        await self.rpc('events/subscribe', {'_meta': {'io.modelcontextprotocol/protocolVersion': 'wrong'}})
        _, result = await self.rpc('tools/call', {'name':'qq_status','arguments':{}})
        data=result['result']['structuredContent']
        self.assertEqual(data['subscribe_http']['recognized_requests'],1)
        self.assertEqual(data['subscribe_http']['last_stage'],'protocol_metadata')
        self.assertEqual(data['subscribe_http']['last_error'],'invalid_params_or_metadata')
        self.assertIsNone(data['events']['last_subscribe'])
        await self.raw(b'{}', {'Authorization':'Bearer SECRET_SENTINEL'})
        _, result=await self.rpc('tools/call', {'name':'qq_status','arguments':{}})
        self.assertEqual(result['result']['structuredContent']['http_rejections']['unauthorized'],1)
        self.assertNotIn('SECRET_SENTINEL',str(result))

    async def test_older_subscribe_failure_cannot_overwrite_latest_attempt(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.endpoint._dispatch
        async def dispatch(method, params):
            if method != 'events/subscribe':
                return await original(method, params)
            if params.get('older'):
                entered.set()
                await release.wait()
                raise endpoint_module._invalid()
            return {}
        self.endpoint._dispatch = dispatch
        # Identity must still distinguish attempts after the public counter saturates.
        self.endpoint.subscribe_http['recognized_requests'] = 2147483647
        older = asyncio.create_task(self.rpc('events/subscribe', {'older': True}))
        await entered.wait()
        try:
            await self.rpc('events/subscribe', {})
            expected = dict(self.endpoint.subscribe_http)
        finally:
            release.set()
            await older
        self.assertEqual(self.endpoint.subscribe_http, expected)
        self.assertIsNone(expected['last_error'])
        self.assertEqual(expected['last_stage'], 'event_dispatch')

    async def test_media_tool_requires_bound_ids_and_completion_is_explicit(self):
        mid=self.ingest()
        _, read=await self.rpc('tools/call',{'name':'qq_read_message','arguments':{'message_id':mid}})
        self.assertEqual(read['result']['structuredContent']['processing_state'],'pending')
        for arguments in ({'message_id':mid,'attachment_id':'https://example.com/file'},
                          {'message_id':mid,'attachment_id':'att_'+'1'*32,'url':'https://example.com/file'}):
            _, response=await self.rpc('tools/call',{'name':'qq_read_attachment','arguments':arguments})
            self.assertIn('error',response)
        _, response=await self.rpc('tools/call',{'name':'qq_complete_message','arguments':{'message_id':mid}})
        self.assertTrue(response['result']['structuredContent']['completed'])
        self.assertEqual(self.store.read(mid)['processing_state'],'completed')
        _, response=await self.rpc('tools/call',{'name':'qq_read_attachment','arguments':{'message_id':mid,'attachment_id':'att_'+'1'*32}})
        self.assertTrue(response['result']['isError'])

    async def test_every_argument_surface_is_strict(self):
        mid = self.ingest()
        good = {"message_id": mid, "text": "reply", "reply_key": "key"}
        for extra in ({"recipient": "other"}, {"sender_id": "other"}, {"user_id": "other"},
                      {"group_id": "other"}, {"action": "send_group_msg"}, {"umo": "other"}):
            _, value = await self.rpc("tools/call", {"name": "qq_reply", "arguments": {**good, **extra}})
            self.assertEqual(value["error"]["code"], -32602)
        for name, arguments in (("qq_reply", {**good, "text": "   "}),
                                ("qq_reply", {**good, "text": "中" * 6000}),
                                ("qq_reply", {**good, "reply_key": "bad\nkey"}),
                                ("qq_read_message", {"message_id": 1}),
                                ("qq_read_message", {"message_id": "source-1"}),
                                ("qq_status", {"anything": True}), ("other", {})):
            _, value = await self.rpc("tools/call", {"name": name, "arguments": arguments})
            self.assertEqual(value["error"]["code"], -32602)
        _, value = await self.rpc("tools/call", {"name": "qq_status", "recipient": "other"})
        self.assertEqual(value["error"]["code"], -32602)
        self.assertEqual(self.sent, [])

    async def test_auth_first_bearer_only_and_no_cors(self):
        for path in (self.url, self.url + "?token=" + TOKEN, self.url.replace("/mcp", "/other")):
            status, value = await self.raw("{", {"Authorization": "Bearer wrong"}, url=path)
            self.assertEqual((status, value), (401, {"error": "unauthorized"}))
        for headers, expected in (({"Origin": "https://evil.example"}, 403),
                                  ({"Content-Type": "text/plain"}, 415),
                                  ({"Content-Encoding": "gzip"}, 415)):
            status, _ = await self.raw("{}", headers)
            self.assertEqual(status, expected)
        self.assertEqual((await self.raw("{}", method="GET"))[0], 405)
        self.assertEqual((await self.raw("{}", url=self.url + "?access_token=" + TOKEN))[0], 404)
        self.assertEqual((await self.raw("{}", {"Authorization": "bearer " + TOKEN}))[0], 401)

    async def test_invalid_json_batches_duplicate_keys_nonfinite_depth_and_limit(self):
        invalid = ["{", '{"jsonrpc":"2.0","jsonrpc":"2.0"}', '{"x":NaN}', '{"x":1e999}',
                   '{"x":"\\ud800"}', "[" * 1000 + "]" * 1000, b"\xff"]
        for raw in invalid:
            with self.subTest(raw=str(raw)[:30]):
                status, value = await self.raw(raw)
                self.assertEqual(status, 400)
                self.assertEqual(value["error"]["code"], -32700)
        for value in ([{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}],
                      {"jsonrpc": "2.0", "method": "tools/list"},
                      {"jsonrpc": "2.0", "id": True, "method": "tools/list"},
                      {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "unknown": 1}):
            _, result = await self.raw(json.dumps(value))
            self.assertEqual(result["error"]["code"], -32600)
        self.assertEqual((await self.raw("x" * (endpoint_module.MAX_BODY + 1)))[0], 413)
        async def chunks():
            for _ in range(9):
                yield b"x" * 8192
        self.assertEqual((await self.raw(chunks()))[0], 413)

    async def test_mcp_metadata_and_header_consistency(self):
        meta = {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientCapabilities": {}}
        _, result = await self.rpc("tools/list", {"_meta": meta})
        self.assertIn("result", result)
        for params, headers in (({"_meta": None}, {}),
                                ({"_meta": {**meta, "io.modelcontextprotocol/protocolVersion": "2025-11-25"}}, {}),
                                ({}, {"MCP-Protocol-Version": "2025-11-25"}),
                                ({}, {"Mcp-Method": "tools/call"}),
                                ({}, {"Mcp-Name": "qq_reply"})):
            _, result = await self.rpc("tools/list", params, headers)
            self.assertEqual(result["error"]["code"], -32602)
        for method in ("initialize", "subscriptions/listen", "resources/list"):
            _, result = await self.rpc(method)
            self.assertEqual(result["error"]["code"], -32601)

    async def test_error_redaction_in_tools_callbacks_and_unknown_exceptions(self):
        marker = "PRIVATE_SECRET_CALLBACK_URL_" + TOKEN
        with patch.object(self.store, "read", side_effect=RuntimeError(marker)):
            _, result = await self.rpc("tools/call", {"name": "qq_read_message", "arguments": {"message_id": "msg_" + "1" * 32}})
            self.assertEqual(result["error"], {"code": -32603, "message": "Internal error"})
            self.assertNotIn(marker, json.dumps(result))
        with patch.object(self.store, "read", side_effect=host_module.Rejected(marker)):
            _, result = await self.rpc("tools/call", {"name": "qq_read_message", "arguments": {"message_id": "msg_" + "1" * 32}})
            self.assertTrue(result["result"]["isError"])
            self.assertNotIn(marker, json.dumps(result))
        _, result = await self.rpc("events/subscribe", {"name": "qq.message", "arguments": {},
                  "delivery": {"mode": "webhook", "url": "https://unapproved.example/" + marker, "secret": SECRET}})
        self.assertEqual(result["error"]["code"], -32015)
        self.assertEqual(result["error"]["data"], {"reason": "callback_not_allowed"})
        self.assertNotIn(marker, json.dumps(result))

    async def test_events_full_http_flow_challenge_ingest_delivery_and_unsubscribe(self):
        received = []
        delivered = asyncio.Event()
        async def callback(request):
            body = await request.read()
            value = Webhook(SECRET).verify(body, dict(request.headers))
            received.append((value, dict(request.headers)))
            if value.get("type") == "verification":
                return web.json_response({"challenge": value["challenge"]})
            delivered.set()
            return web.Response(status=204)
        app = web.Application()
        app.router.add_post("/callback", callback)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        origin = "http://127.0.0.1:" + str(runner.addresses[0][1])
        await self.endpoint.events.transport.close()
        self.endpoint.events.transport = transport_module.CallbackTransport(test_callback_origin=origin)
        try:
            _, result = await self.rpc("events/list")
            event = result["result"]["events"][0]
            self.assertEqual(event["name"], "qq.message")
            self.assertEqual(event["delivery"], ["webhook"])
            params = {"name": "qq.message", "arguments": {}, "cursor": None,
                      "delivery": {"mode": "webhook", "url": origin + "/callback", "secret": SECRET}}
            _, result = await self.rpc("events/subscribe", params)
            sid = result["result"]["id"]
            mid = self.ingest()
            await asyncio.wait_for(delivered.wait(), 3)
            self.assertEqual(received[-1][0]["data"], {"message_id": mid})
            self.assertEqual(received[-1][1]["X-MCP-Subscription-Id"], sid)
            self.assertEqual(len(received), 2)
            unsubscribe = {"name": "qq.message", "arguments": {},
                           "delivery": {"mode": "webhook", "url": origin + "/callback"}}
            _, result = await self.rpc("events/unsubscribe", unsubscribe)
            self.assertEqual(result["result"], {"resultType": "complete"})
            self.ingest("source-2")
            self.assertEqual(self.endpoint.events.status()["active_subscriptions"], 0)
            self.assertEqual(self.store.db.execute("SELECT count(*) FROM event_deliveries").fetchone()[0], 1)
        finally:
            await runner.cleanup()

    async def test_close_cancels_active_reply_and_delivery_releases_port_and_reloads(self):
        mid = self.ingest()
        started = asyncio.Event()
        async def blocked_send(*_args):
            started.set()
            await asyncio.Event().wait()
        self.host.send = blocked_send
        request = asyncio.create_task(self.rpc("tools/call", {"name": "qq_reply",
            "arguments": {"message_id": mid, "text": "blocked", "reply_key": "cancel-key"}}))
        await asyncio.wait_for(started.wait(), 1)
        delivery_task = self.endpoint._delivery_task
        old_port = self.endpoint.port
        await self.endpoint.close()
        await asyncio.gather(request, return_exceptions=True)
        self.assertTrue(delivery_task.done())
        self.assertEqual(self.endpoint._requests, set())
        self.assertIsNone(self.store.events)
        self.assertEqual(self.store.db.execute("SELECT state FROM replies").fetchone()[0], "uncertain")
        await self.endpoint.start()
        self.assertEqual(self.endpoint.port, old_port)
        _, result = await self.rpc("server/discover")
        self.assertIn("result", result)

    async def test_configuration_and_bind_failure_are_safe(self):
        for values in ({"token": "short"}, {"token": "x" * 32 + "\n"}, {"port": True},
                       {"port": -1}, {"listen_host": "example.com"}, {"listen_host": "192.0.2.1"}):
            with self.assertRaisesRegex(EndpointError, "invalid_listener_config"):
                Endpoint(self.host, **{"token": TOKEN, **values})
        explicit = Endpoint(self.host, TOKEN, listen_host="0.0.0.0", port=0)
        self.assertEqual(explicit.listen_host, "0.0.0.0")
        # Merely checking explicit configuration does not open a public listener.
        conflicting = Endpoint(self.host, TOKEN, port=self.endpoint.port)
        with self.assertRaisesRegex(EndpointError, "^endpoint_start_failed$"):
            await conflicting.start()
        self.assertIsNone(conflicting._runner)
        self.assertIs(self.store.events, self.endpoint.events)

    async def test_cleanup_continues_after_individual_resource_failure(self):
        marker = "sensitive-cleanup-" + TOKEN
        old_runner, old_transport = self.endpoint._runner, self.endpoint.events.transport
        delivery_task = self.endpoint._delivery_task
        with patch.object(endpoint_module.web.TCPSite, "stop", AsyncMock(side_effect=RuntimeError(marker))), \
             patch.object(old_transport, "close", AsyncMock(side_effect=RuntimeError(marker))) as transport_close, \
             patch.object(endpoint_module.web.AppRunner, "cleanup", AsyncMock()) as runner_cleanup:
            with self.assertRaisesRegex(EndpointError, "^endpoint_close_failed$"):
                await self.endpoint.close()
            transport_close.assert_awaited_once()
            runner_cleanup.assert_awaited_once()
        self.assertTrue(delivery_task.done())
        self.assertIsNone(self.store.events)
        self.assertIsNone(self.endpoint._runner)
        await old_transport.close()
        await old_runner.cleanup()

    async def test_start_failure_cleanup_exception_cannot_leak_raw_error(self):
        candidate = Endpoint(self.host, TOKEN, port=0)
        with patch.object(endpoint_module.web.TCPSite, "start", AsyncMock(side_effect=RuntimeError(TOKEN))), \
             patch.object(endpoint_module.web.AppRunner, "cleanup", AsyncMock(side_effect=RuntimeError(TOKEN))), \
             patch.object(transport_module.CallbackTransport, "close", AsyncMock()) as transport_close:
            with self.assertRaisesRegex(EndpointError, "^endpoint_start_failed$"):
                await candidate.start()
            transport_close.assert_awaited_once()

    @unittest.skipUnless(os.environ.get("MCP_CORE_MODULE") and shutil.which("node"),
                         "set MCP_CORE_MODULE to an isolated official SDK 2.3.1 core index.mjs")
    async def test_official_sdk2_schema_validation_external_process(self):
        # The SDK stays outside AstrBot. This test imports only in a Node child.
        mid = self.ingest()
        async def media(message_id, attachment_id):
            if attachment_id.endswith('1'):
                content = {"type":"image","mimeType":"image/png","data":"iVBORw0KGgo="}
            else:
                content = {"type":"resource","resource":{"uri":"attachment://"+attachment_id,
                           "mimeType":"application/octet-stream","blob":"aGVsbG8="}}
            return {"content":[content],"isError":False}
        self.host.read_attachment = media
        script = r'''
        import assert from 'node:assert/strict';
        const sdk = await import(process.env.MCP_CORE_MODULE);
        const url = process.env.TEST_MCP_URL;
        for (const [method, schema, args] of [
          ['server/discover', sdk.DiscoverResultSchema, {}],
          ['tools/list', sdk.ListToolsResultSchema, {}],
          ['tools/call', sdk.CallToolResultSchema, {name:'qq_status', arguments:{}}],
          ['tools/call', sdk.CallToolResultSchema, {name:'qq_read_attachment', arguments:{message_id:process.env.TEST_MESSAGE_ID,attachment_id:'att_'+ '1'.repeat(32)}}],
          ['tools/call', sdk.CallToolResultSchema, {name:'qq_read_attachment', arguments:{message_id:process.env.TEST_MESSAGE_ID,attachment_id:'att_'+ '2'.repeat(32)}}]
        ]) {
          const response = await fetch(url, {method:'POST',headers:{
            'Content-Type':'application/json', 'Authorization':'Bearer '+process.env.TEST_MCP_TOKEN,
            'MCP-Protocol-Version':'2026-07-28','Mcp-Method':method,
            ...(args.name?{'Mcp-Name':args.name}:{})
          }, body:JSON.stringify({jsonrpc:'2.0',id:1,method,params:{...args,_meta:{
            'io.modelcontextprotocol/protocolVersion':'2026-07-28',
            'io.modelcontextprotocol/clientCapabilities':{}
          }}})});
          assert.equal(response.status,200);
          const envelope = await response.json();
          sdk.JSONRPCResultResponseSchema.parse(envelope);
          schema.parse(envelope.result);
          assert.equal(envelope.result.resultType,'complete');
        }
        console.log('Official SDK 2.3.1 discovery/list/call schema validation passed');
        '''
        process = await asyncio.create_subprocess_exec("node", "--input-type=module", "-e", script,
            env={**os.environ, "TEST_MCP_URL": self.url, "TEST_MCP_TOKEN": TOKEN, "TEST_MESSAGE_ID": mid},
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
        self.assertEqual(process.returncode, 0, stderr.decode())
        self.assertIn(b"schema validation passed", stdout)


if __name__ == "__main__":
    unittest.main()
