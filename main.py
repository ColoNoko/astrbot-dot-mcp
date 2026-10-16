"""Native AstrBot plugin. No model/provider call, no arbitrary-recipient tool."""
from pathlib import Path
import asyncio
import re
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, StarTools
from astrbot.api.message_components import Plain, At
from .host import Binding, Host, Store, Rejected, NotSent, snapshot, safe_image_file_id, MEDIA_REPLY_PREFIX
from .access_policy import AccessPolicy, PolicyError
from .endpoint import Endpoint
from .response_timing import ResponseTrace, ResponseTimingLogger
from .event_errors import EventError
from .group_file_candidates import GroupFileCandidates, file_intent, identity as candidate_identity
from .discovery import discover_defaults
from .admin import Admin, AdminError
import json
import os
import platform
import stat
import time
import tempfile
import sqlite3
import secrets
from .proxy_config import proxy_from_config
from .network_dns import effective_dns
from .attachments import AttachmentError, note_attachment_api, validate_known_size


def _optional_loaded_hook():
    # Discovery is optional: a missing future hook must not break manual setup.
    try:
        decorator = filter.on_astrbot_loaded()
        if callable(decorator):
            return decorator
    except Exception:
        pass
    return lambda method: method


class Main(Star):
    def __init__(self, context: Context, config: AstrBotConfig, *, _route_parent=None, _route_sender=None, _route_root=None):
        super().__init__(context)
        self.config = config
        self._route_parent = _route_parent
        self._route_sender = _route_sender
        self._route_root = _route_root
        self.route_runtimes = {}
        self._routes_revision = None
        self.host = None
        self.endpoint = None
        self.captured = {}
        self.captured_bots = {}
        self.group_candidates = GroupFileCandidates()
        self.candidate_links = {}
        self._candidate_tasks = set()
        self._candidate_cleanup_task = None
        self.capacity_alert = None
        self._capacity_log_at = 0.0
        self.response_logger = ResponseTimingLogger(logger)
        self.ready = False
        self.raw_event_type = None
        self.media_types = {}
        self.component_types = {}
        self.discovery_status = {'state': 'not_checked', 'candidate_ids': []}
        self.tunnel = None
        self._runtime_lock = asyncio.Lock()
        self._runtime_config_revision = None
        self._runtime_root = None
        self.runtime_error = None
        self.last_tunnel_status = None
        self.admin = Admin(self)
        if _route_parent is None:
            self._register_admin()

    def _register_admin(self):
        register = getattr(self.context, 'register_web_api', None)
        if not callable(register):
            return  # Older/mocked runtimes cannot expose an unauthenticated fallback.
        for name, method, handler in (('status', 'GET', self.page_status),
                                      ('tools', 'GET', self.page_tools),
                                      ('settings', 'POST', self.page_settings),
                                      ('action', 'POST', self.page_action),
                                      ('history', 'POST', self.page_history)):
            register('/astrbot_plugin_dot_private/' + name, handler, [method],
                     'dot connection management')

    async def _page(self, kind):
        from astrbot.api.web import request, json_response, error_response
        headers = {'Cache-Control': 'no-store'}
        try:
            self.admin.authorize(request)
            if kind == 'status':
                return json_response(self.admin.status(request), headers=headers)
            if kind == 'tools':
                return json_response(self.admin.tools(request), headers=headers)
            if request.content_type is None or request.content_type.split(';')[0] != 'application/json':
                raise AdminError('invalid_settings')
            body = await request.body()
            if len(body) > 16384:
                raise AdminError('invalid_settings')
            try:
                payload = json.loads(body)
            except (ValueError, UnicodeError):
                raise AdminError('invalid_settings') from None
            result = (self.admin.history(request, payload) if kind == 'history' else
                      await self.admin.mutate(request, payload, kind))
            return json_response(result, headers=headers)
        except AdminError as exc:
            code = str(exc)
            return error_response(code, status_code=403 if code == 'forbidden' else 400,
                                  headers=headers)
        except Exception:
            return error_response('runtime_failed', status_code=500, headers=headers)

    async def page_status(self):
        return await self._page('status')

    async def page_tools(self):
        return await self._page('tools')

    async def page_settings(self):
        return await self._page('settings')

    async def page_action(self):
        return await self._page('action')

    async def page_history(self):
        return await self._page('history')

    async def _start_tunnel(self, root):
        if self.config.get('tunnel_enabled') is not True:
            return
        from .tunnel_runtime import TunnelSupervisor, TunnelSettings
        arch = {'x86_64': 'amd64', 'aarch64': 'arm64'}.get(platform.machine(), 'unsupported')
        binary = Path(__file__).parent / 'vendor' / ('linux-' + arch) / 'tunnel-client-runtime'
        private = root / 'tunnel-private'
        private.mkdir(mode=0o700, exist_ok=True)
        info = private.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise RuntimeError('tunnel_private_directory_required')
        self.tunnel = TunnelSupervisor(private, binary)
        self.last_tunnel_status = None
        key = self.config.get('tunnel_runtime_key', '')
        if not isinstance(key, str) or not key:
            raise RuntimeError('tunnel_key_missing')
        key_path = self._write_runtime_key(private, key)
        settings = TunnelSettings(enabled=True, tunnel_id=self.config.get('tunnel_id', ''),
                                  runtime_key_file=str(key_path))
        await self.tunnel.start(settings,
            endpoint_url='http://127.0.0.1:%s/mcp' % self.config.get('listen_port', 8769),
            mcp_token=self.config.get('mcp_token', ''), proxy=proxy_from_config(self.config))

    @staticmethod
    def _write_runtime_key(private, key):
        # Atomic replacement never truncates an existing hardlink or symlink.
        fd, temporary = tempfile.mkstemp(prefix='.key-', dir=private)
        try:
            with os.fdopen(fd, 'w') as stream:
                stream.write(key)
                stream.flush()
                os.fsync(stream.fileno())
            path = private / 'tunnel-runtime-key'
            os.replace(temporary, path)
            return path
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _discover(self):
        try:
            self.discovery_status = discover_defaults(self.context, self.config)
        except Exception:
            self.discovery_status = {'state': 'unavailable', 'candidate_ids': []}
        if self.discovery_status['state'] in ('multiple_candidates', 'configured_ambiguity', 'invalid_metadata', 'save_failed'):
            logger.error('dot_private_platform_discovery_' + self.discovery_status['state'])
        if self.discovery_status['state'] == 'multiple_candidates':
            logger.error('dot_private_candidate_platform_ids: ' + ', '.join(self.discovery_status['candidate_ids']))

    async def initialize(self):
        if self.admin.closed:
            self.admin = Admin(self)
        # No reliable public ready flag exists in our tested API boundary.
        # A reload can overlap startup: never persist a first partial instance.
        if any(self.config.get(key, '') == '' for key in ('platform_id', 'adapter')):
            self.discovery_status = {'state': 'waiting_astrbot_loaded', 'candidate_ids': []}
        else:
            self._discover()
        # A conservative upgrade also makes a disabled plugin's native
        # configuration show its existing permissions before it is enabled.
        if all(self.config.get(key, '') != '' for key in
               ('platform_id', 'adapter', 'self_id', 'sender_id')):
            try:
                self._upgrade_access_policy()
            except Rejected as error:
                self.runtime_error = ('access_policy_migration_failed' if
                    str(error) == 'access_policy_migration_failed' else 'startup_failed')
                logger.error('dot_private_access_policy_upgrade_failed')
                return
        # AstrBot initializes plugins before platforms at application startup.
        # Defer only the auto-discoverable blanks; never fill sensitive identity.
        if (self.config.get('enabled') is True and
                any(self.config.get(key, '') == '' for key in ('platform_id', 'adapter'))):
            return
        try:
            await self._start_enabled()
        except RuntimeError:
            # Keep the authenticated setup page available to repair configuration.
            pass

    @_optional_loaded_hook()
    async def on_astrbot_loaded(self):
        self._discover()
        if self.config.get('enabled') is True and not self.ready:
            try:
                await self._start_enabled()
            except RuntimeError:
                # Already sanitized; an optional discovery hook must not stop AstrBot.
                pass

    async def _start_enabled(self):
        async with self._runtime_lock:
            if self.ready or self.admin.closed:
                return
            if self.tunnel is not None or self.route_runtimes:
                raise RuntimeError('tunnel_stop_incomplete')
            await self._start_enabled_unlocked()

    async def _start_enabled_unlocked(self):
        if self.config.get('enabled') is not True:
            return
        self.runtime_error = None
        try:
            from .sender_routes import parse_routes
            routes = parse_routes(self.config)
            if routes and self._route_parent is None:
                await self._start_sender_routes(routes)
                return
            from aiocqhttp import Event as CQEvent
            self._check_capabilities(CQEvent)
            self.raw_event_type = CQEvent
            try:
                from astrbot.api.message_components import Image, File
                self.media_types = {'image': Image, 'file': File}
            except ImportError:
                self.media_types = {}
            try:
                from astrbot.api.message_components import ComponentTypes
                if not isinstance(ComponentTypes, dict) or any(
                        not isinstance(key, str) or not isinstance(value, type)
                        for key, value in ComponentTypes.items()):
                    raise Rejected('unsupported_astrbot_api')
                self.component_types = dict(ComponentTypes)
            except ImportError:
                # A missing optional mapping cannot break module import or
                # enable arbitrary normalized components in older runtimes.
                self.component_types = {}
            binding = self._binding()
            self._upgrade_access_policy()
            proxy = proxy_from_config(self.config)
            callback_dns_mode = effective_dns(self.config, 'callback')
            attachment_dns_mode = effective_dns(self.config, 'attachment')
            self._ensure_mcp_token()
            runtime_revision = self.admin._revision()
            # Configuration is never interpolated into a path.
            root = self._route_root or Path(StarTools.get_data_dir('astrbot_plugin_dot_private'))
            store = Store(root / 'inbox.sqlite3', binding, policy_provider=self._active_policy)
            from .capacity_alert import CapacityAlert
            self.capacity_alert = CapacityAlert(root)
            if self.capacity_alert.status()['active']:
                self.runtime_error = self.capacity_alert.status()['code']
            self.host = Host(store, self._preflight, self._send)
            if self._route_parent is not None:
                self.host.route_active = self._route_is_active
            from .attachments import Attachments
            store.attachments = Attachments(store.db, root / 'attachments', proxy=proxy,
                attachment_dns=attachment_dns_mode,
                message_timing=store.message_timing,
                private_http_enabled=lambda: self.config.get('attachment_private_http') is True,
                group_http_enabled=self._group_cache_enabled)
            self.group_candidates = GroupFileCandidates()
            store.read_context_provider = self._group_file_read_context
            self.host.candidate_status = self._candidate_status
            self.host.read_attachment = self._read_attachment
            self.host.validate_read_message = self._validate_read_message
            self.host.validate_remote_diagnostic = self._validate_remote_diagnostic
            self.host.reply_attachment = self._reply_attachment
            self.host.reply_validated_attachment = self._reply_validated_attachment
            from .media_staging import MediaStaging
            captured_host = self.host
            store.reminders_enabled = lambda: (self.config.get('experimental_unread_reminders') is True and
                self.config.get('enabled') is True and self._route_is_active() and self.host is captured_host and not captured_host.stopping)
            def media_guard(message_id):
                if self.host is not captured_host or captured_host.stopping:
                    raise Rejected('binding_inactive')
                self._validate_read_message(message_id)
                self._validate_target(captured_host.store.message(message_id))
            self.host.media_staging = MediaStaging(self.host, guard=media_guard)
            self.host.capacity_status = lambda: self.capacity_alert.status() if self.capacity_alert else None
            self.host.discovery_status = lambda: dict(self.discovery_status)
            self.endpoint = Endpoint(self.host, self.config.get('mcp_token', ''),
                                     self.config.get('callback_hosts', []),
                                     self.config.get('listen_host', '127.0.0.1'),
                                     self.config.get('listen_port', 8769), proxy=proxy,
                                     callback_dns=callback_dns_mode,
                                     logger=logger, response_logger=self.response_logger)
            await self.endpoint.start()
            await self._start_tunnel(root)
            self._runtime_config_revision = runtime_revision
            self._runtime_root = root
            self.ready = True
        except asyncio.CancelledError:
            await self._stop_runtime_unlocked()
            raise
        except Exception as error:
            failure = str(error) if isinstance(error, (Rejected, EventError, AttachmentError)) and str(error) in {
                "history_migration_requires_space", "history_migration_busy", "storage_full", "storage_busy",
                "invalid_callback_dns", "callback_dns_requires_proxy",
                "invalid_attachment_dns", "attachment_dns_requires_proxy",
                "invalid_network_dns", "network_dns_requires_proxy",
                "access_policy_migration_failed"} else "startup_failed"
            # Never include config values or raw parse/network exception strings.
            try:
                await self._stop_runtime_unlocked()
            except Exception:
                pass
            self.runtime_error = failure
            logger.error('dot_private_startup_failed; check binding, dependencies and endpoint configuration')
            raise RuntimeError('dot_private_startup_failed') from None

    async def _start_sender_routes(self, routes):
        # No parent endpoint/DB: a message can enter only one route runtime.
        self._binding()
        self._upgrade_access_policy()
        self._ensure_mcp_token()
        revision = self.admin._revision()
        root = Path(StarTools.get_data_dir('astrbot_plugin_dot_private')) / 'routes'
        for route in routes:
            config = dict(self.config)
            config.update(sender_routes=[], tunnel_id=route.tunnel_id,
                          listen_port=route.listen_port, listen_host='127.0.0.1')
            child = Main(self.context, config, _route_parent=self,
                         _route_sender=route.sender_id, _route_root=root / route.sender_id)
            self.route_runtimes[route.sender_id] = child
            await child._start_enabled()
            if not child.ready:
                raise RuntimeError('route_start_failed')
        if self.admin._revision() != revision:
            raise RuntimeError('runtime_config_changed')
        self._routes_revision = revision
        self._runtime_config_revision = revision
        self._runtime_root = root
        self.ready = True

    def _ensure_mcp_token(self):
        # Only reached on an explicitly enabled/apply startup, never GET/status.
        if self.config.get('mcp_token', '') != '':
            return
        previous = self.config.get('mcp_token')
        present = 'mcp_token' in self.config
        save = getattr(self.config, 'save_config', None)
        if not callable(save):
            raise RuntimeError('managed_auth_save_failed')
        try:
            self.config['mcp_token'] = secrets.token_urlsafe(48)
            save()
        except Exception:
            if present:
                self.config['mcp_token'] = previous
            else:
                self.config.pop('mcp_token', None)
            raise RuntimeError('managed_auth_save_failed') from None

    @staticmethod
    def _check_capabilities(raw_event_type):
        required = ('get_platform_id', 'get_platform_name', 'get_self_id', 'get_sender_id',
                    'get_group_id', 'is_private_chat', 'get_messages', 'stop_event',
                    'plain_result', 'send')
        if (not isinstance(raw_event_type, type) or not issubclass(raw_event_type, dict) or
                not callable(getattr(StarTools, 'get_data_dir', None)) or
                any(not callable(getattr(AstrMessageEvent, name, None)) for name in required)):
            raise Rejected('unsupported_astrbot_api')

    def _snapshot(self, event, *, allow_unmentioned_group_file=False):
        if (self.raw_event_type is None or
                not isinstance(getattr(getattr(event, 'message_obj', None), 'raw_message', None),
                               self.raw_event_type) or
                any(not callable(getattr(event, name, None))
                    for name in ('stop_event', 'plain_result', 'send'))):
            raise Rejected('unsupported_adapter_event')
        return snapshot(event, self.host.store.binding, Plain, At,
                        media_types=self.media_types, component_types=self.component_types,
                        policy=self._policy(), allow_unmentioned_group_file=allow_unmentioned_group_file)

    def _binding(self):
        binding = Binding(self.config.get('platform_id', ''), self.config.get('adapter', ''),
                          self.config.get('self_id', ''), self.config.get('sender_id', ''))
        if binding.adapter != 'aiocqhttp':
            raise Rejected('unsupported_adapter')
        if binding.adapter == 'aiocqhttp' and any(not re.fullmatch(r'[1-9][0-9]{0,19}', v)
                for v in (binding.self_id, binding.sender_id)):
            raise Rejected('invalid_qq_identifier')
        return binding

    def _upgrade_access_policy(self):
        policy = self._policy()  # Validate every field before changing configuration.
        if self.config.get('access_rules_version', 0) == 1:
            return
        save = getattr(self.config, 'save_config', None)
        if not callable(save):
            # Synthetic/legacy dict callers keep the version-0 fallback contract.
            return
        changes = {'access_rules_version': 1}
        if not self.config.get('private_sender_ids', []):
            changes['private_sender_ids'] = sorted(policy.private_sender_ids)
        # v0.5 starts every existing group behind a real bot-@ trigger to avoid
        # flooding dot. Preserve sender scope; later version-1 edits are final.
        changes['group_rules'] = [
            {'__template_key': 'group', 'group_id': rule.group_id, 'mode': 'at',
             'at_sender_scope': ('selected' if rule.mode == 'selected' else
                                 'all' if rule.mode == 'all' else rule.at_sender_scope),
             'sender_ids': sorted(rule.sender_ids)}
            for rule in policy.group_rules]
        previous = {key: self.config[key] for key in changes if key in self.config}
        try:
            self.config.update(changes)
            save()
        except Exception:
            for key in changes:
                if key in previous:
                    self.config[key] = previous[key]
                else:
                    self.config.pop(key, None)
            raise Rejected('access_policy_migration_failed') from None

    def _route_is_active(self):
        parent = self._route_parent
        return (parent is None or
                (parent.ready and not parent.admin.closed and parent.config.get('enabled') is True and
                 parent._routes_revision == parent.admin._revision() and
                 parent.route_runtimes.get(self._route_sender) is self))

    def _policy(self):
        try:
            if self._route_parent is not None:
                from .sender_routes import restrict_policy
                return restrict_policy(self._route_parent._policy(), self._route_sender)
            return AccessPolicy.from_config(self.config, self._binding())
        except PolicyError as error:
            raise Rejected(str(error)) from None

    def _active_policy(self):
        if (not self._route_is_active() or not self.ready or self.config.get('enabled') is not True or
                self.host is None or self.host.stopping or
                self._binding() != self.host.store.binding):
            raise Rejected('binding_inactive')
        return self._policy()

    def _group_mode(self):
        mode = self.config.get('group_trigger_mode', 'owner_at')
        if mode not in ('owner_at', 'all_messages'):
            raise Rejected('invalid_group_trigger_mode')
        return mode

    def _groups(self):
        groups = self.config.get('group_ids', [])
        if (not isinstance(groups, list) or len(groups) > 100 or
                any(not isinstance(group, str) or not re.fullmatch(r'[1-9][0-9]{0,19}', group)
                    for group in groups) or len(set(groups)) != len(groups)):
            raise Rejected('invalid_group_whitelist')
        return frozenset(groups)

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=100)
    async def on_private_message(self, event: AstrMessageEvent):
        await self._on_message(event)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=100)
    async def on_group_message(self, event: AstrMessageEvent):
        await self._on_message(event)

    def _group_cache_enabled(self):
        return (self._route_is_active() and self.ready and self.config.get('enabled') is True and
                self.config.get('group_file_cache_enabled', True) is True)

    def _candidate_task(self, coroutine):
        task = asyncio.create_task(coroutine)
        self._candidate_tasks.add(task)
        def finished(done):
            self._candidate_tasks.discard(done)
            if not done.cancelled():
                done.exception()
        task.add_done_callback(finished)
        return task

    def _expire_candidates(self):
        for candidate in self.group_candidates.expire():
            self._discard_candidate(candidate)
        for key, candidate in list(self.group_candidates.candidates.items()):
            if not self._candidate_allowed(candidate):
                self.group_candidates.candidates.pop(key, None)
                self._discard_candidate(candidate)
        for key, request in list(self.group_candidates.requests.items()):
            try:
                self._validate_request(request)
            except (Rejected, ValueError):
                if request.task is not None and not request.task.done():
                    request.task.cancel()
                self.group_candidates.requests.pop(key, None)

    def _discard_candidate(self, candidate):
        if candidate.task is not None and not candidate.task.done():
            candidate.task.cancel()
        cache = candidate.host.store.attachments
        for spec in candidate.specs:
            cache.discard_candidate(candidate.id + '_' + str(spec['segment_index']))
        for mid, mapping in list(self.candidate_links.items()):
            if any(item[0] is candidate for item in mapping.values()):
                self.candidate_links.pop(mid, None)

    async def _candidate_cleanup(self):
        try:
            while (self.group_candidates.candidates or self.group_candidates.requests) and self.ready:
                await asyncio.sleep(1)
                self._expire_candidates()
        finally:
            self._candidate_cleanup_task = None

    def _candidate_status(self):
        self._expire_candidates()
        return {**self.group_candidates.status(), 'enabled': self._group_cache_enabled()}

    def _validate_candidate(self, candidate):
        if (not self._group_cache_enabled() or not self.group_candidates.live(candidate) or
                self.host is not candidate.host or candidate.host.stopping or
                self._binding() != candidate.host.store.binding or
                getattr(candidate.event, 'bot', None) is not candidate.bot):
            raise Rejected('candidate_context_unavailable')
        cache = candidate.host.store.attachments
        try:
            current_proxy = proxy_from_config(self.config)
            current_dns = effective_dns(self.config, 'attachment')
        except Exception:
            raise Rejected('binding_inactive') from None
        if (getattr(cache.transport, 'proxy', current_proxy) != current_proxy or
                getattr(cache.transport, 'attachment_dns', current_dns) != current_dns):
            raise Rejected('binding_inactive')
        current = self._snapshot(candidate.event, allow_unmentioned_group_file=True)
        if (current != candidate.value or current['conversation_type'] != 'group' or
                not self._policy().authorizes_stored(current)):
            raise Rejected('candidate_source_changed')
        return candidate.event

    async def _prefetch_candidate(self, candidate):
        cache = candidate.host.store.attachments
        captured_config = self.config
        captured_proxy = proxy_from_config(captured_config)
        captured_dns = effective_dns(captured_config, 'attachment')
        # A single bounded task per source message downloads its <=8 files
        # sequentially; shared cache slots bound network concurrency globally.
        for spec in candidate.specs:
            index = spec['segment_index']
            candidate.states[index] = 'fetching'
            started = time.monotonic_ns()
            candidate.timings[index] = {'started_at_ms': time.time_ns() // 1_000_000,
                                        'finished_at_ms': None, 'elapsed_ms': None}
            scopes = []
            def guard():
                self._validate_candidate(candidate)
                if (self.config is not captured_config or proxy_from_config(self.config) != captured_proxy or
                        effective_dns(self.config, 'attachment') != captured_dns or
                        getattr(cache.transport, 'proxy', captured_proxy) != captured_proxy or
                        getattr(cache.transport, 'attachment_dns', captured_dns) != captured_dns):
                    raise Rejected('binding_inactive')
            async def resolver(descriptor):
                guard()
                if any(descriptor.get(key) != spec[key] for key in ('segment_index', 'kind', 'url', 'file_id')):
                    raise AttachmentError('attachment_source_unavailable')
                raw = candidate.event.message_obj.raw_message
                if spec['file_id'] and callable(getattr(candidate.bot, 'call_action', None)):
                    http_scope = cache.group_http_read(descriptor, conversation_type='group',
                        bot=candidate.bot, self_id=raw['self_id'], group_id=raw['group_id'],
                        sender_id=raw['user_id'], platform_id=candidate.host.store.binding.platform_id,
                        guard=guard)
                    scopes.append(http_scope)
                    return await http_scope.resolve()
                if spec['url']:
                    cache.transport.validate(spec['url'])
                    return spec['url']
                raise AttachmentError('attachment_source_unavailable')
            try:
                guard()
                raw_data = candidate.event.message_obj.raw_message['message'][index]['data']
                validate_known_size(raw_data.get('file_size'))
                validate_known_size(raw_data.get('size'))
                remaining = max(0.001, candidate.expires_at - self.group_candidates.clock())
                token = await cache.prefetch_candidate(candidate.id + '_' + str(index), spec,
                    resolver, guard=guard, ttl_seconds=min(600, remaining))
                guard()
                candidate.tokens[index] = token
                candidate.states[index] = 'cached'
            except asyncio.CancelledError:
                candidate.states[index] = 'cancelled'
                raise
            except Exception as error:
                candidate.states[index] = 'failed'
                candidate.errors[index] = error.code if isinstance(error, AttachmentError) else 'candidate_unavailable'
                self.group_candidates.cache_failures += 1
            finally:
                candidate.timings[index].update(finished_at_ms=time.time_ns() // 1_000_000,
                    elapsed_ms=max(0, (time.monotonic_ns() - started) // 1_000_000))
                for http_scope in scopes:
                    http_scope.close()

    def _register_group_candidate(self, event, value):
        # Bound the retained raw source too, and never retain nested normalized
        # Reply/Forward chains as candidate provenance. This is not a process
        # RAM quota: AstrBot still owns the original event object.
        try:
            allowed = (Plain, At, *self.media_types.values())
            if any(type(item) not in allowed for item in event.get_messages()):
                return
            raw_bytes = 0
            for chunk in json.JSONEncoder(ensure_ascii=False).iterencode(event.message_obj.raw_message):
                raw_bytes += len(chunk.encode('utf-8'))
                if raw_bytes > 65536:
                    self.group_candidates.rejected_count += 1
                    return
        except (TypeError, ValueError, UnicodeError):
            return
        self._expire_candidates()
        candidate, fresh = self.group_candidates.add(value, event, self.host)
        if candidate is not None and fresh:
            candidate.task = self._candidate_task(self._prefetch_candidate(candidate))
            if self._candidate_cleanup_task is None:
                self._candidate_cleanup_task = self._candidate_task(self._candidate_cleanup())
            self.group_candidates.wake_scope(value)

    def _validate_request(self, request):
        if (not self._group_cache_enabled() or self.host is not request.host or
                getattr(request.event, 'bot', None) is not request.bot or
                self._snapshot(request.event) != request.value):
            raise Rejected('candidate_request_unavailable')

    def _publish_file_request(self, request, status, selections):
        self._validate_request(request)
        value = dict(request.value)
        context = {'status': status, 'untrusted': True,
                   'authorization': 'actual_trigger_text_only'}
        selection = None
        if status == 'matched':
            candidate, spec = selections[0]
            self._validate_candidate(candidate)
            if (candidate.value['group_id'], candidate.value['sender_id']) != (value['group_id'], value['sender_id']):
                raise Rejected('candidate_scope_mismatch')
            used = {item['segment_index'] for item in value['attachments']}
            mapped = next(i for i in range(256) if i not in used)
            value['linked_attachments'] = [{**spec, 'segment_index': mapped}]
            value['candidate_links'] = [{'segment_index': mapped, 'source_segment_index': spec['segment_index'],
                'candidate_id': candidate.id, 'expires_at': candidate.expires_wall}]
            context.update(origin='same_sender_group_file', expires_at=candidate.expires_wall,
                           source_received_at_ms=int(candidate.created_at * 1000),
                           prefetch_state=candidate.states.get(spec['segment_index'], 'queued'))
            selection = candidate, spec, mapped
        elif status == 'ambiguous':
            context['choices'] = [{'filename': spec['filename'], 'received_at': int(c.created_at),
                                    'untrusted': True} for c, spec in selections]
        value['group_file_context'] = context
        self._ingest_message(request.event, value, request.captured_at, candidate_selection=selection,
                             reminder_eligible=getattr(request, 'reminder_eligible', False))

    async def _wait_file_request(self, request):
        try:
            while True:
                self._validate_request(request)
                self._expire_candidates()
                status, selections = self.group_candidates.resolve(request.value)
                # Every candidate is revalidated before disclosing even its name.
                selections = [(c, spec) for c, spec in selections if self._candidate_allowed(c)]
                if status != 'waiting' and selections:
                    status = 'matched' if len(selections) == 1 else 'ambiguous'
                    self._publish_file_request(request, status, selections)
                    return
                remaining = request.deadline - self.group_candidates.clock()
                if remaining <= 0:
                    self._publish_file_request(request, 'no_file_found', [])
                    return
                request.wake.clear()
                try:
                    await asyncio.wait_for(request.wake.wait(), remaining)
                except asyncio.TimeoutError:
                    self._publish_file_request(request, 'no_file_found', [])
                    return
        except (Rejected, ValueError):
            return
        finally:
            key = candidate_identity(request.value)
            if self.group_candidates.requests.get(key) is request:
                self.group_candidates.requests.pop(key, None)

    def _candidate_allowed(self, candidate):
        try:
            self._validate_candidate(candidate)
            return True
        except (Rejected, ValueError):
            return False

    async def _on_message(self, event):
        if not self.ready or self.config.get('enabled') is not True or not self._route_is_active():
            return
        if self.route_runtimes:
            # Selection is not acceptance: the child revalidates normalized/raw
            # sender, bot, platform, group and current policy before capture.
            if self._routes_revision != self.admin._revision():
                return
            try:
                sender = event.get_sender_id()
                child = self.route_runtimes.get(str(sender)) if type(sender) in (str, int) else None
            except (AttributeError, ValueError, TypeError):
                return
            if child is not None:
                await child._on_message(event)
            return
        captured_at = (time.time_ns() // 1_000_000, time.monotonic_ns() // 1_000_000)
        try:
            if self._binding() != self.host.store.binding:
                return
            value = self._snapshot(event, allow_unmentioned_group_file=self._group_cache_enabled())
        except (Rejected, ValueError):
            return
        if not value['trigger_evidence']['trigger_matched']:
            # This is a validated candidate only, never a request or an Events
            # input. It must not claim the event or suppress AstrBot's LLM.
            if self._group_cache_enabled():
                self._register_group_candidate(event, value)
            return
        event.stop_event()
        if (self._group_cache_enabled() and value['conversation_type'] == 'group' and
                value['trigger_evidence']['configured_group_mode'] == 'at' and
                not value['attachments'] and file_intent(value)):
            try:
                duplicate = self.host.store.history.lookup_duplicate(value)
                if duplicate:
                    self._ingest_message(event, value, captured_at)
                    return
                request, fresh = self.group_candidates.add_request(value, event, self.host, captured_at)
                if request is None:
                    value['group_file_context'] = {'status': 'request_queue_full', 'untrusted': True}
                    self._ingest_message(event, value, captured_at)
                elif fresh:
                    request.reminder_eligible = self.host.store.reminders_enabled() is True
                    request.task = self._candidate_task(self._wait_file_request(request))
                    if self._candidate_cleanup_task is None:
                        self._candidate_cleanup_task = self._candidate_task(self._candidate_cleanup())
            except (Rejected, ValueError):
                pass
            return
        self._ingest_message(event, value, captured_at)

    def _ingest_message(self, event, value, captured_at, *, candidate_selection=None, reminder_eligible=None):
        try:
            message_id, _fresh = self.host.store.ingest(value, captured_at=captured_at, reminder_eligible=reminder_eligible)
            if candidate_selection is not None and _fresh:
                candidate, spec, mapped_index = candidate_selection
                self.candidate_links[message_id] = {mapped_index: (candidate, spec)}
                candidate.linked = True
            self.captured[message_id] = event
            self.captured_bots[message_id] = getattr(event, 'bot', None)
            if len(self.captured) > 1000:
                oldest = next(iter(self.captured))
                self.captured.pop(oldest)
                self.captured_bots.pop(oldest, None)
                self.candidate_links.pop(oldest, None)
            if _fresh and self.capacity_alert is not None:
                self.capacity_alert.note_success()
            if _fresh and self.runtime_error in ('storage_full', 'storage_busy', 'ingest_failed'):
                self.runtime_error = None
        except Exception as error:
            self.runtime_error = str(error) if isinstance(error, Rejected) and str(error) in ('storage_full', 'storage_busy') else 'ingest_failed'
            if isinstance(error, sqlite3.Error):
                code = self.host.store.history.note_storage_error(error)
                self.runtime_error = code if code in ('storage_full', 'storage_busy') else 'ingest_failed'
            if self.capacity_alert is not None:
                self.capacity_alert.note_rejection(self.runtime_error)
            now = time.monotonic()
            if not self._capacity_log_at or now - self._capacity_log_at >= 30:
                logger.error('dot_private_' + self.runtime_error + ' message_rejected; free_completed_history_then_resend')
                self._capacity_log_at = now

    def _validate_target(self, row):
        if not self._route_is_active() or not self.ready or self.config.get('enabled') is not True or self._binding() != self.host.store.binding:
            raise Rejected('binding_inactive')
        event = self.captured.get(row['id'])
        if event is None:
            # Captured reactive sends avoid unverified adapter fallback routing.
            # After reload, inbox survives but old QQ reply handles are unavailable.
            raise Rejected('reactive_context_unavailable')
        current = self._snapshot(event)
        if any(current[key] != row[key] for key in ('source_id', 'umo', 'text', 'conversation_type', 'group_id', 'sender_id')):
            raise Rejected('message_identity_changed')
        if json.loads(row.get('media_json', '[]')) != current.get('attachments', []):
            raise Rejected('message_identity_changed')
        return event

    def _validate_read_message(self, message_id):
        # Preserve current read permissions without selecting or attaching history.
        # Reading a captured message does not need an old reactive send handle.
        if (not self._route_is_active() or not self.ready or self.config.get('enabled') is not True or
                self.host is None or self.host.stopping or self._binding() != self.host.store.binding):
            raise Rejected('binding_inactive')
        row = self.host.store.message(message_id)
        try:
            allowed = self._policy().authorizes_stored(row)
        except Rejected:
            allowed = False
        if not allowed:
            raise Rejected('binding_inactive')

    def _linked_attachment_source(self, row, descriptor):
        links = json.loads(row.get('linked_media_json', '[]'))
        link = next((item for item in links if item['segment_index'] == descriptor['segment_index']), None)
        if link is None:
            return None
        mapping = self.candidate_links.get(row['id'], {}).get(descriptor['segment_index'])
        if mapping is None:
            raise Rejected('candidate_context_unavailable')
        candidate, spec = mapping
        self._validate_candidate(candidate)
        if (candidate.id != link['candidate_id'] or spec['segment_index'] != link['source_segment_index'] or
                candidate.expires_wall != link['expires_at'] or
                any(row[key] != candidate.value[key] for key in ('group_id', 'sender_id')) or
                any(descriptor.get(key) != spec[key] for key in ('kind', 'url', 'file_id'))):
            raise Rejected('candidate_scope_mismatch')
        return candidate, spec

    def _group_file_read_context(self, row):
        context = json.loads(row.get('group_file_context_json', '{}'))
        links = json.loads(row.get('linked_media_json', '[]'))
        for link in links:
            mapping = self.candidate_links.get(row['id'], {}).get(link['segment_index'])
            if mapping is None:
                context['linked_attachment_state'] = ('expired' if int(time.time()) >= link['expires_at']
                                                      else 'unavailable')
                return context
            candidate, spec = mapping
            if candidate.expires_at <= self.group_candidates.clock():
                context['linked_attachment_state'] = 'expired'
                return context
            try:
                self._validate_target(row)
                self._validate_candidate(candidate)
            except (Rejected, ValueError):
                context['linked_attachment_state'] = 'unavailable'
                return context
            context['linked_attachment_state'] = 'available'
            context['prefetch_state'] = candidate.states.get(spec['segment_index'], 'queued')
            context['prefetch_timing'] = dict(candidate.timings.get(spec['segment_index'], {}))
            if spec['segment_index'] in candidate.errors:
                context['cache_error'] = candidate.errors[spec['segment_index']]
        return context

    def _validate_remote_diagnostic(self, message_id):
        if self.config.get('remote_diagnostics_enabled') is not True:
            raise Rejected('remote_diagnostics_disabled')
        self._validate_read_message(message_id)
        row = self.host.store.message(message_id)
        if row['conversation_type'] != 'private' or row['sender_id'] != self.host.store.binding.sender_id:
            raise Rejected('remote_diagnostics_forbidden')

    async def _reply_attachment(self, message_id, kind, filename, data_base64, reply_key):
        from .media_reply import validate_media
        media = validate_media(kind, filename, data_base64)
        return await self._reply_validated_attachment(message_id, media, reply_key)

    async def _reply_validated_attachment(self, message_id, media, reply_key):
        # Internal entry for envelopes that already performed bounded media
        # validation. It is never an MCP argument or an arbitrary object hook.
        from .media_reply import MediaReply, send_media
        if type(media) is not MediaReply:
            raise Rejected('invalid_media_payload')
        host = self.host
        if host is None or host.stopping:
            raise Rejected('binding_inactive')
        event = self.captured.get(message_id)
        bot = self.captured_bots.get(message_id)
        async def dispatch(target, current_row, _fingerprint):
            def guard():
                if self.host is not host or host.stopping:
                    raise NotSent('binding_inactive')
                current = self._validate_target(current_row)
                if current is not event or self.captured_bots.get(message_id) is not bot:
                    raise NotSent('binding_inactive')
                return current
            return await send_media(event, current_row, media,
                bound_self_id=host.store.binding.self_id, captured_bot=bot, guard=guard)
        return await host.reply(message_id, MEDIA_REPLY_PREFIX + media.fingerprint,
                                reply_key, _send_override=dispatch)

    async def _read_attachment(self, message_id, attachment_id, *, include_raw=True):
        trace = ResponseTrace('main', message_id, logger=self.response_logger)
        timing = self.host.store.message_timing if self.host is not None else None
        try:
            result = await self._read_attachment_impl(message_id, attachment_id,
                                                      include_raw=include_raw, trace=trace)
        except asyncio.CancelledError:
            trace.finish('cancelled', 'cancelled')
            raise
        except Exception as error:
            if isinstance(error, AttachmentError):
                trace.set_download(getattr(error, 'transport_diagnostic', None))
            trace.finish('failed', 'dispatch_failed')
            raise
        else:
            trace.finish('failed' if result.get('isError') else 'succeeded',
                         'dispatch_failed' if result.get('isError') else None)
            return result
        finally:
            if timing is not None:
                timing.note_response(message_id, trace.snapshot())

    async def _read_attachment_impl(self, message_id, attachment_id, *, include_raw=True, trace):
        from .attachments import AttachmentError
        trace.mark('main_guard')
        row = self.host.store.message(message_id)
        captured_host = self.host
        trigger_event = self._validate_target(row)
        descriptor = captured_host.store.attachments.descriptor(message_id, attachment_id)
        linked = self._linked_attachment_source(row, descriptor)
        event = linked[0].event if linked else trigger_event
        captured_bot = getattr(event, 'bot', None)
        captured_config = self.config
        private_http_mode = captured_config.get('attachment_private_http') is True
        cache = captured_host.store.attachments
        try:
            captured_proxy = proxy_from_config(captured_config)
            attachment_dns_mode = effective_dns(captured_config, 'attachment')
        except Exception:
            raise Rejected('binding_inactive') from None
        if captured_proxy != getattr(cache.transport, 'proxy', captured_proxy):
            raise Rejected('binding_inactive')
        if attachment_dns_mode != getattr(cache.transport, 'attachment_dns', attachment_dns_mode):
            raise Rejected('binding_inactive')
        descriptor = cache.descriptor(message_id, attachment_id)
        raw_segments = event.message_obj.raw_message.get('message', [])
        index = linked[1]['segment_index'] if linked else descriptor['segment_index']
        if type(index) is int and 0 <= index < len(raw_segments):
            raw_data = raw_segments[index].get('data', {})
            if isinstance(raw_data, dict):
                validate_known_size(raw_data.get('file_size'))
                validate_known_size(raw_data.get('size'))
        http_scopes = []
        def guard():
            try:
                same_proxy = proxy_from_config(self.config) == captured_proxy
                same_dns = effective_dns(self.config, 'attachment') == attachment_dns_mode
            except Exception:
                same_proxy = same_dns = False
            if (not same_proxy or not same_dns or self.host is not captured_host or captured_host.stopping or
                    self.config is not captured_config or
                    (self.config.get('attachment_private_http') is True) != private_http_mode or
                    getattr(event, 'bot', None) is not captured_bot or
                    self._validate_target(row) is not trigger_event):
                raise Rejected('binding_inactive')
            if linked:
                current = self._linked_attachment_source(row, descriptor)
                if current is None or current[0] is not linked[0] or current[1] != linked[1]:
                    raise Rejected('candidate_context_unavailable')

        if linked:
            candidate, original_spec = linked
            if (candidate.task is not None and not candidate.task.done() and
                    candidate.states.get(original_spec['segment_index']) not in ('cached', 'failed')):
                # Join existing cache work without cancelling it if this MCP
                # caller disconnects. Its own idle/TTL guards remain in force.
                await asyncio.shield(candidate.task)
            guard()
            token = candidate.tokens.get(original_spec['segment_index'])
            if token is not None:
                cache.adopt_candidate(token, message_id, attachment_id, guard=guard)

        async def resolver(descriptor):
            guard()
            # The registered source must still name this exact captured segment.
            source = linked[1] if linked else next((item for item in json.loads(row.get('media_json', '[]'))
                           if item['segment_index'] == descriptor.get('segment_index')), None)
            if (descriptor.get('message_id') != message_id or descriptor.get('id') != attachment_id or
                    type(descriptor.get('segment_index')) is not int or source is None or
                    any(descriptor.get(key) != source[key] for key in ('kind', 'url', 'file_id'))):
                raise AttachmentError('attachment_source_unavailable')
            file_id = source['file_id']
            action, arguments = None, {}
            raw = event.message_obj.raw_message
            if source['kind'] == 'image' and safe_image_file_id(file_id):
                action = 'get_image'
                arguments = {'file': file_id, 'self_id': raw['self_id']}
            elif source['kind'] == 'file' and file_id:
                action = 'get_private_file_url'
                arguments = {'file_id': file_id, 'self_id': raw['self_id']}
                if row['conversation_type'] == 'group':
                    action = 'get_group_file_url'
                    arguments['group_id'] = raw['group_id']
            call_action = getattr(captured_bot, 'call_action', None)
            api_error = None
            api_exception = None
            if action == 'get_group_file_url' and self._group_cache_enabled() and callable(call_action):
                scope = cache.group_http_read(descriptor, conversation_type='group', bot=captured_bot,
                    self_id=raw['self_id'], group_id=raw['group_id'], sender_id=raw['user_id'],
                    platform_id=captured_host.store.binding.platform_id, guard=guard)
                http_scopes.append(scope)
                try:
                    note_attachment_api(action)
                    return await scope.resolve()
                except AttachmentError as error:
                    api_exception = error
                guard()
            elif action == 'get_private_file_url' and private_http_mode and callable(call_action):
                # The issuer itself performs the exact captured API call. A raw
                # URL, API response from another route, or model flag can never
                # mint this ephemeral capability.
                scope = cache.private_http_read(descriptor,
                    conversation_type=row['conversation_type'], bot=captured_bot,
                    self_id=raw['self_id'], guard=guard)
                http_scopes.append(scope)
                try:
                    note_attachment_api(action)
                    return await scope.resolve()
                except AttachmentError as error:
                    api_exception = error
                guard()
            elif action is not None and callable(call_action):
                original_https = False
                if action == 'get_image' and source['url']:
                    try:
                        cache.transport.validate(source['url'])
                        original_https = True
                    except AttachmentError:
                        pass
                try:
                    note_attachment_api(action)
                    query = call_action(action=action, **arguments)
                    # An optional metadata refresh must not consume the entire
                    # source-resolution budget before a captured HTTPS fallback.
                    result = await asyncio.wait_for(query, 3.0) if original_https else await query
                    note_attachment_api(action, outcome='succeeded')
                except asyncio.TimeoutError:
                    note_attachment_api(action, outcome='timeout')
                    api_error = 'attachment_api_failed'
                except Exception:
                    note_attachment_api(action, outcome='failed')
                    api_error = 'attachment_api_failed'
                # Revalidate on failures too, before considering an HTTPS fallback.
                guard()
                if api_error is None:
                    if isinstance(result, dict):
                        validate_known_size(result.get('file_size'))
                        validate_known_size(result.get('size'))
                    if not isinstance(result, dict) or not isinstance(result.get('url'), str) or not result['url']:
                        api_error = 'attachment_api_url_missing'
                    else:
                        try:
                            cache.transport.validate(result['url'])
                        except AttachmentError as error:
                            error.note_http_api_source(result['url'])
                            api_exception = error
                        else:
                            return result['url']
            if source['url']:
                try:
                    cache.transport.validate(source['url'])
                except AttachmentError:
                    if api_exception is not None:
                        raise api_exception from None
                    if api_error is not None:
                        raise AttachmentError(api_error) from None
                    raise
                return source['url']
            if api_exception is not None:
                raise api_exception from None
            if api_error is not None:
                raise AttachmentError(api_error)
            if action is not None:
                raise AttachmentError('attachment_api_unavailable')
            else:
                raise AttachmentError('attachment_source_unavailable')
        try:
            trace.mark('dispatch')
            result = await cache.read(message_id, attachment_id, resolver=resolver, guard=guard, include_raw=include_raw)
            trace.set_download(result.get('_attachment_read'))
            trace.mark('main_guard')
            guard()
        finally:
            for http_scope in http_scopes:
                http_scope.close()
        trace.mark('main_scope')
        read_diagnostic = result.pop('_attachment_read', {})
        attachment_metadata = json.loads(result['content'][0]['text'])
        scope = {'read_id': read_diagnostic.get('read_id'), 'message_id': message_id, 'conversation_type': row['conversation_type'],
                 'group_id': row['group_id'] or None, 'sender_id': row['sender_id'],
                 'privacy_scope': captured_host.store.privacy_scope(row),
                 'content_trust': 'untrusted_attachment_data',
                 'transport': attachment_metadata['transport'],
                 'integrity': attachment_metadata['integrity'],
                 'sha256_scope': attachment_metadata['sha256_scope'],
                 'text_preview': attachment_metadata['text_preview']}
        if 'delivery_mode' in attachment_metadata:
            scope.update(delivery_mode=attachment_metadata['delivery_mode'], raw_returned=attachment_metadata['raw_returned'])
        result['structuredContent'] = scope
        result['content'].insert(0, {'type': 'text', 'text': json.dumps(scope, ensure_ascii=False)})
        from .attachments import MAX_OUTPUT_BYTES
        trace.mark('main_json_budget')
        encoded_size = len(json.dumps(result, ensure_ascii=False).encode('utf-8'))
        trace.set_encoded_bytes(encoded_size)
        if encoded_size > MAX_OUTPUT_BYTES - 512:
            if "raw_returned" in scope:
                scope["raw_returned"] = False
            scope['text_preview'] = {**scope['text_preview'], 'available': False,
                                     'complete': False, 'truncated': False, 'reason': 'response_too_large'}
            result = {'content': [{'type': 'text', 'text': json.dumps({**scope, 'error': 'attachment_output_too_large'})}],
                      'structuredContent': scope, 'isError': True}
        return result

    async def _preflight(self, row):
        return self._validate_target(row)

    async def _send(self, event, row, text):
        # Recheck immediately before crossing into the adapter; no await in between.
        try:
            checked = self._validate_target(row)
        except Exception:
            raise NotSent('binding_inactive') from None
        if checked is not event or self.host.stopping:
            raise NotSent('binding_inactive')
        await event.send(event.plain_result(text))
        return True

    def _check_tunnel_reconnect(self, revision, host, endpoint):
        # The native configuration editor may change values outside Admin's
        # lock. Never use reconnect as an implicit apply of those changes.
        if self.admin.closed:
            raise AdminError('forbidden')
        if revision != self.admin._revision():
            raise AdminError('revision_conflict')
        if (self.config.get('enabled') is not True or not self.ready or
                host is None or endpoint is None or host.stopping or
                self.host is not host or self.endpoint is not endpoint):
            raise AdminError('runtime_not_ready')
        if self.config.get('tunnel_enabled') is not True:
            raise AdminError('tunnel_disabled')
        if (self._runtime_config_revision != revision or
                self._runtime_root is None):
            raise AdminError('runtime_config_changed')
        try:
            if self._binding() != host.store.binding:
                raise Rejected('binding_inactive')
        except (Rejected, ValueError, TypeError):
            raise AdminError('binding_inactive') from None

    def _validate_tunnel_reconnect(self):
        # Validate without saving, migrating, minting credentials, or starting
        # anything. A missing/invalid existing credential is an apply/setup job.
        self.admin._validate({})
        try:
            self._policy()
            proxy_from_config(self.config)
        except Exception:
            raise AdminError('invalid_settings') from None
        for name, limit in (('mcp_token', 1024), ('tunnel_runtime_key', 4096)):
            value = self.config.get(name, '')
            if (type(value) is not str or not 32 <= len(value) <= limit or
                    not value.isascii() or
                    any(ord(char) <= 32 or ord(char) >= 127 for char in value)):
                raise AdminError('invalid_settings')

    async def _close_reconnect_tunnel(self):
        """Finish a Tunnel-only close before allowing lifecycle lock release.

        Keep the supervisor on every incomplete stop, including a close that
        cancels itself. Caller cancellation cannot orphan its close task.
        """
        tunnel = self.tunnel
        if tunnel is None:
            return

        async def close():
            await tunnel.close()
            status = tunnel.status()
            if status.get('process_running') is not False:
                raise RuntimeError('tunnel_stop_incomplete')
            self.last_tunnel_status = status
            if self.tunnel is tunnel:
                self.tunnel = None

        task = asyncio.create_task(close())
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
            except Exception:
                break
        try:
            task.result()
        except BaseException:
            self.runtime_error = 'tunnel_stop_incomplete'
            logger.error('dot_private_tunnel_cleanup_failed')
            if cancelled:
                raise asyncio.CancelledError() from None
            raise AdminError('tunnel_stop_incomplete') from None
        if cancelled:
            raise asyncio.CancelledError()

    async def _reconnect_tunnel(self, revision):
        if self.route_runtimes:
            raise AdminError('multi_route_apply_required')
        async with self._runtime_lock:
            host, endpoint = self.host, self.endpoint
            self._check_tunnel_reconnect(revision, host, endpoint)
            self._validate_tunnel_reconnect()
            root = self._runtime_root
            await self._close_reconnect_tunnel()
            # Stop is an await boundary: disable, config edits, binding changes,
            # and terminate must all veto a replacement before it is launched.
            self._check_tunnel_reconnect(revision, host, endpoint)
            try:
                await self._start_tunnel(root)
                self._check_tunnel_reconnect(revision, host, endpoint)
                if self.tunnel is None:
                    raise AdminError('tunnel_reconnect_failed')
            except asyncio.CancelledError:
                try:
                    await self._close_reconnect_tunnel()
                except (AdminError, asyncio.CancelledError):
                    pass
                raise
            except Exception as error:
                await self._close_reconnect_tunnel()
                if isinstance(error, AdminError):
                    raise
                self.runtime_error = 'tunnel_reconnect_failed'
                raise AdminError('tunnel_reconnect_failed') from None
            if self.runtime_error in ('tunnel_stop_incomplete', 'tunnel_reconnect_failed'):
                self.runtime_error = None

    async def _stop_runtime(self):
        task = asyncio.create_task(self._stop_runtime_serialized())
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        task.result()
        if cancelled:
            raise asyncio.CancelledError()

    async def _stop_runtime_serialized(self):
        async with self._runtime_lock:
            await self._stop_runtime_unlocked()

    async def _stop_runtime_unlocked(self):
        self.ready = False
        if self.route_runtimes:
            failed = False
            cancelled = False
            for sender, child in list(self.route_runtimes.items()):
                try:
                    await child._stop_runtime()
                except asyncio.CancelledError:
                    cancelled = True
                except Exception:
                    failed = True
                else:
                    del self.route_runtimes[sender]
            self._routes_revision = None
            if failed:
                self.runtime_error = 'tunnel_stop_incomplete'
                raise RuntimeError('tunnel_stop_incomplete')
            if cancelled:
                raise asyncio.CancelledError()
        self.ready = False
        self.group_candidates.closed = True
        for task in tuple(self._candidate_tasks):
            task.cancel()
        if self._candidate_tasks:
            await asyncio.gather(*tuple(self._candidate_tasks), return_exceptions=True)
        for candidate in list(self.group_candidates.candidates.values()):
            self._discard_candidate(candidate)
        self.group_candidates.candidates.clear()
        self.group_candidates.requests.clear()
        self.candidate_links.clear()
        self._runtime_config_revision = None
        self._runtime_root = None
        tunnel = self.tunnel
        stop_failed = False
        cancelled = False
        if tunnel:
            try:
                prior = tunnel.status()
                await self._close_reconnect_tunnel()
                self.last_tunnel_status = tunnel.status()
                if prior.get('last_error_code'):
                    self.last_tunnel_status['last_error_code'] = prior['last_error_code']
                self.tunnel = None
            except asyncio.CancelledError:
                cancelled = True
                # The close helper retains the supervisor if shutdown failed.
            except Exception:
                stop_failed = True
                self.runtime_error = 'tunnel_stop_incomplete'
                logger.error('dot_private_tunnel_cleanup_failed')
        host, endpoint = self.host, self.endpoint
        self.host = self.endpoint = None
        if host:
            host.stopping = True
        try:
            if host:
                staging = getattr(host, 'media_staging', None)
                if staging is not None:
                    try:
                        staging.close()
                    except Exception:
                        logger.error('dot_private_media_staging_cleanup_failed')
            if endpoint:
                try:
                    await endpoint.close()
                except asyncio.CancelledError:
                    cancelled = True
                except Exception:
                    logger.error('dot_private_endpoint_cleanup_failed')
        finally:
            try:
                if host:
                    try:
                        staging = getattr(host, 'media_staging', None)
                        if staging is not None:
                            await staging.wait_closed()
                    finally:
                        await host.close()
            finally:
                if host:
                    try:
                        if host.store.attachments:
                            await host.store.attachments.close()
                    finally:
                        host.store.close()
                self.captured.clear()
                self.captured_bots.clear()
        if cancelled:
            raise asyncio.CancelledError()
        if stop_failed:
            raise RuntimeError('tunnel_stop_incomplete')

    async def terminate(self):
        self.admin.close()
        task = asyncio.create_task(self._terminate_serialized())
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        task.result()
        if cancelled:
            raise asyncio.CancelledError()

    async def _terminate_serialized(self):
        async with self.admin._lock:
            await self._stop_runtime()
