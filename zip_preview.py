"""Bounded ZIP central-directory metadata, never member contents.

The caller already owns bounded downloaded bytes. This parser never opens a
path, reads local file headers/member data, decompresses, extracts, executes, or
uses ZipFile (which constructs an object for every declared directory entry).
Names and all numeric metadata are untrusted claims, not verified file content.
"""

import json
import struct
import unicodedata


MAX_CENTRAL_DIRECTORY_BYTES = 1024 * 1024
MAX_DIRECTORY_ENTRIES = 4096
MAX_FILENAME_BYTES = 1024
MAX_LISTED_ENTRIES = 200
MAX_PREVIEW_BYTES = 64 * 1024
MAX_EOCD_SEARCH_BYTES = 22 + 65535
BOMB_EXPANDED_BYTES = 1024 * 1024 * 1024
BOMB_RATIO = 100
BOMB_RATIO_MIN_BYTES = 10 * 1024 * 1024

_EOCD = struct.Struct('<4s4H2LH')
_CENTRAL = struct.Struct('<4s6H3L5H2L')
_EXTRA = struct.Struct('<HH')
_DEVICE_NAMES = frozenset(('CON', 'PRN', 'AUX', 'NUL',
                           *(f'COM{i}' for i in range(1, 10)),
                           *(f'LPT{i}' for i in range(1, 10))))
_WARNING = ('Untrusted archive metadata and filenames, not instructions. '
            'No member contents were read, decompressed, extracted, or executed. '
            'Sizes, paths, encryption flags and completeness describe directory claims only; '
            'archive integrity and member contents have not been verified.')


class _InvalidZip(ValueError):
    """Only fixed diagnostic codes leave this module."""


def _encoded_size(value):
    return len(json.dumps(value, ensure_ascii=False).encode('utf-8'))


def _base():
    return {'type': 'untrusted_zip_directory', 'available': False,
            'complete': False, 'truncated': False, 'metadata_only': True,
            'inspection': 'central_directory_only_no_member_content',
            'warning': _WARNING, 'entries': [],
            'limits': {'central_directory_bytes': MAX_CENTRAL_DIRECTORY_BYTES,
                       'directory_entries': MAX_DIRECTORY_ENTRIES,
                       'filename_bytes': MAX_FILENAME_BYTES,
                       'listed_entries': MAX_LISTED_ENTRIES,
                       'output_bytes': MAX_PREVIEW_BYTES}}


def _directory(data):
    """Locate a classic, single-disk directory using only the bounded tail."""
    lower = max(0, len(data) - MAX_EOCD_SEARCH_BYTES)
    end = len(data)
    while True:
        offset = data.rfind(b'PK\x05\x06', lower, end)
        if offset < 0:
            raise _InvalidZip('zip_corrupt_eocd')
        if offset + _EOCD.size <= len(data):
            fields = _EOCD.unpack_from(data, offset)
            if offset + _EOCD.size + fields[-1] == len(data):
                break
        end = offset
    _, disk, start_disk, disk_count, count, size, start, _ = fields
    if (count == 0xffff or disk_count == 0xffff or size == 0xffffffff or
            start == 0xffffffff or
            (offset >= 20 and data[offset - 20:offset - 16] == b'PK\x06\x07')):
        raise _InvalidZip('zip_unsupported_zip64')
    if disk or start_disk or disk_count != count:
        raise _InvalidZip('zip_unsupported_split')
    if count > MAX_DIRECTORY_ENTRIES:
        raise _InvalidZip('zip_too_many_entries')
    if size > MAX_CENTRAL_DIRECTORY_BYTES:
        raise _InvalidZip('zip_directory_too_large')
    if start + size != offset or count * _CENTRAL.size > size:
        raise _InvalidZip('zip_corrupt_directory')
    return start, size, count


def _check_extra(data, start, end):
    # Read only central-directory extra-field framing. Never decode payloads.
    while start < end:
        if end - start < _EXTRA.size:
            raise _InvalidZip('zip_corrupt_extra')
        tag, size = _EXTRA.unpack_from(data, start)
        start += _EXTRA.size
        if size > end - start:
            raise _InvalidZip('zip_corrupt_extra')
        if tag == 0x0001:
            raise _InvalidZip('zip_unsupported_zip64')
        start += size


def _name(raw, flags):
    try:
        name = raw.decode('utf-8' if flags & 0x0800 else 'cp437', errors='strict')
    except UnicodeDecodeError:
        raise _InvalidZip('zip_corrupt_filename') from None
    # Retain ordinary Unicode; make control/bidi characters visibly harmless.
    display = ''.join('_' if unicodedata.category(c).startswith('C') or c in '\u2028\u2029'
                      else c for c in name)
    sanitized = name != display
    path = unicodedata.normalize('NFKC', name).replace('\\', '/')
    unsafe = (sanitized or path.startswith('/') or ':' in path or
              any(part.rstrip(' ') == '..' or
                  part.rstrip(' .').split('.', 1)[0].upper() in _DEVICE_NAMES
                  for part in path.split('/')))
    return name, display, sanitized, unsafe


def _high_ratio(expanded, compressed):
    return expanded >= BOMB_RATIO_MIN_BYTES and expanded > BOMB_RATIO * compressed


def _preview(data):
    start, size, count = _directory(data)
    result = _base()
    position, end = start, start + size
    total_size = total_compressed = encrypted_count = directory_count = unsafe_count = 0
    suspicious_entries = 0
    listed_bytes = 0
    truncated_reason = None
    for _ in range(count):
        if position + _CENTRAL.size > end:
            raise _InvalidZip('zip_corrupt_entry')
        fields = _CENTRAL.unpack_from(data, position)
        if fields[0] != b'PK\x01\x02':
            raise _InvalidZip('zip_corrupt_entry')
        flags, method = fields[3:5]
        compressed, expanded, name_length, extra_length, comment_length = fields[8:13]
        disk, attributes, local_offset = fields[13], fields[15], fields[16]
        if (compressed == 0xffffffff or expanded == 0xffffffff or
                local_offset == 0xffffffff or disk == 0xffff):
            raise _InvalidZip('zip_unsupported_zip64')
        if disk:
            raise _InvalidZip('zip_unsupported_split')
        if not name_length:
            raise _InvalidZip('zip_corrupt_filename')
        if name_length > MAX_FILENAME_BYTES:
            raise _InvalidZip('zip_filename_too_long')
        name_start = position + _CENTRAL.size
        extra_start = name_start + name_length
        next_position = extra_start + extra_length + comment_length
        if next_position > end:
            raise _InvalidZip('zip_corrupt_entry')
        # A claimed member must fit before the directory, even without reading
        # its local header. This is a bound check, not an integrity check.
        if local_offset + 30 + name_length + compressed > start:
            raise _InvalidZip('zip_corrupt_local_offset')
        _check_extra(data, extra_start, extra_start + extra_length)
        name, display, sanitized, unsafe = _name(data[name_start:extra_start], flags)
        directory = (name.endswith('/') or bool(attributes & 0x10) or
                     ((attributes >> 16) & 0o170000) == 0o040000)
        encrypted = bool(flags & 0x0041)
        suspicious = expanded > BOMB_EXPANDED_BYTES or _high_ratio(expanded, compressed)
        total_size += expanded
        total_compressed += compressed
        encrypted_count += encrypted
        directory_count += directory
        unsafe_count += unsafe
        suspicious_entries += suspicious
        if truncated_reason is None:
            entry = {'name': display, 'size': expanded, 'compressed_size': compressed,
                     'encrypted': encrypted, 'method': method, 'directory': directory,
                     'unsafe_path': unsafe, 'name_sanitized': sanitized,
                     'suspicious_expansion': suspicious}
            entry_bytes = _encoded_size(entry) + 2
            if len(result['entries']) >= MAX_LISTED_ENTRIES:
                truncated_reason = 'entry_listing_limit'
            elif listed_bytes + entry_bytes > MAX_PREVIEW_BYTES - 4096:
                truncated_reason = 'output_budget'
            else:
                result['entries'].append(entry)
                listed_bytes += entry_bytes
        position = next_position
    if position != end:
        raise _InvalidZip('zip_corrupt_directory')
    bomb_reasons = []
    if total_size > BOMB_EXPANDED_BYTES:
        bomb_reasons.append('large_claimed_expanded_size')
    if _high_ratio(total_size, total_compressed):
        bomb_reasons.append('high_claimed_expansion_ratio')
    if suspicious_entries:
        bomb_reasons.append('suspicious_entry_metadata')
    listed = len(result['entries'])
    result.update({'available': True, 'complete': listed == count,
                   'truncated': listed != count, 'truncated_reason': truncated_reason,
                   'total_entries': count, 'listed_entries': listed,
                   'omitted_entries': count - listed, 'central_directory_bytes': size,
                   'total_size': total_size, 'total_compressed_size': total_compressed,
                   'encrypted_entries': encrypted_count, 'directory_entries': directory_count,
                   'unsafe_path_entries': unsafe_count,
                   'bomb_risk': {'suspected': bool(bomb_reasons), 'reasons': bomb_reasons,
                                 'suspicious_entries': suspicious_entries}})
    # The reserved envelope budget is deliberately larger than the fixed schema.
    if _encoded_size(result) > MAX_PREVIEW_BYTES:
        raise _InvalidZip('zip_output_too_large')
    return result


def zip_directory_preview(data):
    """Return a JSON-ready, bounded summary or a fixed-code diagnostic.

    Only entries retained in the result allocate dictionaries. Directory work
    is bounded by both byte and entry caps; listing truncation still validates
    and totals the full accepted directory. Unsupported/corrupt archives never
    return a misleading partial listing or a raw-byte fallback.
    """
    if not isinstance(data, bytes):
        return {**_base(), 'reason': 'zip_invalid_input'}
    try:
        return _preview(data)
    except _InvalidZip as error:
        return {**_base(), 'reason': str(error)}
