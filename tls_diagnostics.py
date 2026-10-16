"""Fixed, non-sensitive TLS failure diagnostics. Never stringify exceptions."""
import ssl

_REASONS = {
    'CERTIFICATE_VERIFY_FAILED': 'certificate_verify_failed',
    'WRONG_VERSION_NUMBER': 'wrong_version',
    'UNSUPPORTED_PROTOCOL': 'protocol_version',
    'TLSV1_ALERT_PROTOCOL_VERSION': 'protocol_version',
    'SSLV3_ALERT_HANDSHAKE_FAILURE': 'handshake_alert',
    'TLSV1_ALERT_INTERNAL_ERROR': 'handshake_alert',
    'TLSV1_ALERT_ACCESS_DENIED': 'handshake_alert',
    'TLSV1_ALERT_UNKNOWN_CA': 'certificate_alert',
    'SSLV3_ALERT_BAD_CERTIFICATE': 'certificate_alert',
    'UNEXPECTED_EOF_WHILE_READING': 'unexpected_eof',
}


def record_tls(detail, error):
    """Inspect a bounded exception graph, exposing only fixed enums and a number."""
    pending, seen, found = [error], set(), None
    while pending and len(seen) < 16:
        current = pending.pop(0)
        if not isinstance(current, BaseException) or id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, ssl.SSLError):
            found = current
            break
        for name in ('__cause__', '__context__', 'certificate_error', 'os_error'):
            child = getattr(current, name, None)
            if isinstance(child, BaseException):
                pending.append(child)
    detail.update(tls_category=None, tls_verify_code=None, tls_reason=None)
    if found is None:
        return
    if isinstance(found, ssl.SSLCertVerificationError):
        category = 'certificate_verification'
        code = getattr(found, 'verify_code', None)
        detail['tls_verify_code'] = code if type(code) is int and 0 <= code <= 65535 else None
    elif isinstance(found, ssl.SSLEOFError):
        category = 'unexpected_eof'
    elif isinstance(found, ssl.SSLZeroReturnError):
        category = 'zero_return'
    else:
        category = 'ssl_error'
    detail['tls_category'] = category
    reason = getattr(found, 'reason', None)
    detail['tls_reason'] = _REASONS.get(reason, 'other') if type(reason) is str else 'other'
