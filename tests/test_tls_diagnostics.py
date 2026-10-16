import importlib
import ssl
import unittest
from pathlib import Path
import sys
import types

ROOT = Path(__file__).resolve().parents[1]
if "astrbot_dot_plugin" not in sys.modules:
    package = types.ModuleType("astrbot_dot_plugin")
    package.__path__ = [str(ROOT)]
    sys.modules[package.__name__] = package
record_tls=importlib.import_module('astrbot_dot_plugin.tls_diagnostics').record_tls

class TLSDiagnosticsTests(unittest.TestCase):
    def test_wrapped_certificate_number_without_private_message(self):
        cert=ssl.SSLCertVerificationError(1,'SECRET_SENTINEL host/credential')
        cert.verify_code=62; cert.verify_message='SECRET_SENTINEL'; cert.reason='CERTIFICATE_VERIFY_FAILED'
        wrapper=RuntimeError('SECRET_SENTINEL');wrapper.__cause__=cert
        result={};record_tls(result,wrapper)
        self.assertEqual(result,{'tls_category':'certificate_verification','tls_verify_code':62,'tls_reason':'certificate_verify_failed'})
        self.assertNotIn('SECRET_SENTINEL',str(result))
    def test_unknown_reason_and_bad_codes_are_not_echoed(self):
        for code in (True,-1,65536,'SECRET_SENTINEL',None):
            cert=ssl.SSLCertVerificationError(1,'SECRET_SENTINEL')
            cert.verify_code=code;cert.reason='SECRET_SENTINEL'
            result={};record_tls(result,cert)
            self.assertIsNone(result['tls_verify_code']);self.assertEqual(result['tls_reason'],'other')
            self.assertNotIn('SECRET_SENTINEL',str(result))
    def test_eof_zero_and_protocol_fixed_categories(self):
        for cls,reason,category,mapped in [(ssl.SSLEOFError,'UNEXPECTED_EOF_WHILE_READING','unexpected_eof','unexpected_eof'),(ssl.SSLZeroReturnError,None,'zero_return','other'),(ssl.SSLError,'WRONG_VERSION_NUMBER','ssl_error','wrong_version')]:
            error=cls(1,'SECRET_SENTINEL');error.reason=reason
            result={};record_tls(result,error)
            self.assertEqual(result['tls_category'],category);self.assertEqual(result['tls_reason'],mapped)
    def test_cycle_and_non_tls_clear_stale_details(self):
        error=RuntimeError('SECRET_SENTINEL');error.__cause__=error
        result={'tls_category':'old'};record_tls(result,error)
        self.assertEqual(result,dict(tls_category=None,tls_verify_code=None,tls_reason=None))
    def test_certificate_attribute_adapter_chain(self):
        cert=ssl.SSLCertVerificationError(1,'SECRET_SENTINEL');cert.verify_code=20
        wrapper=RuntimeError();wrapper.certificate_error=cert
        result={};record_tls(result,wrapper)
        self.assertEqual(result['tls_verify_code'],20)
