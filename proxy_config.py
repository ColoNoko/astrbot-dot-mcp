"""Explicit, local-admin HTTP forward proxy settings; no ambient proxy state."""
from dataclasses import dataclass, field
import ipaddress
import re
from urllib.parse import urlsplit, quote

class ProxyError(ValueError):
    def __init__(self): super().__init__('invalid_proxy_settings')

@dataclass(frozen=True)
class ProxySettings:
    enabled: bool = False
    url: str = ''
    username: str = field(default='', repr=False)
    password: str = field(default='', repr=False)

    def authenticated_url(self):
        """For a private child environment only; never log or return this value."""
        if not self.username:
            return self.url
        return 'http://' + quote(self.username, safe='') + ':' + quote(self.password, safe='') + '@' + self.url.removeprefix('http://')

def validate_proxy(settings):
    if not isinstance(settings, ProxySettings) or type(settings.enabled) is not bool:
        raise ProxyError()
    if (type(settings.url) is not str or len(settings.url) > 1024 or
            not settings.url.isascii() or any(ord(c) <= 32 or ord(c) >= 127 for c in settings.url)):
        raise ProxyError()
    for value,limit in ((settings.username,256),(settings.password,4096)):
        if type(value) is not str or len(value)>limit or not value.isascii() or any(ord(c)<32 or ord(c)>=127 for c in value):
            raise ProxyError()
    if ':' in settings.username or (settings.password and not settings.username):
        raise ProxyError()
    if not settings.url and not settings.enabled:
        return settings
    try:
        url=urlsplit(settings.url)
        host=url.hostname
        if (not settings.url.startswith('http://') or url.scheme!='http' or not host or url.port is None or not 1<=url.port<=65535 or
                url.username is not None or url.password is not None or url.path not in ('','/') or
                url.query or url.fragment or '\\' in settings.url or '%' in url.netloc):
            raise ProxyError()
        try:
            ip=ipaddress.ip_address(host)
        except ValueError:
            if not all(re.fullmatch(r'[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?',part) for part in host.split('.')): raise ProxyError()
        else:
            if ip.is_unspecified or ip.is_multicast: raise ProxyError()
    except (ValueError,TypeError):
        raise ProxyError() from None
    return settings

def proxy_from_config(config):
    return validate_proxy(ProxySettings(config.get('proxy_enabled',False),config.get('proxy_url',''),
        config.get('proxy_username',''),config.get('proxy_password','')))
