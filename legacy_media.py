"""Explicit bounded media commands carried by the legacy qq_reply text field.

Only an authenticated qq_reply tool invocation may call this dispatcher. Never
run this parser on a captured message, attachment text, preview or history.
The existing reader and sender remain responsible for current permissions,
captured-conversation identity, durable reply-key claims and uncertain sends.
This module does not fetch URLs, open paths, execute content or stage uploads.
"""

from dataclasses import dataclass, field
import json
import re

from .host import Rejected
from .media_reply import MAX_MEDIA_BYTES, MediaReply, _filename, validate_media


PREFIX = 'DOT_MEDIA_V1\n'
MAX_ENVELOPE_BYTES = 32 * 1024 * 1024
MAX_METADATA_BYTES = 16384
_RESERVED_PREFIX = 'DOT_MEDIA_'
_MESSAGE_ID = re.compile(r'msg_[a-f0-9]{32}\Z')
_ATTACHMENT_ID = re.compile(r'att_[a-f0-9]{32}\Z')
_UPLOAD_ID = re.compile(r'upl_[a-f0-9]{32}\Z')
_REPLY_KEY = re.compile(r'[A-Za-z0-9_.:-]{1,128}\Z')
_SHA256 = re.compile(r'[a-f0-9]{64}\Z')
LEGACY_MEDIA_ERRORS = frozenset({
    'invalid_legacy_media_envelope', 'legacy_media_attachment_unavailable',
    'legacy_media_attachment_mismatch', 'legacy_media_staging_unavailable',
})


@dataclass(frozen=True)
class LegacyMediaRequest:
    op: str
    attachment_id: str = ''
    media: MediaReply | None = field(default=None, repr=False)
    staging_args: tuple = field(default=(), repr=False)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def _constant(_value):
    raise ValueError()


def _bounded_utf8(text, limit):
    if not isinstance(text, str) or len(text) > limit:
        raise ValueError()
    size = 0
    # Avoid allocating a worst-case 128 MiB UTF-8 copy just to reject its size.
    for offset in range(0, len(text), 65536):
        size += len(text[offset:offset + 65536].encode('utf-8'))
        if size > limit:
            raise ValueError()


def _json_object(text, error, *, limit=MAX_METADATA_BYTES):
    try:
        if limit is not None:
            _bounded_utf8(text, limit)
        value = json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant)
        if type(value) is not dict:
            raise ValueError()
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise Rejected(error) from None


def parse_legacy_media(text):
    """Return None for ordinary text; reserved but malformed commands reject.

    Exact V1 forms after PREFIX:
      {"op":"return_attachment","attachment_id":"att_<32 lowercase hex>"}
      {"op":"inline","kind":"image|file","filename":"name","data_base64":"..."}

    No extra fields, duplicate JSON keys, source locations or recipients exist.
    The envelope has a separate 32 MiB ceiling; decoded media remain <=20 MB.
    Ordinary text retains the endpoint's independent 16 KiB limit. Staged
    begin/chunk/commit/abort operations use the same exact versioned prefix.
    """
    if not isinstance(text, str):
        raise Rejected('invalid_legacy_media_envelope')
    if not text.startswith(_RESERVED_PREFIX):
        return None
    try:
        if not text.startswith(PREFIX):
            raise ValueError()
        _bounded_utf8(text, MAX_ENVELOPE_BYTES)
    except (ValueError, UnicodeError):
        raise Rejected('invalid_legacy_media_envelope') from None
    # The complete envelope, including PREFIX, was budgeted exactly once above.
    value = _json_object(text[len(PREFIX):], 'invalid_legacy_media_envelope', limit=None)
    if value.get('op') == 'return_attachment' and set(value) == {'op', 'attachment_id'}:
        aid = value['attachment_id']
        if isinstance(aid, str) and _ATTACHMENT_ID.fullmatch(aid):
            return LegacyMediaRequest('return_attachment', attachment_id=aid)
    elif value.get('op') == 'inline' and set(value) == {'op', 'kind', 'filename', 'data_base64'}:
        return LegacyMediaRequest('inline', media=validate_media(
            value['kind'], value['filename'], value['data_base64']))
    elif value.get('op') == 'begin' and set(value) == {'op', 'kind', 'filename', 'size_bytes', 'sha256'}:
        if (value['kind'] in ('image', 'file') and isinstance(value['filename'], str) and
                type(value['size_bytes']) is int and 1 <= value['size_bytes'] <= MAX_MEDIA_BYTES and
                isinstance(value['sha256'], str) and _SHA256.fullmatch(value['sha256'])):
            return LegacyMediaRequest('begin', staging_args=(
                value['kind'], _filename(value['filename']), value['size_bytes'], value['sha256']))
    elif value.get('op') == 'chunk' and set(value) == {'op', 'upload_id', 'offset', 'data_base64'}:
        from .media_staging import MAX_CHUNK_BASE64_CHARS
        if (isinstance(value['upload_id'], str) and _UPLOAD_ID.fullmatch(value['upload_id']) and
                type(value['offset']) is int and 0 <= value['offset'] < MAX_MEDIA_BYTES and
                isinstance(value['data_base64'], str) and
                1 <= len(value['data_base64']) <= MAX_CHUNK_BASE64_CHARS):
            return LegacyMediaRequest('chunk', staging_args=(
                value['upload_id'], value['offset'], value['data_base64']))
    elif value.get('op') in ('commit', 'abort') and set(value) == {'op', 'upload_id'}:
        if isinstance(value['upload_id'], str) and _UPLOAD_ID.fullmatch(value['upload_id']):
            return LegacyMediaRequest(value['op'], staging_args=(value['upload_id'],))
    raise Rejected('invalid_legacy_media_envelope')


def _returned_media(result, message_id, attachment_id):
    """Read only the guarded reader's fixed metadata/raw slots, never previews.

    Main returns scope text, public attachment metadata, exactly one raw image
    or resource, then optional untrusted text previews. Match both opaque IDs,
    the exact resource URI (if present), content type, byte count and SHA-256.
    A hash establishes local consistency only, not source authenticity.
    """
    error = 'legacy_media_attachment_unavailable'
    if (type(result) is not dict or result.get('isError') is not False or
            type(result.get('structuredContent')) is not dict):
        raise Rejected(error)
    if result['structuredContent'].get('message_id') != message_id:
        raise Rejected('legacy_media_attachment_mismatch')
    content = result.get('content')
    if (type(content) is not list or not 3 <= len(content) <= 5 or
            any(type(block) is not dict for block in content) or
            content[0].get('type') != 'text' or content[1].get('type') != 'text' or
            any(block.get('type') != 'text' for block in content[3:])):
        raise Rejected(error)
    metadata = _json_object(content[1].get('text'), error)
    if (metadata.get('message_id') != message_id or
            metadata.get('attachment_id') != attachment_id):
        raise Rejected('legacy_media_attachment_mismatch')
    if (metadata.get('kind') not in ('image', 'file') or
            type(metadata.get('size_bytes')) is not int or
            not 1 <= metadata['size_bytes'] <= MAX_MEDIA_BYTES or
            not isinstance(metadata.get('sha256'), str) or
            not _SHA256.fullmatch(metadata['sha256']) or
            not isinstance(metadata.get('mime_type'), str)):
        raise Rejected(error)
    raw = content[2]
    if raw.get('type') == 'image' and metadata['kind'] == 'image':
        data, mime = raw.get('data'), raw.get('mimeType')
    elif raw.get('type') == 'resource' and type(raw.get('resource')) is dict:
        resource = raw['resource']
        if resource.get('uri') != 'qq-attachment://' + message_id + '/' + attachment_id:
            raise Rejected('legacy_media_attachment_mismatch')
        data, mime = resource.get('blob'), resource.get('mimeType')
    else:
        raise Rejected(error)
    media = validate_media(metadata['kind'], metadata.get('filename'), data)
    if (media.size_bytes != metadata['size_bytes'] or media.sha256 != metadata['sha256'] or
            media.mime_type != metadata['mime_type'] or mime != metadata['mime_type']):
        raise Rejected('legacy_media_attachment_mismatch')
    return media


async def dispatch_legacy_media(host, message_id, reply_key, request):
    """Use only same-message reader/sender hooks; preserve the exact reply key.

    Original attachment bytes never travel through the model or qq_reply text.
    They remain bounded by the existing 20 MB reader and sender validators.
    No send fallback or automatic retry is performed, including on cancellation.
    """
    if not isinstance(message_id, str) or not _MESSAGE_ID.fullmatch(message_id):
        raise Rejected('invalid_message_id')
    if not isinstance(reply_key, str) or not _REPLY_KEY.fullmatch(reply_key):
        raise Rejected('invalid_reply_key')
    if type(request) is not LegacyMediaRequest:
        raise Rejected('invalid_legacy_media_envelope')
    if request.op in ('begin', 'chunk', 'commit', 'abort'):
        if request.attachment_id or request.media is not None:
            raise Rejected('invalid_legacy_media_envelope')
        staging = getattr(host, 'media_staging', None)
        operation = getattr(staging, request.op, None)
        if not callable(operation):
            raise Rejected('legacy_media_staging_unavailable')
        expected = {'begin': 4, 'chunk': 3, 'commit': 1, 'abort': 1}[request.op]
        if type(request.staging_args) is not tuple or len(request.staging_args) != expected:
            raise Rejected('invalid_legacy_media_envelope')
        result = operation(message_id, reply_key, *request.staging_args)
        return await result if request.op == 'commit' else result
    if request.staging_args:
        raise Rejected('invalid_legacy_media_envelope')
    validated_sender = getattr(host, 'reply_validated_attachment', None)
    sender = getattr(host, 'reply_attachment', None)
    if not callable(validated_sender) and not callable(sender):
        raise Rejected('binding_inactive')
    if request.op == 'return_attachment':
        if (request.media is not None or not isinstance(request.attachment_id, str) or
                not _ATTACHMENT_ID.fullmatch(request.attachment_id)):
            raise Rejected('invalid_legacy_media_envelope')
        reader = getattr(host, 'read_attachment', None)
        if not callable(reader):
            raise Rejected('legacy_media_attachment_unavailable')
        result = await reader(message_id, request.attachment_id, include_raw=True)
        media = _returned_media(result, message_id, request.attachment_id)
    elif request.op == 'inline' and request.attachment_id == '' and type(request.media) is MediaReply:
        media = request.media
    else:
        raise Rejected('invalid_legacy_media_envelope')
    # Main's private typed hook avoids hashing already validated 20 MB again.
    # Both sender routes recheck permissions and own durable deduplication.
    if callable(validated_sender):
        return await validated_sender(message_id, media, reply_key)
    return await sender(message_id, media.kind, media.filename, media.data_base64, reply_key)
