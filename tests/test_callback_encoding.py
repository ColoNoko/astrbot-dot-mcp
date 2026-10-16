"""Real loopback regressions for uncompressed callback challenge exchanges.

The receiver emulates gateways that gzip whenever aiohttp advertises gzip. No
external callback, proxy, DNS lookup, user account, or NAS is contacted.
"""
import gzip
import json
import sqlite3
import time
import unittest

from aiohttp import web
from multidict import CIMultiDict
from standardwebhooks import Webhook

from test_events import Events, EventError, SECRET, signing_module


class RecordingLogger:
    def __init__(self):
        self.records = []

    def info(self, message):
        self.records.append(message)

    def warning(self, message):
        self.records.append(message)


class CallbackEncodingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.response_status = 200
        self.response_headers = CIMultiDict()
        self.response_mode = 'echo'
        self.compress_if_accepted = True
        self.response_clock_advance = 0
        self.response_body = None
        self.logger = RecordingLogger()
        app = web.Application()
        app.router.add_post('/events', self.receive)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, '127.0.0.1', 0)
        await self.site.start()
        port = self.site._server.sockets[0].getsockname()[1]
        self.origin = f'http://127.0.0.1:{port}'
        self.db = sqlite3.connect(':memory:')
        self.events = Events(self.db, 'owner', test_callback_origin=self.origin,
                             logger=self.logger)
        self.now = time.time()
        self.events.clock = lambda: self.now

    async def asyncTearDown(self):
        await self.events.transport.close()
        self.db.close()
        await self.runner.cleanup()

    async def receive(self, request):
        body = await request.read()
        headers = CIMultiDict(request.headers)
        self.requests.append((body, headers))
        value = json.loads(body)
        modes = {
            'echo': {'challenge': value.get('challenge')},
            'wrong': {'challenge': 'WRONG_CHALLENGE_PRIVATE_SENTINEL'},
            'missing': {'PRIVATE_RESPONSE_KEY_SENTINEL': 'PRIVATE_RESPONSE_VALUE_SENTINEL'},
            'number': {'challenge': 123},
            'list': [{'challenge': value.get('challenge')}],
            'not_json': None,
        }
        if self.response_body is not None:
            payload = self.response_body
        elif self.response_mode == 'not_json':
            payload = b'PRIVATE_RESPONSE_NON_JSON_SENTINEL'
        else:
            payload = json.dumps(modes[self.response_mode]).encode()
        response_headers = CIMultiDict(self.response_headers)
        if self.compress_if_accepted and 'gzip' in headers.get('Accept-Encoding', ''):
            payload = gzip.compress(payload)
            response_headers['Content-Encoding'] = 'gzip'
        if response_headers.get('Content-Encoding') == 'gzip' and not payload.startswith(b'\x1f\x8b'):
            payload = gzip.compress(payload)
        self.now += self.response_clock_advance
        return web.Response(status=self.response_status, body=payload,
                            headers=response_headers, content_type='application/json')

    def params(self):
        return {'name': 'qq.message', 'arguments': {}, 'delivery': {
            'mode': 'webhook', 'url': self.origin + '/events', 'secret': SECRET}}

    def verification(self):
        return self.events.status()['callback_transport']['verification']

    def assert_safe_diagnostics(self):
        exported = json.dumps(self.events.status()) + '\n'.join(self.logger.records)
        for sentinel in (SECRET, self.origin, 'PRIVATE_RESPONSE', 'PRIVATE_ENCODING',
                         'PRIVATE_HEADER', 'WRONG_CHALLENGE_PRIVATE_SENTINEL'):
            self.assertNotIn(sentinel, exported)
        for body, headers in self.requests:
            value = json.loads(body)
            if isinstance(value.get('challenge'), str):
                self.assertNotIn(value['challenge'], exported)
            for key in ('webhook-signature', 'webhook-id', 'X-MCP-Subscription-Id'):
                if headers.get(key):
                    self.assertNotIn(headers[key], exported)

    async def test_conditional_gzip_receiver_now_accepts_exact_challenge(self):
        result = await self.events.subscribe(self.params())
        body, headers = self.requests[0]
        self.assertEqual(headers.getall('Accept-Encoding'), ['identity'])
        verified = Webhook(SECRET).verify(body, headers)
        self.assertEqual(verified['type'], 'verification')
        self.assertEqual(headers['X-MCP-Subscription-Id'], result['id'])
        self.assertEqual(self.events.status()['active_subscriptions'], 1)
        self.assertEqual(self.events.status()['last_subscribe']['http_status'], 200)
        self.assertEqual(self.events.transport.last_transport['attempts'][0]['response_encoding'], 'identity')
        self.assertEqual(self.verification(), {
            'json_valid': True, 'object_response': True, 'challenge_present': True,
            'challenge_string': True, 'challenge_match': True,
            'within_deadline': True, 'status_2xx': True})
        self.assert_safe_diagnostics()

    async def test_identity_override_preserves_signed_bytes_headers_and_caller(self):
        body = b'{ "challenge": "exact-byte-sentinel", "unicode": "\\u00e9" }\n'
        headers = CIMultiDict(signing_module.signed_headers(
            'verify_exact_bytes', body, SECRET, self.now, 'sub_exact_bytes'))
        headers.add('accept-encoding', 'gzip')
        headers.add('Accept-Encoding', 'deflate')
        original_headers = tuple(headers.items())
        status, response = await self.events.transport.post(self.origin + '/events', body, headers)
        received_body, received_headers = self.requests[0]
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(response), {'challenge': 'exact-byte-sentinel'})
        self.assertEqual(received_body, body)
        self.assertEqual(tuple(headers.items()), original_headers)
        self.assertEqual(received_headers.getall('Accept-Encoding'), ['identity'])
        for key, value in headers.items():
            if key.lower() != 'accept-encoding':
                self.assertEqual(received_headers[key], value)
        self.assertEqual(Webhook(SECRET).verify(received_body, received_headers), json.loads(body))

    async def test_wrong_challenge_never_activates_and_reports_only_booleans(self):
        self.response_mode = 'wrong'
        with self.assertRaisesRegex(EventError, '^challenge_failed$'):
            await self.events.subscribe(self.params())
        self.assertEqual(self.events.status()['active_subscriptions'], 0)
        self.assertEqual(self.verification(), {
            'json_valid': True, 'object_response': True, 'challenge_present': True,
            'challenge_string': True, 'challenge_match': False,
            'within_deadline': True, 'status_2xx': True})
        self.assert_safe_diagnostics()

    async def test_invalid_json_shape_and_type_have_fixed_boolean_diagnostics(self):
        for mode, expected in (
            ('not_json', (False, False, False, False)),
            ('list', (True, False, False, False)),
            ('missing', (True, True, False, False)),
            ('number', (True, True, True, False)),
        ):
            with self.subTest(mode=mode):
                self.response_mode = mode
                self.events._last_subscription_log_at = None
                with self.assertRaisesRegex(EventError, '^challenge_failed$'):
                    await self.events.subscribe(self.params())
                detail = self.verification()
                for key, value in zip(('json_valid', 'object_response', 'challenge_present', 'challenge_string'), expected):
                    self.assertIs(detail[key], value)
                self.assertIs(detail['challenge_match'], False)
                self.assertTrue(all(type(value) is bool for value in detail.values()))
                self.assertEqual(self.events.status()['active_subscriptions'], 0)
                self.assert_safe_diagnostics()

    async def test_correct_echo_requires_success_http_status(self):
        self.response_status = 503
        with self.assertRaisesRegex(EventError, '^challenge_failed$'):
            await self.events.subscribe(self.params())
        self.assertIs(self.verification()['challenge_match'], True)
        self.assertIs(self.verification()['status_2xx'], False)
        self.assertEqual(self.events.status()['active_subscriptions'], 0)
        self.assertEqual(self.events.status()['last_subscribe']['http_status'], 503)

    async def test_correct_echo_requires_original_verification_deadline(self):
        self.response_clock_advance = 31
        with self.assertRaisesRegex(EventError, '^challenge_failed$'):
            await self.events.subscribe(self.params())
        self.assertIs(self.verification()['challenge_match'], True)
        self.assertIs(self.verification()['within_deadline'], False)
        self.assertEqual(self.events.status()['active_subscriptions'], 0)

    async def test_server_ignoring_identity_is_rejected_without_decoding(self):
        self.response_headers['Content-Encoding'] = 'gzip'
        with self.assertRaisesRegex(EventError, '^callback_encoding_unsupported$'):
            await self.events.subscribe(self.params())
        status = self.events.status()
        self.assertEqual(self.requests[0][1].getall('Accept-Encoding'), ['identity'])
        self.assertEqual(status['active_subscriptions'], 0)
        self.assertEqual(status['last_subscribe']['error'], 'callback_encoding_unsupported')
        self.assertEqual(status['last_subscribe']['http_status'], 200)
        detail = status['callback_transport']['attempts'][0]
        self.assertEqual(detail['response_encoding'], 'gzip')
        self.assertEqual(detail['response_status'], 200)
        self.assertEqual(detail['error'], 'callback_encoding_unsupported')
        self.assert_safe_diagnostics()

    async def test_encoded_expansion_is_not_decompressed(self):
        self.response_headers['Content-Encoding'] = 'gzip'
        self.response_body = b'X' * (1024 * 1024)
        with self.assertRaisesRegex(EventError, '^callback_encoding_unsupported$'):
            await self.events.subscribe(self.params())
        self.assertEqual(self.events.status()['active_subscriptions'], 0)
        self.assert_safe_diagnostics()

    async def test_nonidentity_and_private_encoding_values_are_fixed_enums(self):
        for encoding, expected in (('deflate', 'deflate'), ('br', 'br'), ('zstd', 'zstd'),
                                   ('PRIVATE_ENCODING_HEADER_SENTINEL', 'other')):
            with self.subTest(encoding=encoding):
                self.response_headers['Content-Encoding'] = encoding
                self.response_headers['X-Private-Header'] = 'PRIVATE_HEADER_VALUE_SENTINEL'
                self.events._last_subscription_log_at = None
                with self.assertRaisesRegex(EventError, '^callback_encoding_unsupported$'):
                    await self.events.subscribe(self.params())
                detail = self.events.status()['callback_transport']['attempts'][0]
                self.assertEqual(detail['response_encoding'], expected)
                self.assertEqual(self.events.status()['active_subscriptions'], 0)
                self.assert_safe_diagnostics()

    async def test_duplicate_or_stacked_encodings_cannot_hide_behind_identity(self):
        for values in (('identity', 'gzip'), ('gzip', 'identity'), ('identity', 'identity'),
                       ('identity, gzip',), ('gzip, identity',)):
            with self.subTest(values=values):
                self.response_headers = CIMultiDict(('Content-Encoding', value) for value in values)
                with self.assertRaisesRegex(EventError, '^callback_encoding_unsupported$'):
                    await self.events.subscribe(self.params())
                self.assertEqual(self.events.status()['active_subscriptions'], 0)
                self.assertEqual(self.events.status()['callback_transport']['attempts'][0]['response_encoding'], 'other')

    async def test_explicit_identity_response_is_accepted(self):
        self.response_headers['Content-Encoding'] = 'identity'
        await self.events.subscribe(self.params())
        self.assertEqual(self.events.status()['active_subscriptions'], 1)
        self.assertEqual(self.events.status()['callback_transport']['attempts'][0]['response_encoding'], 'identity')


if __name__ == '__main__':
    unittest.main()
