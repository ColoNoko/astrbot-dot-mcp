"""Bounded Google DNS-over-HTTPS for explicitly allowed callback hostnames.

Only the hostname is disclosed. The resolver uses a fixed numeric CONNECT
bootstrap, with normal aiohttp TLS verification for dns.google. The configured
proxy is mandatory; there is no environment, resolver, or direct fallback.

URL/public-address checks adapted from wechat-dot-bridge (MIT, cd233).
Copyright (c) 2026 cd233. See THIRD_PARTY_EVENTS_LICENSE.txt.
"""

import asyncio
import ipaddress
import json
import re
import socket

import aiohttp
from yarl import URL

from .event_errors import EventError
from .proxy_config import validate_proxy

GOOGLE_HOST = "dns.google"
GOOGLE_BOOTSTRAP = "8.8.8.8"
MAX_DNS_RESPONSE = 65536
MAX_DNS_RECORDS = 16
MAX_CNAME_HOPS = 8
DNS_CONNECTION_LIMIT = 4
DNS_KEEPALIVE_SECONDS = 15


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


def _dns_name(value):
    if not isinstance(value, str) or not value.isascii():
        raise EventError("callback_dns_failed")
    name = value.removesuffix(".").lower()
    if not _hostname(name):
        raise EventError("callback_dns_failed")
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return name
    raise EventError("callback_dns_failed")


def _unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError
        result[name] = value
    return result


def _invalid_constant(_value):
    raise ValueError


def parse_google_answers(body, host):
    """Accept only a complete A response linked to the requested DNS name."""
    if not isinstance(body, bytes) or len(body) > MAX_DNS_RESPONSE:
        raise EventError("callback_dns_failed")
    host = _dns_name(host)
    try:
        value = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object,
                           parse_constant=_invalid_constant)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise EventError("callback_dns_failed") from None
    if (not isinstance(value, dict) or type(value.get("Status")) is not int or
            value["Status"] != 0 or value.get("TC") is not False):
        raise EventError("callback_dns_failed")
    if any(key in value and type(value[key]) is not bool for key in ("RD", "RA", "AD", "CD")):
        raise EventError("callback_dns_failed")
    question = value.get("Question")
    if (not isinstance(question, list) or len(question) != 1 or
            not isinstance(question[0], dict) or type(question[0].get("type")) is not int or
            question[0]["type"] != 1 or _dns_name(question[0].get("name")) != host):
        raise EventError("callback_dns_failed")
    answers = value.get("Answer")
    if not isinstance(answers, list) or not 1 <= len(answers) <= MAX_DNS_RECORDS:
        raise EventError("callback_dns_failed")
    # A successful answer needs no authority/glue records. Never use or chase
    # additional records that might name unrelated hosts or addresses.
    if any(key in value and value[key] != [] for key in ("Authority", "Additional")):
        raise EventError("callback_dns_failed")
    aliases, addresses = {}, []
    for record in answers:
        if (not isinstance(record, dict) or type(record.get("type")) is not int or
                record["type"] not in (1, 5) or not isinstance(record.get("data"), str) or
                type(record.get("TTL")) is not int or not 0 <= record["TTL"] <= 0xffffffff):
            raise EventError("callback_dns_failed")
        owner = _dns_name(record.get("name"))
        if record["type"] == 5:
            if owner in aliases:
                raise EventError("callback_dns_failed")
            aliases[owner] = _dns_name(record["data"])
        else:
            address = _public_ip(record["data"])
            if address.version != 4:
                raise EventError("callback_dns_blocked")
            addresses.append((owner, str(address)))
    reachable, current = {host}, host
    for _hop in range(MAX_CNAME_HOPS):
        if current not in aliases:
            break
        current = aliases[current]
        if current in reachable:
            raise EventError("callback_dns_failed")
        reachable.add(current)
    if (current in aliases or not addresses or not set(aliases).issubset(reachable) or
            any(owner not in reachable or owner in aliases for owner, _address in addresses)):
        raise EventError("callback_dns_failed")
    result = []
    for _owner, address in addresses:
        if address not in result:
            result.append(address)
    return [{"hostname": host, "host": address, "port": 443,
             "family": socket.AF_INET, "proto": socket.IPPROTO_TCP,
             "flags": socket.AI_NUMERICHOST} for address in result]


class GoogleResolver:
    """A proxy-bound pool used exclusively for fresh, validated Google DoH GETs.

    No answers are cached. The fixed bootstrap, TLS name, proxy and credentials
    never vary during this object's lifetime. A transport reload owns a new pool.
    """
    def __init__(self, proxy):
        self._proxy = validate_proxy(proxy)
        if not self._proxy.enabled:
            raise EventError("callback_dns_requires_proxy")
        self._session = None
        self._closed = False

    @property
    def proxy(self):
        return self._proxy

    def _get_session(self):
        if self._closed:
            raise EventError("callback_stopping")
        if self._session is None:
            # These supported trace events observe actual connection creation
            # and selection, rather than inferring reuse from request order.
            trace = aiohttp.TraceConfig()
            async def created(_session, context, _params):
                detail = context.trace_request_ctx
                detail["connections_created"] += 1
                if detail["connection_reused"] is None:
                    detail["connection_reused"] = False
            async def reused(_session, context, _params):
                detail = context.trace_request_ctx
                detail["connections_reused"] += 1
                detail["connection_reused"] = True
            trace.on_connection_create_end.append(created)
            trace.on_connection_reuseconn.append(reused)
            connector = aiohttp.TCPConnector(
                use_dns_cache=False, force_close=False,
                limit=DNS_CONNECTION_LIMIT, limit_per_host=DNS_CONNECTION_LIMIT,
                keepalive_timeout=DNS_KEEPALIVE_SECONDS)
            self._session = aiohttp.ClientSession(
                connector=connector, trust_env=False, cookie_jar=aiohttp.DummyCookieJar(),
                auto_decompress=False, timeout=aiohttp.ClientTimeout(total=10),
                max_line_size=8190, max_field_size=8190, trace_configs=[trace])
        return self._session

    async def close(self):
        self._closed = True
        if self._session is not None:
            await self._session.close()

    async def resolve(self, host, *, timeout, detail):
        if self._closed:
            raise EventError("callback_stopping")
        # Only the validated hostname enters the sole fixed-origin query. There
        # is no arbitrary-URL/session API and no callback credentials or body.
        if host != _dns_name(host):
            raise EventError("callback_dns_failed")
        target = URL("https://" + GOOGLE_BOOTSTRAP + "/resolve").with_query(name=host, type="A")
        options = {"proxy": self.proxy.url, "server_hostname": GOOGLE_HOST}
        if self.proxy.username:
            options["proxy_auth"] = aiohttp.BasicAuth(self.proxy.username, self.proxy.password)
        detail.update(connection_reused=None, connections_created=0, connections_reused=0)
        detail["phase"] = "connection_or_request"
        try:
            session = self._get_session()
            async with session.get(target, headers={"Host": GOOGLE_HOST,
                    "Accept": "application/dns-json", "Accept-Encoding": "identity"},
                    timeout=aiohttp.ClientTimeout(total=timeout),
                    trace_request_ctx=detail, allow_redirects=False, **options) as response:
                detail["phase"] = "response_body"
                detail["http_status"] = response.status
                if response.status != 200:
                    detail["error"] = "redirect" if 300 <= response.status < 400 else "http_status"
                    raise EventError("callback_dns_failed")
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                if content_type not in ("application/json", "application/dns-json"):
                    detail["error"] = "invalid_response"
                    raise EventError("callback_dns_failed")
                if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                    detail["error"] = "invalid_response"
                    raise EventError("callback_dns_failed")
                if response.content_length is not None and response.content_length > MAX_DNS_RESPONSE:
                    detail["error"] = "response_too_large"
                    raise EventError("callback_dns_failed")
                body = bytearray()
                async for chunk in response.content.iter_chunked(8192):
                    if len(body) + len(chunk) > MAX_DNS_RESPONSE:
                        detail["error"] = "response_too_large"
                        raise EventError("callback_dns_failed")
                    body.extend(chunk)
                detail["phase"] = "validation"
                return parse_google_answers(bytes(body), host)
        except EventError as error:
            if detail.get("error") is None:
                detail["error"] = "blocked" if error.code == "callback_dns_blocked" else "invalid_response"
            raise
        except asyncio.TimeoutError:
            detail["error"] = "timeout"
            raise EventError("callback_timeout") from None
        except (aiohttp.ClientConnectorCertificateError, aiohttp.ClientSSLError):
            detail["error"] = "tls_failed"
            raise EventError("callback_dns_failed") from None
        except aiohttp.ClientHttpProxyError as error:
            detail["connect_status"] = error.status if type(error.status) is int and 100 <= error.status <= 599 else None
            detail["error"] = "proxy_failed"
            raise EventError("callback_dns_failed") from None
        except aiohttp.ClientPayloadError:
            detail["error"] = "response_incomplete"
            raise EventError("callback_dns_failed") from None
        except (aiohttp.ClientError, OSError, ValueError):
            detail["error"] = "network_failed"
            raise EventError("callback_dns_failed") from None


async def resolve_google(host, proxy, *, timeout, detail):
    """Compatibility one-shot resolver; production keeps one pool per transport."""
    resolver = GoogleResolver(proxy)
    try:
        return await resolver.resolve(host, timeout=timeout, detail=detail)
    finally:
        await resolver.close()
