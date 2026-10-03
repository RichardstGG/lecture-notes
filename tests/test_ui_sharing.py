"""Sharing contracts using synthetic session files, ASGI and loopback HTTP clients."""
import asyncio
import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from tests import _pathfix  # noqa: F401
from ui.backend.app import create_app
from ui.backend.session_store import SessionStore
from ui.backend.settings import BackendSettings
from ui.backend.share_app import COOKIE, create_share_app
from ui.backend.share_runtime import ShareRuntime, validate_address
from ui.backend.sharing import MAX_VISITORS, ShareError, ShareRoom


class SharingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root / 'course' / 'first'
        self.path.mkdir(parents=True)
        (self.path / 'transcript.md').write_text('第一場逐字稿 <script>evil()</script>', encoding='utf-8')
        (self.path / 'notes.md').write_text('初步筆記', encoding='utf-8')
        self.set_phase('recording')
        other = self.root / 'second'
        other.mkdir()
        (other / 'notes.md').write_text('PRIVATE OTHER SESSION')
        self.now = 100.0
        self.store = SessionStore(self.root)
        self.room = ShareRoom(self.store, 'course/first', clock=lambda: self.now)
        self.app = create_share_app(self.room, '192.168.1.2:8766')
        self.clients = []

    def set_phase(self, phase):
        (self.path / 'status.json').write_text(json.dumps({
            'course': '測試課', 'phase': phase, 'session': '/private/path',
        }), encoding='utf-8')

    async def asyncTearDown(self):
        for client in self.clients:
            await client.aclose()
        self.tmp.cleanup()

    def client(self):
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                   base_url='http://192.168.1.2:8766')
        self.clients.append(client)
        return client

    async def join(self, client, nickname='小明', invitation=None):
        return await client.post('/share/v1/join', json={
            'invitation': invitation if invitation is not None else self.room.invitation,
            'nickname': nickname,
        })

    async def test_guest_routes_expose_only_fixed_text_and_downloads(self):
        client = self.client()
        self.assertEqual((await client.get('/share/v1/snapshot')).status_code, 401)
        response = await self.join(client)
        self.assertEqual(response.status_code, 200)
        self.assertIn('HttpOnly', response.headers['set-cookie'])
        self.assertIn('SameSite=strict', response.headers['set-cookie'])
        response = await client.get('/share/v1/snapshot')
        self.assertEqual(response.json()['course'], '測試課')
        self.assertNotIn('/private', response.text)
        self.assertNotIn('course/first', response.text)
        self.assertNotIn('PRIVATE OTHER SESSION', response.text)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        for target in ('transcript', 'notes'):
            response = await client.get(f'/share/v1/download/{target}')
            self.assertEqual(response.status_code, 200)
            self.assertIn('目前版本', response.text)
            self.assertIn('擷取時間', response.text)
            self.assertIn('current-version', response.headers['content-disposition'])
        for path in ('/api/v1/status', '/api/v1/sessions', '/api/v1/sharing',
                     '/api/docs', '/openapi.json', '/assets/index.js',
                     '/share/v1/download/status.json', '/share/v1/download/recording',
                     '/share/v1/sessions/second', '/config/local.toml'):
            self.assertEqual((await client.get(path)).status_code, 404, path)
        for path in ('/api/v1/runs', '/api/v1/runs/stop', '/api/v1/sharing/close',
                     '/share/v1/comments', '/share/v1/edit'):
            self.assertEqual((await client.post(path, json={})).status_code, 404, path)

    async def test_twenty_distinct_visitors_and_expiring_seats(self):
        clients = [self.client() for _ in range(21)]
        responses = await asyncio.gather(*(self.join(c, f'同學{i}') for i, c in enumerate(clients)))
        self.assertEqual(sum(r.status_code == 200 for r in responses), 20)
        self.assertEqual(responses[-1].status_code, 409)
        self.assertEqual(sum(v['online'] for v in self.room.roster()), 20)
        # Same browser refresh/join must not consume another seat.
        self.assertEqual((await self.join(clients[0])).status_code, 200)
        self.assertEqual(len(self.room.roster()), 20)
        self.now += 31
        self.assertFalse(any(v['online'] for v in self.room.roster()))
        self.assertEqual((await self.join(clients[-1])).status_code, 200)
        # Existing credentials reconnect under the same capacity constraint.
        for client in clients[1:20]:
            self.assertEqual((await client.get('/share/v1/snapshot')).status_code, 200)
        self.assertEqual((await clients[0].get('/share/v1/snapshot')).status_code, 409)
        self.assertEqual((await clients[1].post('/share/v1/leave', json={})).status_code, 200)
        self.assertEqual((await clients[0].get('/share/v1/snapshot')).status_code, 200)
        self.assertEqual(sum(v['online'] for v in self.room.roster()), 20)

    async def test_revocation_and_new_generation(self):
        client = self.client()
        old_invitation = self.room.invitation
        await self.join(client)
        token = client.cookies.get(COOKIE)
        self.room.close()
        for path in ('/', '/share/v1/snapshot', '/share/v1/download/notes'):
            self.assertEqual((await client.get(path)).status_code, 410)
        room = ShareRoom(self.store, 'course/first')
        self.assertNotEqual(old_invitation, room.invitation)
        with self.assertRaises(ShareError) as invalid:
            room.authorize(token)
        self.assertEqual(invalid.exception.status_code, 401)
        with self.assertRaises(ShareError):
            room.join(old_invitation, '小明')

    async def test_updates_continue_after_stop_and_do_not_follow_another_session(self):
        client = self.client()
        await self.join(client)
        before = (await client.get('/share/v1/snapshot')).json()
        (self.path / 'transcript.md').write_text('追加逐字稿', encoding='utf-8')
        self.set_phase('summarizing')
        self.now += 2
        after = (await client.get('/share/v1/snapshot')).json()
        self.assertEqual(after['phase'], 'summarizing')
        self.assertNotEqual(before['version'], after['version'])
        self.assertEqual(after['transcript']['content'], '追加逐字稿')
        self.set_phase('done')
        (self.path / 'notes.md').write_text('收尾筆記', encoding='utf-8')
        self.now += 2
        self.assertIn('收尾筆記', (await client.get('/share/v1/download/notes')).text)
        self.assertTrue(self.room.active)
        self.assertEqual(self.room.session_id, 'course/first')

    async def test_nickname_invitation_and_browser_origin_validation(self):
        client = self.client()
        for name in ('', '   ', 'a' * 41, 'bad\nname'):
            self.assertIn((await self.join(client, name)).status_code, (400, 422))
        for invitation in ('wrong', '壞連結'):
            self.assertEqual((await self.join(client, invitation=invitation)).status_code, 403)
        self.assertEqual(len(self.room.roster()), 0)
        response = await client.post('/share/v1/join', json={
            'invitation': self.room.invitation, 'nickname': '同學',
        }, headers={'Origin': 'https://attacker.example'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual((await client.get('/', headers={'Host': 'evil.example'})).status_code, 403)
        self.assertEqual((await client.post('/share/v1/leave', content='{}')).status_code, 415)
        self.assertNotIn('access-control-allow-origin', (await client.get('/')).headers)

    async def test_errors_are_redacted_and_symlinks_not_shared(self):
        client = self.client()
        await self.join(client)
        (self.path / 'notes.md').unlink()
        (self.path / 'notes.md').symlink_to(self.root / 'second' / 'notes.md')
        self.now += 2
        self.assertEqual((await client.get('/share/v1/snapshot')).json()['notes']['content'], '')
        with patch.object(self.store, 'get', side_effect=OSError('/private/secret')):
            self.now += 2
            response = await client.get('/share/v1/snapshot')
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('/private', response.text)

    async def test_conditional_polling_renews_lease_without_resending_content(self):
        client = self.client()
        await self.join(client)
        response = await client.get('/share/v1/snapshot')
        etag = response.headers['etag']
        self.now += 29
        response = await client.get('/share/v1/snapshot', headers={'If-None-Match': etag})
        self.assertEqual(response.status_code, 304)
        self.assertFalse(response.content)
        self.now += 2
        self.assertTrue(self.room.roster()[0]['online'])
        self.set_phase('done')
        response = await client.get('/share/v1/snapshot', headers={'If-None-Match': etag})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['phase'], 'done')

    async def test_shared_cache_and_bounded_offline_history(self):
        with patch.object(self.store, 'get', wraps=self.store.get) as read:
            self.now += 2
            for _ in range(20):
                self.room.snapshot()
            self.assertEqual(read.call_count, 1)
        oldest = None
        for i in range(MAX_VISITORS + 1):
            token = self.room.join(self.room.invitation, f'Guest {i}')
            oldest = oldest or token
            self.room.leave(token)
            self.now += 1
        self.assertEqual(len(self.room.visitors), MAX_VISITORS)
        with self.assertRaises(ShareError):
            self.room.authorize(oldest)

    async def test_local_control_contract_and_lifespan_cleanup(self):
        runtime = ShareRuntime(self.store)
        app = create_app(settings=BackendSettings(self.root, output_root=self.root), sharing=runtime)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url='http://127.0.0.1:8765') as client:
            self.assertFalse((await client.get('/api/v1/sharing')).json()['active'])
            for host in ('0.0.0.0', '127.0.0.1', '8.8.8.8', 'localhost'):
                response = await client.post('/api/v1/sharing/open', json={
                    'session_id': 'course/first', 'host': host, 'port': 8766,
                })
                self.assertEqual(response.status_code, 400, host)
            response = await client.post('/api/v1/sharing/close', json={},
                                          headers={'Origin': 'https://evil.example'})
            self.assertEqual(response.status_code, 403)
            response = await client.get('/api/v1/sharing', headers={'Host': 'evil.example'})
            self.assertEqual(response.status_code, 403)
            self.assertEqual((await client.post('/api/v1/sharing/close', json={})).status_code, 200)
        with patch.object(runtime, 'close', wraps=runtime.close) as close:
            async with app.router.lifespan_context(app):
                pass
            close.assert_awaited_once()

    async def test_real_loopback_listener_capacity_close_and_reopen(self):
        # Real HTTP sockets on one machine. Only the private-IP policy is bypassed.
        probe = socket.socket()
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
        probe.close()
        runtime = ShareRuntime(self.store)
        base = f'http://127.0.0.1:{port}'
        clients = [httpx.AsyncClient(base_url=base, trust_env=False) for _ in range(21)]
        self.clients.extend(clients)
        try:
            with patch('ui.backend.share_runtime.validate_address'):
                first = await runtime.open('course/first', '127.0.0.1', port)
                invitation = first['url'].split('#')[1]
                responses = await asyncio.gather(*(c.post('/share/v1/join', json={
                    'invitation': invitation, 'nickname': f'Guest {i}',
                }) for i, c in enumerate(clients)))
                self.assertEqual(sorted(r.status_code for r in responses), [200] * 20 + [409])
                admitted = [c for c, r in zip(clients, responses) if r.status_code == 200]
                snapshots = await asyncio.gather(*(c.get('/share/v1/snapshot') for c in admitted))
                self.assertTrue(all(r.status_code == 200 for r in snapshots))
                with self.assertRaises(ShareError):
                    await runtime.open('second', '127.0.0.1', port)
                self.assertEqual(runtime.status()['session_id'], 'course/first')
                await runtime.close()
                self.assertFalse(runtime.status()['active'])
                with self.assertRaises(httpx.ConnectError):
                    await clients[0].get('/')
                second = await runtime.open('course/first', '127.0.0.1', port)
                self.assertNotEqual(first['url'], second['url'])
                self.assertEqual((await clients[0].get('/share/v1/snapshot')).status_code, 401)
                self.assertEqual((await clients[0].post('/share/v1/join', json={
                    'invitation': invitation, 'nickname': 'Old',
                })).status_code, 403)
        finally:
            await runtime.close()

    async def test_busy_port_does_not_publish_link(self):
        with socket.socket() as occupied:
            occupied.bind(('127.0.0.1', 0))
            occupied.listen()
            runtime = ShareRuntime(self.store)
            with patch('ui.backend.share_runtime.validate_address'), self.assertRaises(ShareError) as error:
                await runtime.open('course/first', '127.0.0.1', occupied.getsockname()[1])
            self.assertEqual(error.exception.code, 'share_bind_failed')
            self.assertFalse(runtime.status()['active'])
            await runtime.close()

    def test_private_ipv4_only(self):
        for host in ('10.1.2.3', '172.16.1.2', '172.31.255.254', '192.168.1.2'):
            validate_address(host, 8766)
        for host in ('0.0.0.0', '127.0.0.1', '::1', '169.254.1.2', '172.32.0.1', '8.8.8.8', 'example.com'):
            with self.assertRaises(ShareError):
                validate_address(host, 8766)
        for port in (0, 1023, 65536):
            with self.assertRaises(ShareError):
                validate_address('192.168.1.2', port)


if __name__ == '__main__':
    unittest.main()
