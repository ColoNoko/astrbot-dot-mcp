"""Async, pinned public HTTPS callbacks with optional explicit HTTP CONNECT proxy.

URL/public-address checks adapted from wechat-dot-bridge (MIT, cd233).
Copyright (c) 2026 cd233. See THIRD_PARTY_EVENTS_LICENSE.txt.
HTTP framing, TLS and timeouts are provided by aiohttp, not custom sockets.
"""

import asyncio
from contextvars import ContextVar
import errno
import ipaddress
import socket
import time
from urllib.parse import urlsplit

import aiohttp
from multidict import CIMultiDict
from yarl import URL

from .event_errors import EventError
from .callback_dns import _hostname, _public_ip, GoogleResolver
from .tls_diagnostics import record_tls
from .proxy_config import ProxySettings, validate_proxy

MAX_BODY = 262144
MAX_RESPONSE = 65536
NETWORK_TIMEOUT = 10
DNS_TIMEOUT = 10


def _stamp(detail, phase):
    # Operation boundaries, not claims about packet or body transmission.
    detail[phase + "_utc_ms"] = time.time_ns() // 1_000_000
    detail[phase + "_monotonic_ms"] = time.monotonic_ns() // 1_000_000


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
    def __init__(self, allowed_hosts=(), test_callback_origin=None, *, proxy=ProxySettings(), backend="aiohttp", callback_dns="system"):
        if callback_dns not in ("system", "google"):
            raise EventError("invalid_callback_dns")
        self.callback_dns = callback_dns
        # Legacy callers may still supply backend; callbacks now always use
        # aiohttp and never load the removed temporary compatibility transport.
        self.backend = "aiohttp"
        if not isinstance(allowed_hosts, (tuple, list)):
            raise EventError("invalid_callback_hosts")
        if any(not isinstance(h, str) for h in allowed_hosts):
            raise EventError("invalid_callback_hosts")
        self.allowed_hosts = frozenset(h.lower() for h in allowed_hosts)
        if any(not _hostname(h) for h in self.allowed_hosts):
            raise EventError("invalid_callback_hosts")
        self._proxy = validate_proxy(ProxySettings() if proxy is None else proxy)
        if callback_dns == "google" and not self.proxy.enabled:
            raise EventError("callback_dns_requires_proxy")
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
        self.last_transport = None
        # The global snapshot remains latest-started for status(), while each
        # caller/DNS task observes only its own POST's diagnostic dictionary.
        self._request_transport = ContextVar('callback_request_transport', default=None)
        self._request_preflight = ContextVar('callback_request_preflight', default=None)
        self._google_resolver = GoogleResolver(self.proxy) if callback_dns == "google" else None

    @property
    def request_transport(self):
        return self._request_transport.get()

    @property
    def proxy(self):
        return self._proxy

    async def close(self):
        """Cancel tracked DNS waiters; libc may finish an existing OS lookup.

        The owner must cancel/gather active post callers before closing this
        transport. An OS resolver thread cannot be force-stopped by asyncio.
        """
        self._closed = True
        pending = tuple(self._lookups)
        for task in pending:
            task.cancel()
        try:
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        finally:
            # Closing the owner also closes every idle DoH tunnel. In-flight
            # requests have been cancelled before their reusable pool is closed.
            if self._google_resolver is not None:
                await self._google_resolver.close()

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
        diagnostic = self.request_transport
        detail = diagnostic["dns"] if diagnostic is not None else None
        if detail is not None:
            _stamp(detail, "started")
        try:
            if self.callback_dns == "google":
                return await self._google_addresses(host)
            return await self._system_addresses(host)
        finally:
            if detail is not None:
                _stamp(detail, "completed")
                detail["elapsed_ms"] = detail["completed_monotonic_ms"] - detail["started_monotonic_ms"]

    async def _google_addresses(self, host):
        if self._closed:
            raise EventError("callback_stopping")
        if not _hostname(host) or host not in self.allowed_hosts:
            raise EventError("callback_not_allowed")
        diagnostic = self.request_transport
        detail = diagnostic["dns"] if diagnostic is not None else {}
        detail.update(mode="google", phase="queued", error=None,
                      elapsed_ms=0, http_status=None, connect_status=None)
        loop = asyncio.get_running_loop()
        started = loop.time()
        deadline = started + DNS_TIMEOUT

        async def lookup():
            try:
                # Queueing and the complete DoH exchange share one DNS budget.
                async with asyncio.timeout_at(deadline):
                    async with self._dns_slots:
                        if self._closed:
                            raise EventError("callback_stopping")
                        remaining = deadline - loop.time()
                        if remaining <= 0:
                            raise TimeoutError
                        result = await self._google_resolver.resolve(host,
                            timeout=remaining, detail=detail)
                        if self._closed:
                            raise EventError("callback_stopping")
                        if loop.time() >= deadline:
                            raise TimeoutError
                        detail["phase"] = "complete"
                        return result
            except asyncio.TimeoutError:
                detail["error"] = "timeout"
                raise EventError("callback_timeout") from None
            except asyncio.CancelledError:
                detail["error"] = "stopping" if self._closed else "cancelled"
                raise
            except EventError as error:
                if detail["error"] is None:
                    detail["error"] = "stopping" if error.code == "callback_stopping" else "invalid_response"
                raise
            finally:
                detail["elapsed_ms"] = round((loop.time() - started) * 1000)

        task = asyncio.create_task(lookup())
        self._lookups.add(task)
        task.add_done_callback(self._lookups.discard)
        # Unlike a libc lookup, aiohttp is cancellable: cancellation closes the
        # active request before the slot is returned. The owner keeps only safe
        # idle DoH connections. No shield or late callback POST.
        return await task

    async def _system_addresses(self, host):
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

    def _check_request_preflight(self):
        guard = self._request_preflight.get()
        if guard is None:
            return
        try:
            permitted = callable(guard) and guard() is True
        except Exception:
            permitted = False
        if not permitted:
            raise EventError('callback_preflight_rejected')

    async def post(self, url, body, headers, *, preflight=None):
        self._request_preflight.set(preflight)
        diagnostic = self.last_transport = {"backend": self.backend, "proxy_enabled": self.proxy.enabled, "attempts": [],
                               "aiohttp_version": aiohttp.__version__,
                               "backend_version": aiohttp.__version__,
                               "dns": {"mode": self.callback_dns, "phase": "system" if self.callback_dns == "system" else "pending", "error": None}}
        self._request_transport.set(diagnostic)
        self._check_request_preflight()
        if self._closed:
            raise EventError("callback_stopping")
        parts, test = self.validate(url)
        if not isinstance(body, bytes) or len(body) > MAX_BODY:
            raise EventError("event_too_large")
        host = parts.hostname.lower()
        entries = None if test else await self._addresses(host)
        self._check_request_preflight()
        if self._closed:
            raise EventError("callback_stopping")
        candidates = [entries]
        if self.proxy.enabled:
            # Prefer IPv4 for common HTTP proxies, but retain a vetted alternative.
            unique = {entry['host']: entry for entry in entries}
            ordered = sorted(unique.values(), key=lambda entry: entry['family'] != socket.AF_INET)
            first = ordered[0]
            alternative = next((entry for entry in ordered[1:] if entry['family'] != first['family']),
                               ordered[1] if len(ordered) > 1 else None)
            candidates = [[first]] + ([[alternative]] if alternative else [])
        loop = asyncio.get_running_loop()
        deadline = loop.time() + NETWORK_TIMEOUT
        try:
            async with asyncio.timeout_at(deadline):
                for index, addresses in enumerate(candidates):
                    if self._closed:
                        raise EventError('callback_stopping')
                    started = loop.time()
                    remaining = deadline - started
                    if remaining <= 0:
                        raise TimeoutError
                    detail = {'tls_category': None, 'tls_verify_code': None, 'tls_reason': None, 'phase': 'starting', 'connect_status': None, 'response_status': None,
                              'request_body_complete': None, 'elapsed_ms': 0,
                              'attempt_budget_ms': round(remaining * 1000), 'address_family': ('ipv4' if addresses[0]['family'] == socket.AF_INET else 'ipv6')
                              if self.proxy.enabled else 'pinned_set', 'error': None, 'errno': None,
                              'http_status': None, 'exception_category': None, 'cause_category': None}
                    diagnostic['attempts'].append(detail)
                    _stamp(detail, 'started')
                    try:
                        result = await self._post_once(url, body, headers, addresses, test,
                            remaining, detail)
                        detail['http_status'] = result[0]
                        return result
                    except EventError as error:
                        detail['error'] = error.code
                        detail['http_status'] = getattr(error, 'http_status', None)
                        # Only a rejected CONNECT proves the callback POST did not start.
                        # Resets/timeouts/disconnects may follow a delivered body: never
                        # replay them here merely because another address is available.
                        retry = (error.code == 'callback_proxy_failed' and
                                 detail['http_status'] in (502, 503, 504))
                        if not retry or index == len(candidates) - 1:
                            raise
                    finally:
                        _stamp(detail, 'completed')
                        detail['elapsed_ms'] = round((loop.time() - started) * 1000)
        except asyncio.TimeoutError:
            if diagnostic['attempts']:
                diagnostic['attempts'][-1]['error'] = 'callback_timeout'
            raise EventError('callback_timeout') from None

    async def _post_once(self, url, body, headers, entries, test, timeout, detail):
        # Decompression is deliberately disabled for bounded response bytes.
        # Do not advertise aiohttp's default gzip/deflate support.
        headers = CIMultiDict(headers)
        headers['Accept-Encoding'] = 'identity'
        host = urlsplit(url).hostname.lower()
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
        self._check_request_preflight()
        connector_type = aiohttp.TCPConnector
        if self._request_preflight.get() is not None:
            guard = self._check_request_preflight
            class GuardedConnector(aiohttp.TCPConnector):
                async def _create_connection(self, req, traces, timeout):
                    protocol = await super()._create_connection(req, traces, timeout)
                    try:
                        # CONNECT/TLS has completed; no request bytes sent yet.
                        guard()
                    except BaseException:
                        protocol.close()
                        raise
                    return protocol
            connector_type = GuardedConnector
        connector = connector_type(resolver=resolver, use_dns_cache=False,
                                   force_close=True, limit=1)
        detail['phase'] = 'connection_or_request'
        try:
            async with aiohttp.ClientSession(
                    connector=connector, trust_env=False,
                    cookie_jar=aiohttp.DummyCookieJar(), auto_decompress=False,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                    max_line_size=8190, max_field_size=8190) as session:
                async with session.post(request_url, data=body, headers=headers,
                                        allow_redirects=False, **request_options) as response:
                    detail['phase'] = 'response_body'
                    detail['response_status'] = response.status
                    response_headers = getattr(response, 'headers', {})
                    encodings = (response_headers.getall('Content-Encoding', [])
                                 if hasattr(response_headers, 'getall') else
                                 ([response_headers['Content-Encoding']] if 'Content-Encoding' in response_headers else []))
                    encoding = encodings[0].strip().lower() if len(encodings) == 1 else ('identity' if not encodings else 'other')
                    detail['response_encoding'] = encoding if encoding in {'identity','gzip','deflate','br','zstd'} else 'other'
                    if len(encodings) > 1 or encoding != 'identity':
                        raise EventError('callback_encoding_unsupported', http_status=response.status)
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
        except (aiohttp.ClientConnectorCertificateError, aiohttp.ClientSSLError) as error:
            record_tls(detail, error)
            raise EventError("callback_tls_failed") from None
        except aiohttp.ClientHttpProxyError as error:
            detail['connect_status'] = error.status
            raise EventError("callback_proxy_failed", http_status=error.status) from None
        except aiohttp.ClientProxyConnectionError as error:
            number = getattr(getattr(error, 'os_error', None), 'errno', None)
            detail['errno'] = errno.errorcode.get(number) if number in (errno.ECONNREFUSED, errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ETIMEDOUT, errno.EACCES) else None
            raise EventError('callback_proxy_connect_failed') from None
        except aiohttp.ClientConnectorError as error:
            # aiohttp wraps OSError during TLS upgrade, including resets after
            # a proxy has already returned CONNECT 200. Never stringify it.
            wrapped = getattr(error, 'os_error', None)
            detail['exception_category'] = 'connector_error'
            detail['cause_category'] = ('connection_reset' if isinstance(wrapped, ConnectionResetError)
                                        else 'dns_error' if isinstance(wrapped, socket.gaierror)
                                        else 'os_error' if isinstance(wrapped, OSError) else None)
            number = getattr(wrapped, 'errno', None)
            detail['errno'] = errno.errorcode.get(number) if number in (
                errno.ECONNRESET, errno.ECONNREFUSED, errno.ECONNABORTED, errno.ENETUNREACH,
                errno.EHOSTUNREACH, errno.ETIMEDOUT, errno.EPIPE, errno.EACCES) else None
            raise EventError('callback_connection_reset' if isinstance(wrapped, ConnectionResetError)
                             else 'callback_connector_failed') from None
        except aiohttp.ServerDisconnectedError:
            raise EventError('callback_server_disconnected') from None
        except (aiohttp.ClientConnectionResetError, ConnectionResetError):
            raise EventError('callback_connection_reset') from None
        except aiohttp.ClientPayloadError:
            raise EventError('callback_response_incomplete') from None
        except (aiohttp.ClientError, OSError, ValueError) as error:
            detail['exception_category'] = ('http_response_error' if isinstance(error, aiohttp.ClientResponseError)
                else 'connection_error' if isinstance(error, aiohttp.ClientConnectionError)
                else 'client_error' if isinstance(error, aiohttp.ClientError)
                else 'os_error' if isinstance(error, OSError) else 'invalid_request')
            raise EventError("callback_network_failed") from None

