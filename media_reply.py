"""Bounded outbound bytes for a single, already authorized QQ conversation.

This module accepts no source path, source URL, arbitrary action or destination
from MCP. Main supplies the original captured event/bot and a synchronous
current-binding guard. Host owns the durable reply key and cancellation state.

Images use OneBot's base64 image segment. Files use the base64 file-segment
extension documented by NapCat; not every OneBot implementation supports it.
There is deliberately no local-file, callback URL or upload-API fallback.
"""

import base64
import binascii
from dataclasses import dataclass, field
import hashlib
import json
import re
import unicodedata

from .attachments import sniff_mime
from .host import NotSent, Rejected


MAX_MEDIA_BYTES = 20_000_000
MAX_BASE64_CHARS = 4 * ((MAX_MEDIA_BYTES + 2) // 3)
MAX_FILENAME_BYTES = 180
_QQ_ID = re.compile(r'[1-9][0-9]{0,19}\Z')
_MESSAGE_ID = re.compile(r'-?[1-9][0-9]{0,18}\Z')
_BASE64_ALPHABET = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'
_IMAGE_MIMES = frozenset(('image/png', 'image/jpeg', 'image/gif', 'image/webp'))
_DEVICE_NAMES = frozenset(('CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)),
                           *(f'LPT{i}' for i in range(1, 10))))


def _filename(value):
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_FILENAME_BYTES:
        raise Rejected('invalid_media_filename')
    name = unicodedata.normalize('NFKC', value)
    if (not name or name in ('.', '..') or name != name.strip() or name.endswith('.') or
            any(c in '/\\:*?"<>|' or unicodedata.category(c).startswith('C') for c in name) or
            name.split('.', 1)[0].upper() in _DEVICE_NAMES):
        raise Rejected('invalid_media_filename')
    try:
        if len(name.encode('utf-8')) > MAX_FILENAME_BYTES:
            raise Rejected('invalid_media_filename')
    except UnicodeError:
        raise Rejected('invalid_media_filename') from None
    return name


@dataclass(frozen=True)
class MediaReply:
    """Validated payload; only fingerprint/metadata may enter durable storage."""

    kind: str
    filename: str
    data_base64: str = field(repr=False)
    size_bytes: int
    sha256: str
    mime_type: str

    @property
    def fingerprint(self):
        return json.dumps({'v': 1, 'kind': self.kind, 'filename': self.filename,
                           'size_bytes': self.size_bytes, 'sha256': self.sha256},
                          ensure_ascii=False, sort_keys=True, separators=(',', ':'))

    def segment(self):
        # Always an object segment. Filename/data are never parsed as CQ text.
        return {'type': self.kind,
                'data': {'file': 'base64://' + self.data_base64, 'name': self.filename}}


def validate_media(kind, filename, data_base64):
    """Validate without opening files, fetching URLs or decoding an image.

    Base64 is canonical RFC 4648: no whitespace, prefixes, URL-safe alphabet,
    missing/extra padding or nonzero padding bits. The size is checked before
    allocating decoded bytes and again after decoding. Only base64 and small
    digest metadata survive this call; decoded bytes are not persisted.
    """
    if kind not in ('image', 'file') or not isinstance(kind, str):
        raise Rejected('invalid_media_kind')
    name = _filename(filename)
    if not isinstance(data_base64, str) or not data_base64:
        raise Rejected('invalid_media_base64')
    if len(data_base64) > MAX_BASE64_CHARS:
        raise Rejected('media_too_large')
    if len(data_base64) % 4:
        raise Rejected('invalid_media_base64')
    padding = 2 if data_base64.endswith('==') else int(data_base64.endswith('='))
    estimated = len(data_base64) // 4 * 3 - padding
    if estimated > MAX_MEDIA_BYTES:
        raise Rejected('media_too_large')
    try:
        if padding and (_BASE64_ALPHABET.index(data_base64[-padding - 1]) &
                        (15 if padding == 2 else 3)):
            raise ValueError()
        data = base64.b64decode(data_base64, validate=True)
    except (ValueError, binascii.Error, UnicodeError):
        raise Rejected('invalid_media_base64') from None
    if not data or len(data) != estimated:
        raise Rejected('invalid_media_base64')
    if len(data) > MAX_MEDIA_BYTES:
        raise Rejected('media_too_large')
    mime_type = sniff_mime(data[:512])
    if kind == 'image' and mime_type not in _IMAGE_MIMES:
        raise Rejected('unsupported_media_image')
    return MediaReply(kind, name, data_base64, len(data), hashlib.sha256(data).hexdigest(), mime_type)


def _qq_id(value):
    return type(value) in (str, int) and _QQ_ID.fullmatch(str(value)) is not None


def _acknowledged(result):
    """Only an explicit message ID acknowledges adapter completion, not delivery."""
    if not isinstance(result, dict):
        return False
    if ('status' in result and result['status'] != 'ok') or (
            'retcode' in result and (type(result['retcode']) is not int or result['retcode'] != 0)):
        return False
    value = result.get('message_id')
    if type(value) not in (str, int) or not _MESSAGE_ID.fullmatch(str(value)):
        return False
    return -(2 ** 63) <= int(value) <= 2 ** 63 - 1


async def send_media(event, row, media, *, bound_self_id, captured_bot, guard):
    """Perform one adapter call using only Main's guarded captured route.

    guard must synchronously return this exact event, having checked active host,
    binding, current group whitelist and captured-event identity. Main must retain
    captured_bot from ingestion, not read a replacement from the event now.
    Pre-entry rejection is NotSent. Adapter exceptions/cancellation propagate to
    Host, which must mark uncertain and never automatically resend.
    """
    if type(media) is not MediaReply:
        raise NotSent('invalid_media_payload')
    try:
        if guard() is not event or getattr(event, 'bot', None) is not captured_bot:
            raise NotSent('binding_inactive')
        call_action = getattr(captured_bot, 'call_action', None)
        raw = event.message_obj.raw_message
        if (not callable(call_action) or not isinstance(raw, dict) or
                raw.get('post_type') != 'message' or
                not _qq_id(bound_self_id) or not _qq_id(raw.get('self_id')) or
                str(raw['self_id']) != str(bound_self_id) or
                not _qq_id(raw.get('user_id')) or not _qq_id(row.get('sender_id')) or
                str(raw['user_id']) != str(row['sender_id']) or
                str(raw['user_id']) == str(bound_self_id) or
                type(raw.get('message_id')) not in (str, int) or
                str(raw['message_id']) != row.get('source_id') or
                not isinstance(raw.get('sender'), dict) or
                not _qq_id(raw['sender'].get('user_id')) or
                str(raw['sender']['user_id']) != str(row['sender_id'])):
            raise NotSent('binding_inactive')
        kind = row.get('conversation_type')
        if kind == 'private' and raw.get('message_type') == 'private':
            if row.get('group_id') != '' or raw.get('group_id') not in (None, ''):
                raise NotSent('binding_inactive')
            action = 'send_private_msg'
            params = {'user_id': int(row['sender_id'])}
        elif kind == 'group' and raw.get('message_type') == 'group':
            if (not _qq_id(row.get('group_id')) or not _qq_id(raw.get('group_id')) or
                    str(raw['group_id']) != str(row['group_id'])):
                raise NotSent('binding_inactive')
            action = 'send_group_msg'
            params = {'group_id': int(row['group_id'])}
        else:
            raise NotSent('binding_inactive')
        params['self_id'] = raw['self_id']
        params['message'] = [media.segment()]
    except NotSent:
        raise
    except Exception:
        raise NotSent('binding_inactive') from None
    # No await, source resolution, hook or temporary-file operation precedes
    # this one adapter entry after the synchronous guard/route checks.
    result = await call_action(action=action, **params)
    return _acknowledged(result)
