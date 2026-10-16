"""Pre-POST revocation at DNS and actual connection completion; loopback only."""
import asyncio
import socket
import unittest
from unittest.mock import patch
import aiohttp
from aiohttp import web
from test_events import Events, EventError
import sqlite3

class CallbackReminderGuardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests=[]
        async def receive(request):
            self.requests.append(await request.read())
            return web.Response(text='{}')
        app=web.Application();app.router.add_post('/event',receive)
        self.runner=web.AppRunner(app);await self.runner.setup()
        site=web.TCPSite(self.runner,'127.0.0.1',0);await site.start()
        self.origin=f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}'
        self.db=sqlite3.connect(':memory:');self.events=Events(self.db,'owner',['callbacks.example.com'],test_callback_origin=self.origin)
        self.transport=self.events.transport;self.allowed=True
    async def asyncTearDown(self):
        await self.transport.close();self.db.close();await self.runner.cleanup()
    async def test_revoked_while_dns_awaits_never_posts(self):
        entered=asyncio.Event();release=asyncio.Event()
        async def addresses(host):
            entered.set();await release.wait()
            return [{'hostname':host,'host':'127.0.0.1','port':443,'family':socket.AF_INET,'proto':0,'flags':0}]
        with patch.object(self.transport,'_addresses',addresses):
            task=asyncio.create_task(self.transport.post('https://callbacks.example.com/event',b'{}',{},preflight=lambda:self.allowed))
            # Allow only this fixed synthetic host without making a DNS request.
            await asyncio.wait_for(entered.wait(),1)
            self.allowed=False;release.set()
            with self.assertRaises(EventError) as caught:await task
            self.assertEqual(caught.exception.code,'callback_preflight_rejected')
        self.assertEqual(self.requests,[])
    async def test_revoked_after_connection_before_request_sends_no_body(self):
        entered=asyncio.Event();release=asyncio.Event()
        original=aiohttp.TCPConnector._create_connection
        protocols=[]
        async def delayed(connector,*args,**kwargs):
            connection=await original(connector,*args,**kwargs)
            protocols.append(connection)
            entered.set();await release.wait()
            return connection
        with patch.object(aiohttp.TCPConnector,'_create_connection',delayed):
            task=asyncio.create_task(self.transport.post(self.origin+'/event',b'{}',{},preflight=lambda:self.allowed))
            await asyncio.wait_for(entered.wait(),1)
            self.allowed=False;release.set()
            with self.assertRaises(EventError) as caught:await task
            self.assertEqual(caught.exception.code,'callback_preflight_rejected')
        await asyncio.sleep(0)
        self.assertEqual(self.requests,[])
        self.assertEqual(len(protocols),1)
        self.assertFalse(protocols[0].is_connected())
    async def test_guard_is_task_local_and_normal_post_still_works(self):
        with self.assertRaises(EventError):
            await self.transport.post(self.origin+'/event',b'{}',{},preflight=lambda:False)
        self.assertEqual(self.transport.last_transport['attempts'],[])
        status,_=await self.transport.post(self.origin+'/event',b'{}',{})
        self.assertEqual(status,200);self.assertEqual(self.requests,[b'{}'])
