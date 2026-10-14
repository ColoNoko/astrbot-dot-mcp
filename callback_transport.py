"""Async, pinned public HTTPS callbacks with optional explicit HTTP CONNECT proxy.

URL/public-address checks adapted from wechat-dot-bridge (MIT, cd233).
Copyright (c) 2026 cd233. See THIRD_PARTY_EVENTS_LICENSE.txt.
HTTP framing, TLS and timeouts are provided by aiohttp, not custom sockets.
"""

import asyncio
import ipaddress
import re
import socket
from urllib.parse import urlsplit

import aiohttp
from multidict import CIMultiDict
from yarl import URL

from .event_errors import EventError
from .proxy_config import ProxySettings, validate_proxy

MAX_BODY = 262144
MAX_RESPONSE = 65536
NETWORK_TIMEOUT = 10
DNS_TIMEOUT = 10


def _hostname(host):
    return (isinstance(host, str) and 1 <= len(host) <= 253 and
            all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", p)
                for p in host.split(".")))


def _public_ip(value):
    try:
        address = ipaddress.ip_address(value)
    except (ValueError, TypeError):
        raise EventError("callback_dns_blocked") from None
    if (not address.is_global or address.is_multicast or address.is_reserved or
            address.is_unspecified or (address.version == 6 and
            (address.ipv4_mapped is not None or address.is_site_local or
             address.sixtofour is not None or address.teredo is not None))):
        raise EventError("callback_dns_blocked")
    return address


class _PinnedResolver(aiohttp.abc.AbstractResolver):
    def __init__(self, hostname, entries):
        self.hostname, self.entries = hostname, entries

    async def resolve(self, host, port=443, family=socket.AF_INET):
        if host != self.hostname or port != 443:
            raise EventError("callback_dns_blocked")
        return self.entries

    async def close(self):
        pass


class CallbackTransport:
    def __init__(self, allowed_hosts=(), test_callback_origin=None, *, proxy=ProxySettings()):
        if not isinstance(allowed_hosts, (tuple, list)):
            raise EventError("invalid_callback_hosts")
        if any(not isinstance(h, str) for h in allowed_hosts):
            raise EventError("invalid_callback_hosts")
        self.allowed_hosts = frozenset(h.lower() for h in allowed_hosts)
        if any(not _hostname(h) for h in self.allowed_hosts):
            raise EventError("invalid_callback_hosts")
        self.proxy = validate_proxy(ProxySettings() if proxy is None else proxy)
        self.test_origin = None
        if test_callback_origin is not None:
            if self.proxy.enabled:
                raise EventError("invalid_test_callback_origin")
            try:
                origin = urlsplit(test_callback_origin)
                ip = ipaddress.ip_address(origin.hostname or "")
                if (origin.scheme != "http" or not ip.is_loopback or not origin.port or
                        origin.username is not None or origin.password is not None or
                        origin.path not in ("", "/") or origin.query or origin.fragment):
                    raise ValueError
                self.test_origin = (origin.scheme, origin.hostname, origin.port)
            except (ValueError, TypeError):
                raise EventError("invalid_test_callback_origin") from None
        # A timed-out libc resolver keeps its slot until it really finishes.
        self._dns_slots = asyncio.Semaphore(4)
        self._lookups = set()
        self._closed = False

    async def close(self):
        """Cancel tracked DNS waiters; libc may finish an existing OS lookup.

        The owner must cancel/gather active post callers before closing this
        transport. An OS resolver thread cannot be force-stopped by asyncio.
        """
        self._closed = True
        pending = tuple(self._lookups)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def validate(self, url):
        if (not isinstance(url, str) or not url.isascii() or len(url) > 2048 or
                any(ord(c) <= 32 or ord(c) == 127 for c in url) or "\\" in url):
            raise EventError("callback_invalid")
        try:
            parts = urlsplit(url)
            host, port = (parts.hostname or "").lower(), parts.port
        except (ValueError, TypeError):
            raise EventError("callback_invalid") from None
        if (not host or parts.username is not None or parts.password is not None or
                parts.fragment or "%" in parts.netloc):
            raise EventError("callback_invalid")
        if self.test_origin == (parts.scheme, host, port):
            return parts, True
        if parts.scheme != "https" or port not in (None, 443) or not _hostname(host):
            raise EventError("callback_invalid")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise EventError("callback_invalid")
        if host not in self.allowed_hosts:
            raise EventError("callback_not_allowed")
        return parts, False

    async def _addresses(self, host):
        if self._closed:
            raise EventError("callback_stopping")
        acquired = False
        try:
            await asyncio.wait_for(self._dns_slots.acquire(), DNS_TIMEOUT)
            acquired = True
            if self._closed:
                raise EventError("callback_stopping")
            lookup = asyncio.create_task(asyncio.get_running_loop().getaddrinfo(
                host, 443, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM,
                proto=socket.IPPROTO_TCP))
            self._lookups.add(lookup)
            def finished(task):
                self._lookups.discard(task)
                self._dns_slots.release()
                if not task.cancelled():
                    task.exception()
            lookup.add_done_callback(finished)
            acquired = False
            entries = await asyncio.wait_for(asyncio.shield(lookup), DNS_TIMEOUT)
        except asyncio.TimeoutError:
            raise EventError("callback_timeout") from None
        except OSError:
            raise EventError("callback_dns_failed") from None
        finally:
            if acquired:
                self._dns_slots.release()
        if not entries or len(entries) > 16:
            raise EventError("callback_dns_failed")
        result = []
        for family, kind, protocol, _canonical, endpoint in entries:
            if family not in (socket.AF_INET, socket.AF_INET6) or kind != socket.SOCK_STREAM:
                raise EventError("callback_dns_blocked")
            ip = _public_ip(endpoint[0])
            if (ip.version == 4) != (family == socket.AF_INET) or endpoint[1] != 443:
                raise EventError("callback_dns_blocked")
            result.append({"hostname": host, "host": str(ip), "port": 443,
                           "family": family, "proto": socket.IPPROTO_TCP,
                           "flags": socket.AI_NUMERICHOST})
        return result

    async def post(self, url, body, headers):
        if self._closed:
            raise EventError("callback_stopping")
        parts, test = self.validate(url)
        if not isinstance(body, bytes) or len(body) > MAX_BODY:
            raise EventError("event_too_large")
        host = parts.hostname.lower()
        entries = None if test else await self._addresses(host)
        if self._closed:
            raise EventError("callback_stopping")
        request_url = URL(url, encoded=True)
        request_options = {}
        if self.proxy.enabled:
            # CONNECT must name the vetted IP, not let the proxy re-resolve
            # the callback hostname. All DNS answers were checked above.
            # TLS still validates the original hostname end to end, and the
            # callback receives its original Host. No direct fallback exists.
            request_url = request_url.with_host(entries[0]["host"])
            headers = CIMultiDict(headers)
            headers["Host"] = host
            request_options = {"proxy": self.proxy.url, "server_hostname": host}
            if self.proxy.username:
                request_options["proxy_auth"] = aiohttp.BasicAuth(
                    self.proxy.username, self.proxy.password)
            # Only the deliberately configured proxy hostname is resolved by
            # this connector; private proxy addresses are an admin choice.
            resolver = None
        else:
            # Direct mode keeps the original URL for Host/SNI and supplies
            # only vetted numeric addresses through the pinned resolver.
            resolver = None if test else _PinnedResolver(host, entries)
        connector = aiohttp.TCPConnector(resolver=resolver, use_dns_cache=False,
                                         force_close=True, limit=1)
        try:
            async with aiohttp.ClientSession(
                    connector=connector, trust_env=False,
                    cookie_jar=aiohttp.DummyCookieJar(), auto_decompress=False,
                    timeout=aiohttp.ClientTimeout(total=NETWORK_TIMEOUT),
                    max_line_size=8190, max_field_size=8190) as session:
                async with session.post(request_url, data=body, headers=headers,
                                        allow_redirects=False, **request_options) as response:
                    if 300 <= response.status < 400:
                        raise EventError("callback_redirect")
                    if response.content_length is not None and response.content_length > MAX_RESPONSE:
                        raise EventError("callback_response_too_large")
                    data = bytearray()
                    async for chunk in response.content.iter_chunked(8192):
                        data.extend(chunk)
                        if len(data) > MAX_RESPONSE:
                            raise EventError("callback_response_too_large")
                    return response.status, bytes(data)
        except EventError:
            raise
        except asyncio.TimeoutError:
            raise EventError("callback_timeout") from None
        except (aiohttp.ClientConnectorCertificateError, aiohttp.ClientSSLError):
            raise EventError("callback_tls_failed") from None
        except (aiohttp.ClientError, OSError, ValueError):
            raise EventError("callback_network_failed") from None

