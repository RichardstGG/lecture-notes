"""Manage an on-demand LAN listener in the same event loop as the local UI."""
import asyncio
from contextlib import contextmanager
import ipaddress
import socket

import uvicorn

from .share_app import create_share_app
from .sharing import ShareError, ShareRoom
from .share_network import sharing_network, usable_ipv4

LAN_NETWORKS = tuple(ipaddress.ip_network(value) for value in
                     ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))


def validate_address(host, port):
    try:
        address = ipaddress.IPv4Address(host)
    except ipaddress.AddressValueError:
        raise ShareError('invalid_share_host', '請輸入本機的內網 IPv4 位址。') from None
    if host != "0.0.0.0" and not any(address in network for network in LAN_NETWORKS):
        raise ShareError('invalid_share_host', '分享可綁定 0.0.0.0 或 10.x、172.16–31.x、192.168.x 內網位址。')
    if not 1024 <= port <= 65535:
        raise ShareError('invalid_share_port', '分享埠必須介於 1024 與 65535。')


class ShareServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        # The control server remains the sole owner of process signals.
        yield


class ShareRuntime:
    def __init__(self, sessions):
        self.sessions = sessions
        self.room = None
        self.server = None
        self.task = None
        self.sock = None
        self.url = None
        self.bind_host = self.advertise_host = None
        self.lock = asyncio.Lock()

    def status(self):
        if not self.room or not self.room.active:
            return {'api_version': 1, 'active': False, 'participants': []}
        if self.task and self.task.done():
            self.room.close()
            return {'api_version': 1, 'active': False, 'participants': [],
                    'error': '分享服務已停止，請重新開啟。'}
        return {'api_version': 1, 'active': True, 'session_id': self.room.session_id,
                'work_type': self.room.work_type,
                'url': self.url, 'bind_host': self.bind_host, 'advertise_host': self.advertise_host,
                'participants': self.room.roster(), 'max_online': 20}

    async def open(self, session_id, host, port, advertise_host=None):
        async with self.lock:
            if self.status()['active']:
                raise ShareError('share_already_open', '請先關閉目前分享，再開啟另一場。', 409)
            await self._close()
            validate_address(host, port)
            if host == '0.0.0.0':
                if not advertise_host:
                    advertise_host = (await sharing_network()).get('advertise_host')
                if not usable_ipv4(advertise_host):
                    raise ShareError('invalid_advertise_host', '請輸入連結使用的本機 IPv4 位址。')
            else:
                if advertise_host and advertise_host != host:
                    raise ShareError('invalid_advertise_host', '指定介面時，連結 IP 必須與監聽 IP 相同。')
                advertise_host = host
            room = ShareRoom(self.sessions, session_id)
            sock = None
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                # No SO_REUSEPORT: a second sharing instance must fail closed.
                if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                else:
                    # Allow immediate reopen after accepted connections enter TIME_WAIT.
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind((host, port))
                sock.listen(64)
                sock.setblocking(False)
            except OSError:
                if sock is not None:
                    sock.close()
                room.close()
                raise ShareError('share_bind_failed', '無法啟動分享：請確認本機內網 IP 與埠是否可用。', 409) from None
            authority = f'{advertise_host}:{port}'
            server = ShareServer(uvicorn.Config(
                create_share_app(room, authority, all_interfaces=host == "0.0.0.0"), host=host, port=port,
                access_log=False, proxy_headers=False, timeout_graceful_shutdown=1,
                limit_concurrency=64,
            ))
            self.bind_host, self.advertise_host = host, advertise_host
            self.room, self.sock, self.server = room, sock, server
            self.task = asyncio.create_task(server.serve(sockets=[sock]))
            try:
                async with asyncio.timeout(5):
                    while not server.started:
                        if self.task.done():
                            await self.task
                            raise RuntimeError('share server stopped during startup')
                        await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                await self._close()
                raise
            except Exception:
                await self._close()
                raise ShareError('share_start_failed', '分享服務啟動失敗。', 503) from None
            self.url = f'http://{authority}/#{room.invitation}'
            return self.status()

    async def _close(self):
        if self.room:
            self.room.close()  # Revoke before waiting for HTTP requests to drain.
        if self.server:
            self.server.should_exit = True
        if self.task:
            try:
                await asyncio.wait_for(asyncio.shield(self.task), timeout=3)
            except TimeoutError:
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)
            except Exception:
                pass
        if self.sock:
            self.sock.close()
        self.room = self.server = self.task = self.sock = self.url = None
        self.bind_host = self.advertise_host = None

    async def close(self):
        async with self.lock:
            await self._close()
        return self.status()
