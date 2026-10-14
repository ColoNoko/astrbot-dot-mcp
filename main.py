"""Native AstrBot plugin. No model/provider call, no arbitrary-recipient tool."""
from pathlib import Path
import asyncio
import re
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, StarTools
from astrbot.api.message_components import Plain, At
from .host import Binding, Host, Store, Rejected, NotSent, snapshot
from .endpoint import Endpoint
from .discovery import discover_defaults
from .admin import Admin, AdminError
import json
import os
import platform
import stat
import tempfile
import secrets
from .proxy_config import proxy_from_config


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
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.host = None
        self.endpoint = None
        self.captured = {}
        self.ready = False
        self.raw_event_type = None
        self.discovery_status = {'state': 'not_checked', 'candidate_ids': []}
        self.tunnel = None
        self._runtime_lock = asyncio.Lock()
        self.runtime_error = None
        self.last_tunnel_status = None
        self.admin = Admin(self)
        self._register_admin()

    def _register_admin(self):
        register = getattr(self.context, 'register_web_api', None)
        if not callable(register):
            return  # Older/mocked runtimes cannot expose an unauthenticated fallback.
        for name, method, handler in (('status', 'GET', self.page_status),
                                      ('settings', 'POST', self.page_settings),
                                      ('action', 'POST', self.page_action)):
            register('/astrbot_plugin_dot_private/' + name, handler, [method],
                     'dot connection management')

    async def _page(self, kind):
        from astrbot.api.web import request, json_response, error_response
        headers = {'Cache-Control': 'no-store'}
        try:
            self.admin.authorize(request)
            if kind == 'status':
                return json_response(self.admin.status(request), headers=headers)
            if request.content_type is None or request.content_type.split(';')[0] != 'application/json':
                raise AdminError('invalid_settings')
            body = await request.body()
            if len(body) > 16384:
                raise AdminError('invalid_settings')
            try:
                payload = json.loads(body)
            except (ValueError, UnicodeError):
                raise AdminError('invalid_settings') from None
            result = await self.admin.mutate(request, payload, kind)
            return json_response(result, headers=headers)
        except AdminError as exc:
            code = str(exc)
            return error_response(code, status_code=403 if code == 'forbidden' else 400,
                                  headers=headers)
        except Exception:
            return error_response('runtime_failed', status_code=500, headers=headers)

    async def page_status(self):
        return await self._page('status')

    async def page_settings(self):
        return await self._page('settings')

    async def page_action(self):
        return await self._page('action')

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
            if self.tunnel is not None:
                raise RuntimeError('tunnel_stop_incomplete')
            await self._start_enabled_unlocked()

    async def _start_enabled_unlocked(self):
        if self.config.get('enabled') is not True:
            return
        self.runtime_error = None
        try:
            from aiocqhttp import Event as CQEvent
            self._check_capabilities(CQEvent)
            self.raw_event_type = CQEvent
            binding = self._binding()
            self._groups()
            self._group_mode()
            proxy = proxy_from_config(self.config)
            self._ensure_mcp_token()
            # Configuration is never interpolated into a path.
            root = Path(StarTools.get_data_dir('astrbot_plugin_dot_private'))
            store = Store(root / 'inbox.sqlite3', binding)
            self.host = Host(store, self._preflight, self._send)
            self.host.discovery_status = lambda: dict(self.discovery_status)
            self.endpoint = Endpoint(self.host, self.config.get('mcp_token', ''),
                                     self.config.get('callback_hosts', []),
                                     self.config.get('listen_host', '127.0.0.1'),
                                     self.config.get('listen_port', 8769), proxy=proxy)
            await self.endpoint.start()
            await self._start_tunnel(root)
            self.ready = True
        except asyncio.CancelledError:
            await self._stop_runtime_unlocked()
            raise
        except Exception:
            # Never include config values or raw parse/network exception strings.
            try:
                await self._stop_runtime_unlocked()
            except Exception:
                pass
            self.runtime_error = 'startup_failed'
            logger.error('dot_private_startup_failed; check binding, dependencies and endpoint configuration')
            raise RuntimeError('dot_private_startup_failed') from None

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

    def _snapshot(self, event):
        if (self.raw_event_type is None or
                not isinstance(getattr(getattr(event, 'message_obj', None), 'raw_message', None),
                               self.raw_event_type) or
                any(not callable(getattr(event, name, None))
                    for name in ('stop_event', 'plain_result', 'send'))):
            raise Rejected('unsupported_adapter_event')
        return snapshot(event, self.host.store.binding, Plain, At, self._groups(), self._group_mode())

    def _binding(self):
        binding = Binding(self.config.get('platform_id', ''), self.config.get('adapter', ''),
                          self.config.get('self_id', ''), self.config.get('sender_id', ''))
        if binding.adapter != 'aiocqhttp':
            raise Rejected('unsupported_adapter')
        if binding.adapter == 'aiocqhttp' and any(not re.fullmatch(r'[1-9][0-9]{0,19}', v)
                for v in (binding.self_id, binding.sender_id)):
            raise Rejected('invalid_qq_identifier')
        return binding

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

    async def _on_message(self, event):
        if not self.ready or self.config.get('enabled') is not True:
            return
        try:
            if self._binding() != self.host.store.binding:
                return
            value = self._snapshot(event)
        except (Rejected, ValueError):
            return
        # Matched valid text is exclusively routed to dot, including duplicates.
        # Stop before any queue/database work so the default LLM never answers it.
        event.stop_event()
        try:
            message_id, _fresh = self.host.store.ingest(value)
            self.captured[message_id] = event
            if len(self.captured) > 1000:
                self.captured.pop(next(iter(self.captured)))
        except Exception:
            logger.error('dot_private_ingest_failed')

    def _validate_target(self, row):
        if not self.ready or self.config.get('enabled') is not True or self._binding() != self.host.store.binding:
            raise Rejected('binding_inactive')
        event = self.captured.get(row['id'])
        if event is None:
            # Captured reactive sends avoid unverified adapter fallback routing.
            # After reload, inbox survives but old QQ reply handles are unavailable.
            raise Rejected('reactive_context_unavailable')
        current = self._snapshot(event)
        if any(current[key] != row[key] for key in ('source_id', 'umo', 'text', 'conversation_type', 'group_id', 'sender_id')):
            raise Rejected('message_identity_changed')
        return event

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
        tunnel = self.tunnel
        stop_failed = False
        cancelled = False
        if tunnel:
            try:
                prior = tunnel.status()
                await tunnel.close()
                self.last_tunnel_status = tunnel.status()
                if prior.get('last_error_code'):
                    self.last_tunnel_status['last_error_code'] = prior['last_error_code']
                self.tunnel = None
            except asyncio.CancelledError:
                cancelled = True
                self.tunnel = None
            except Exception:
                stop_failed = True
                self.runtime_error = 'tunnel_stop_incomplete'
                logger.error('dot_private_tunnel_cleanup_failed')
        host, endpoint = self.host, self.endpoint
        self.host = self.endpoint = None
        if host:
            host.stopping = True
        try:
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
                    await host.close()
            finally:
                if host:
                    host.store.close()
                self.captured.clear()
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
