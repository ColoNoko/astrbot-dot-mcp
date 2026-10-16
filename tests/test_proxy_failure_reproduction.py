"""Read-only v0.3.4 diagnosis against a loopback fake proxy.

The fake proxy never opens an upstream socket. Public numeric addresses below
are inert CONNECT request text. The frozen production source remains unchanged.
Run directly to print only safe exception classes / errno symbols.
"""
import asyncio
import errno
import importlib
import json
from pathlib import Path
import socket
import struct
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

import aiohttp

ROOT = Path(__file__).resolve().parents[1]
if not (ROOT / ".work-v034" / "astrbot_plugin_dot_private").exists():
    raise unittest.SkipTest("Frozen v0.3.4 reproduction fixture is external to installable package")
PACKAGE = "_frozen_v034_proxy_reproduction"
if PACKAGE not in sys.modules:
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(ROOT / ".work-v034" / "astrbot_plugin_dot_private")]
    sys.modules[PACKAGE] = package
transport_module = importlib.import_module(PACKAGE + ".callback_transport")
proxy_module = importlib.import_module(PACKAGE + ".proxy_config")
HOST = "callbacks.example.com"
URL = "https://callbacks.example.com/synthetic-callback"


def entry(ip="8.8.8.8"):
    return {"hostname": HOST, "host": ip, "port": 443,
            "family": socket.AF_INET, "proto": socket.IPPROTO_TCP,
            "flags": socket.AI_NUMERICHOST}


def safe_exception(error):
    """Never stringify arbitrary exceptions, URLs, headers, or bodies."""
    wrapped = getattr(error, "os_error", None)
    number = getattr(wrapped, "errno", None)
    return {"exception_class": type(error).__name__,
            "os_error_class": type(wrapped).__name__ if wrapped else None,
            "errno": errno.errorcode.get(number)}


class ClosingProxy:
    def __init__(self, mode):
        self.mode = mode
        self.connect_requests = []
        self.tls_prefixes = []
        self.tasks = set()
        self.errors = []

    async def start(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        self.url = "http://127.0.0.1:" + str(self.server.sockets[0].getsockname()[1])
        return self

    async def close(self):
        self.server.close()
        await self.server.wait_closed()
        tasks = tuple(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        try:
            request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2)
            self.connect_requests.append(request)
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
            if self.mode != "immediate_close":
                # Wait for TLS ClientHello bytes, but never finish the handshake.
                self.tls_prefixes.append(await asyncio.wait_for(reader.readexactly(5), 2))
            if self.mode == "reset_after_clienthello":
                writer.get_extra_info("socket").setsockopt(
                    socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                writer.transport.abort()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.errors.append(type(error).__name__)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass
            self.tasks.discard(task)


class ProxyFailureReproduction(unittest.IsolatedAsyncioTestCase):
    async def proxy(self, mode):
        proxy = await ClosingProxy(mode).start()
        self.addAsyncCleanup(proxy.close)
        return proxy

    async def check_frozen(self, mode):
        proxy = await self.proxy(mode)
        transport = transport_module.CallbackTransport(
            [HOST], proxy=proxy_module.ProxySettings(True, proxy.url))
        self.addAsyncCleanup(transport.close)
        with patch.object(transport, "_addresses", AsyncMock(return_value=[entry(), entry("1.1.1.1")])):
            try:
                await transport.post(URL, b"{}", {})
            except transport_module.EventError as error:
                self.assertEqual(error.code, "callback_network_failed")
                raw = error.__context__
                self.assertIsInstance(raw, aiohttp.ClientConnectorError)
                self.assertIsInstance(raw.os_error, ConnectionResetError)
                report = safe_exception(raw)
            else:
                self.fail("Synthetic pre-TLS close must fail")
        detail = transport.last_transport["attempts"]
        self.assertEqual(len(detail), 1, "Uncertain failures must not replay another candidate")
        self.assertEqual(detail[0], {"address_family": "ipv4", "error": "callback_network_failed",
                                    "errno": None, "http_status": None})
        self.assertEqual(len(proxy.connect_requests), 1)
        self.assertTrue(proxy.connect_requests[0].startswith(b"CONNECT 8.8.8.8:443 HTTP/1.1\r\n"))
        self.assertIn(b"Host: callbacks.example.com\r\n", proxy.connect_requests[0])
        self.assertEqual(proxy.errors, [])
        if mode != "immediate_close":
            self.assertEqual(proxy.tls_prefixes[0][0], 0x16)
        print(json.dumps({"aiohttp": aiohttp.__version__, "mode": mode,
                          **report, "frozen_transport": detail}, sort_keys=True))

    async def test_connect_200_immediate_close_hits_generic_branch(self):
        await self.check_frozen("immediate_close")

    async def test_connect_200_close_after_clienthello_hits_generic_branch(self):
        await self.check_frozen("close_after_clienthello")

    async def test_connect_200_reset_after_clienthello_hits_generic_branch(self):
        await self.check_frozen("reset_after_clienthello")

    async def test_current_transport_classifies_real_wrapped_reset(self):
        name='_current_proxy_reproduction'
        if name not in sys.modules:
            package=types.ModuleType(name);package.__path__=[str(ROOT)];sys.modules[name]=package
        current=importlib.import_module(name+'.callback_transport')
        settings=importlib.import_module(name+'.proxy_config')
        for mode in ('close_after_clienthello','reset_after_clienthello'):
            proxy=await self.proxy(mode)
            transport=current.CallbackTransport([HOST],proxy=settings.ProxySettings(True,proxy.url))
            self.addAsyncCleanup(transport.close)
            with patch.object(transport,'_addresses',AsyncMock(return_value=[entry()])):
                with self.assertRaisesRegex(current.EventError,'callback_connection_reset'):
                    await transport.post(URL,b'{}',{})
            detail=transport.last_transport['attempts']
            self.assertEqual(len(detail),1)
            self.assertEqual(detail[0]['exception_category'],'connector_error')
            self.assertEqual(detail[0]['cause_category'],'connection_reset')
            if mode=='reset_after_clienthello':self.assertEqual(detail[0]['errno'],'ECONNRESET')
            self.assertEqual(len(proxy.connect_requests),1)

    async def test_connect_authority_and_host_header_are_independent(self):
        for target, host_header in (("8.8.8.8", HOST),
                                    ("8.8.8.8", "8.8.8.8:443"),
                                    (HOST, HOST)):
            with self.subTest(target=target, host_header=host_header):
                proxy = await self.proxy("close_after_clienthello")
                async with aiohttp.ClientSession(trust_env=False,
                         timeout=aiohttp.ClientTimeout(total=3)) as session:
                    with self.assertRaises(aiohttp.ClientConnectorError) as raised:
                        await session.post("https://" + target + "/synthetic-callback",
                            proxy=proxy.url, server_hostname=HOST, headers={"Host": host_header}, data=b"{}")
                self.assertIsInstance(raised.exception.os_error, ConnectionResetError)
                self.assertTrue(proxy.connect_requests[0].startswith(
                    ("CONNECT " + target + ":443 HTTP/1.1\r\n").encode()))
                self.assertIn(("Host: " + host_header + "\r\n").encode(), proxy.connect_requests[0])
                self.assertEqual(proxy.errors, [])
                print(json.dumps({"aiohttp": aiohttp.__version__,
                                  "connect_authority": "numeric" if target[0].isdigit() else "hostname",
                                  "host_header": "numeric" if host_header[0].isdigit() else "hostname",
                                  **safe_exception(raised.exception)}, sort_keys=True))


if __name__ == "__main__":
    unittest.main(verbosity=2)
