"""Authenticated native Dashboard API. Never mounted on the public MCP listener."""
import asyncio
import copy
import hashlib
import hmac
import json
import re
import secrets
from .discovery import _metadata, valid_platform_id
from .proxy_config import proxy_from_config, ProxyError, validate_proxy, ProxySettings
from .event_errors import EventError
from .network_dns import dns_settings, effective_dns
from .sender_routes import parse_routes, RouteError

PLUGIN = 'astrbot_plugin_dot_private'
FIELDS = ('platform_id', 'adapter', 'self_id', 'listen_host', 'listen_port',
          'callback_hosts', 'callback_backend', 'callback_dns', 'attachment_dns', 'network_dns', 'attachment_private_http', 'chat_context_messages', 'tunnel_id', 'tunnel_enabled', 'sender_routes', 'proxy_enabled', 'proxy_url')
DEFAULTS = dict(platform_id='', adapter='', self_id='', listen_host='127.0.0.1',
                listen_port=8769, callback_hosts=[], network_dns='legacy', attachment_private_http=False, tunnel_id='', tunnel_enabled=False, sender_routes=[], proxy_enabled=False, proxy_url='')

class AdminError(Exception):
    pass

class Admin:
    def __init__(self, plugin):
        self.plugin = plugin
        self._nonce = secrets.token_bytes(32)
        self._lock = asyncio.Lock()
        self.closed = False

    def _digest(self, value):
        return hmac.new(self._nonce, value.encode(), hashlib.sha256).hexdigest()

    def _revision(self):
        return self._digest(json.dumps(dict(self.plugin.config), sort_keys=True,
                                      separators=(',', ':'), ensure_ascii=True))

    def authorize(self, request):
        if (self.closed or request.plugin_name != PLUGIN or
                not isinstance(request.username, str) or not request.username):
            raise AdminError('forbidden')
        return self._digest('csrf:' + request.username)

    def status(self, request):
        csrf = self.authorize(request)
        config = self.plugin.config
        settings = {k: copy.deepcopy(config.get(k, v)) for k, v in DEFAULTS.items()}
        settings['enabled'] = config.get('enabled') is True
        route_error = None
        try:
            parse_routes(config)
        except RouteError:
            settings['sender_routes'] = []
            route_error = 'invalid_sender_routes'
        attachment_error = None
        if type(settings['attachment_private_http']) is not bool:
            settings['attachment_private_http'] = False
            attachment_error = 'invalid_attachment_http_setting'
        proxy_error = None
        dns_error = None
        dns_conflict = False
        try:
            display = dns_settings(config)
            settings['network_dns'] = display['network_dns']
            dns_conflict = display['conflict']
        except EventError as error:
            settings['network_dns'] = ''
            dns_error = str(error)
        if dns_error is None:
            try:
                effective_dns(config, 'callback')
                effective_dns(config, 'attachment')
            except EventError as error:
                dns_error = str(error)
        try:
            validate_proxy(ProxySettings(False, settings['proxy_url']))
        except ProxyError:
            settings['proxy_url'] = ''
            proxy_error = 'invalid_proxy_settings'
        candidates = []
        try:
            for instance in self.plugin.context.platform_manager.get_insts():
                adapter, pid = _metadata(instance)
                if adapter == 'aiocqhttp':
                    candidates.append({'id': pid, 'adapter': adapter})
            if len({v['id'] for v in candidates}) != len(candidates):
                candidates = []
        except Exception:
            candidates = []
        tunnel = self.plugin.tunnel.status() if self.plugin.tunnel else self.plugin.last_tunnel_status or {
            'enabled': False, 'state': 'stopped', 'process_running': False,
            'healthy': False, 'ready': False, 'connected': None,
            'roundtrip_verified': False, 'error_code': None, 'binary_available': False}
        routes = []
        for sender, child in getattr(self.plugin, 'route_runtimes', {}).items():
            routes.append({'sender_id': sender, 'listen_port': child.config['listen_port'],
                           'mcp_ready': child.ready and child._route_is_active(),
                           'error_code': child.runtime_error,
                           'tunnel': child.tunnel.status() if child.tunnel else child.last_tunnel_status or {}})
        routes_stale = bool(routes) and self.plugin._routes_revision != self._revision()
        return {'settings': settings,
                'network_dns_conflict': dns_conflict,
                'secrets': {'mcp_token_set': bool(config.get('mcp_token')),
                            'tunnel_key_set': bool(config.get('tunnel_runtime_key')),
                            'proxy_username_set': bool(config.get('proxy_username')),
                            'proxy_password_set': bool(config.get('proxy_password'))},
                'platforms': candidates[:32],
                'capacity_alert': self.plugin.capacity_alert.status() if getattr(self.plugin, 'capacity_alert', None) else None,
                'runtime': {'mcp_ready': self.plugin.ready and not routes_stale, 'error_code': route_error or proxy_error or dns_error or attachment_error or ('runtime_config_changed' if routes_stale else self.plugin.runtime_error), 'tunnel': tunnel, 'routes': routes},
                'versions': {'plugin': '0.5.6'},
                'csrf_token': csrf, 'revision': self._revision()}

    def tools(self, request):
        self.authorize(request)
        from .endpoint import TOOLS, PLUGIN_VERSION, tool_manifest
        return {'version': PLUGIN_VERSION, 'tool_count': len(TOOLS),
                'tools': copy.deepcopy(TOOLS), 'client_refresh_supported': False,
                'client_visible_tools': 'unknown', 'manifest': tool_manifest()}

    def history(self, request, payload):
        csrf = self.authorize(request)
        if (not isinstance(payload, dict) or not isinstance(payload.get('csrf_token'), str) or
                not hmac.compare_digest(payload['csrf_token'], csrf)):
            raise AdminError('forbidden')
        if getattr(self.plugin, 'route_runtimes', {}):
            raise AdminError('multi_route_history_unavailable')
        if not self.plugin.host or not self.plugin.ready:
            raise AdminError('history_unavailable')
        history = self.plugin.host.store.history
        action = payload.get('action')
        try:
            if action == 'conversations' and set(payload) == {'csrf_token', 'action', 'before', 'limit'}:
                result = history.conversations(limit=payload['limit'], cursor=payload['before'])
                result['next_before'] = result.pop('next_cursor', None)
            elif action == 'messages' and set(payload) in (
                    {'csrf_token','action','conversation_type','group_id','before','limit'},
                    {'csrf_token','action','conversation_type','group_id','before','limit','sender_id'}):
                if type(payload['limit']) is not int or not 1 <= payload['limit'] <= 50:
                    raise AdminError('invalid_history_request')
                result = history.page(payload['conversation_type'], payload['group_id'],
                                      limit=payload['limit'], cursor=payload['before'],
                                      sender_id=payload.get('sender_id'))
                for item in result['messages']:
                    item['processing_state'] = 'completed' if item.pop('completed', False) else 'pending'
                    for reply in item.get('replies', []):
                        reply.pop('reply_key', None)
                    attachments = getattr(self.plugin.host.store, 'attachments', None)
                    item['attachments'] = attachments.list(item['message_id']) if attachments else []
                result['next_before'] = result.pop('next_cursor', None)
            else:
                raise AdminError('invalid_history_request')
            result['storage'] = history.status()
            return result
        except AdminError:
            raise
        except Exception as error:
            code = str(error)
            if code in ('private_sender_required', 'invalid_history_scope',
                        'invalid_history_limit', 'invalid_history_cursor', 'binding_inactive'):
                raise AdminError(code) from None
            raise AdminError('history_unavailable') from None

    def _validate(self, settings):
        if not isinstance(settings, dict) or set(settings) - set(FIELDS):
            raise AdminError('invalid_settings')
        result = {k: copy.deepcopy(self.plugin.config.get(k, v)) for k, v in DEFAULTS.items()}
        # Accept a stale page's retired keys without exposing or rewriting them.
        # Hidden legacy DNS values remain effective until explicit unification.
        result.update({key: value for key, value in settings.items()
                       if key not in ('chat_context_messages', 'callback_backend',
                                      'callback_dns', 'attachment_dns')})
        if type(result['attachment_private_http']) is not bool:
            raise AdminError('invalid_attachment_http_setting')
        effective = dict(self.plugin.config)
        effective.update(result)
        try:
            effective_dns(effective, 'callback')
            effective_dns(effective, 'attachment')
        except EventError as error:
            raise AdminError(str(error)) from None
        if (type(result['platform_id']) is not str or
                (result['platform_id'] != '' and not valid_platform_id(result['platform_id'])) or
                result['adapter'] not in ('', 'aiocqhttp') or
                type(result['self_id']) is not str or
                (result['self_id'] and not re.fullmatch(r'[1-9][0-9]{0,19}', result['self_id'])) or
                result['listen_host'] != '127.0.0.1' or
                type(result['listen_port']) is not int or not 1024 <= result['listen_port'] <= 65535 or
                type(result['tunnel_enabled']) is not bool or
                type(result['tunnel_id']) is not str or
                (result['tunnel_id'] and not re.fullmatch(r'tunnel_(?:[a-z0-9]{4}_)?[a-z0-9]{32}', result['tunnel_id']))):
            raise AdminError('invalid_settings')
        hosts = result['callback_hosts']
        if (type(hosts) is not list or len(hosts) > 8 or len(set(str(v) for v in hosts)) != len(hosts) or
                any(type(v) is not str or len(v) > 253 or not re.fullmatch(
                    r'[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+', v)
                    for v in hosts)):
            raise AdminError('invalid_settings')
        try:
            routes = parse_routes(effective)
        except RouteError:
            raise AdminError('invalid_sender_routes') from None
        if result['tunnel_enabled'] and not result['tunnel_id'] and not routes:
            raise AdminError('invalid_settings')
        return result

    async def mutate(self, request, payload, kind):
        async with self._lock:
            csrf = self.authorize(request)
            if (not isinstance(payload, dict) or
                    not isinstance(payload.get('csrf_token'), str) or
                    not hmac.compare_digest(payload['csrf_token'], csrf)):
                raise AdminError('forbidden')
            if payload.get('revision') != self._revision():
                raise AdminError('revision_conflict')
            allowed = {'csrf_token', 'revision', 'settings', 'tunnel_key', 'proxy_username', 'proxy_password', 'clear_proxy_auth'} if kind == 'settings' else {'csrf_token', 'revision', 'action'}
            if set(payload) - allowed:
                raise AdminError('invalid_settings')
            updates = {}
            action = payload.get('action')
            if kind == 'settings':
                updates = self._validate(payload.get('settings', {}))
                value = payload.get('tunnel_key', '')
                if (type(value) is not str or len(value) > 4096 or
                        (value and (len(value) < 32 or not value.isascii() or
                         any(ord(c) <= 32 or ord(c) >= 127 for c in value)))):
                    raise AdminError('invalid_settings')
                if value:
                    updates['tunnel_runtime_key'] = value
                clear = payload.get('clear_proxy_auth', False)
                if type(clear) is not bool:
                    raise AdminError('invalid_settings')
                for key in ('proxy_username', 'proxy_password'):
                    value = payload.get(key, '')
                    if type(value) is not str:
                        raise AdminError('invalid_proxy_settings')
                    if clear and value:
                        raise AdminError('invalid_proxy_settings')
                    if clear or value:
                        updates[key] = '' if clear else value
                if (updates['proxy_url'] != self.plugin.config.get('proxy_url','') and
                        any(self.plugin.config.get(k) for k in ('proxy_username','proxy_password')) and
                        not clear and not (payload.get('proxy_username') and
                            (not self.plugin.config.get('proxy_password') or payload.get('proxy_password')))):
                    raise AdminError('proxy_auth_reentry_required')
                effective = dict(self.plugin.config); effective.update(updates)
                try:
                    proxy_from_config(effective)
                except ProxyError:
                    raise AdminError('invalid_proxy_settings') from None
            elif action not in ('apply', 'stop', 'reconnect_tunnel'):
                raise AdminError('invalid_settings')
            if action == 'reconnect_tunnel':
                await self.plugin._reconnect_tunnel(payload['revision'])
                self.authorize(request)
                if payload['revision'] != self._revision():
                    raise AdminError('revision_conflict')
                return self.status(request)
            # Stop old route/credentials before committing a changed configuration.
            await self.plugin._stop_runtime()
            self.authorize(request)
            if payload.get('revision') != self._revision():
                raise AdminError('revision_conflict')
            if updates:
                before = copy.deepcopy(dict(self.plugin.config))
                try:
                    save = getattr(self.plugin.config, 'save_config', None)
                    if not callable(save):
                        raise ValueError()
                    self.plugin.config.update(updates)
                    save()
                except Exception:
                    self.plugin.config.clear(); self.plugin.config.update(before)
                    raise AdminError('save_failed') from None
            if action == 'apply':
                try:
                    await self.plugin._start_enabled()
                except Exception:
                    raise AdminError('runtime_failed') from None
            return self.status(request)

    def close(self):
        self.closed = True
