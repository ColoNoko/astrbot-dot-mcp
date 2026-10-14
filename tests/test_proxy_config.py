import unittest
from proxy_config import ProxySettings, validate_proxy, proxy_from_config, ProxyError

class ProxyConfigTests(unittest.TestCase):
    def test_default_off(self):
        self.assertFalse(proxy_from_config({}).enabled)
    def test_valid_intentional_private_proxy(self):
        for url in ('http://127.0.0.1:7890','http://192.168.1.20:8080','http://proxy.local:3128','http://[::1]:7890'):
            self.assertEqual(validate_proxy(ProxySettings(True,url)).url,url)
    def test_unsupported_or_credential_url_rejected(self):
        for url in ('HTTP://host:1','socks5://host:1','https://host:1','http://host','http://user:pass@host:1',
                    'http://host:1/a','http://host:1/?secret=x','http://host:1/#x',
                    'http://0.0.0.0:1','http://224.0.0.1:1','http://[::]:1','http://a..b:1','http://host:0'):
            with self.subTest(url=url),self.assertRaisesRegex(ProxyError,'^invalid_proxy_settings$'):
                validate_proxy(ProxySettings(True,url))
    def test_credentials_never_repr_and_encoded_for_child(self):
        s=validate_proxy(ProxySettings(True,'http://proxy.local:1','synthetic@user','synthetic:#pass'))
        self.assertNotIn('synthetic',repr(s)); self.assertIn('synthetic%40user:synthetic%3A%23pass@',s.authenticated_url())
    def test_auth_shape(self):
        for username,password in (('a:b','p'),('','p'),('a','x\nsecret'),(True,'p')):
            with self.assertRaises(ProxyError): validate_proxy(ProxySettings(False,'',username,password))
