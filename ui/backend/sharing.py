"""Ephemeral, single-session visitor leases. Access only on the backend event loop."""
import hashlib
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from .session_store import SessionStoreError

MAX_ONLINE = 20
LEASE_SECONDS = 30
MAX_VISITORS = 200
POLL_SECONDS = 2


@dataclass
class ShareError(Exception):
    code: str
    message: str
    status_code: int = 400


@dataclass
class Visitor:
    id: str
    nickname: str
    last_seen: float
    connected: bool = True


class ShareRoom:
    def __init__(self, sessions, session_id, clock=time.monotonic):
        self.sessions = sessions
        self.session_id = session_id
        self.clock = clock
        self.invitation = secrets.token_urlsafe(32)
        self.active = True
        self.visitors = {}
        self.cached = None
        self.cached_at = float('-inf')
        initial = self.snapshot()  # Validate the fixed session before publishing a link.
        self.work_type = initial['work_type']

    def require_active(self):
        if not self.active:
            raise ShareError('share_closed', '分享已關閉，請向主控端取得新連結。', 410)

    def online(self, visitor):
        return visitor.connected and self.clock() - visitor.last_seen < LEASE_SECONDS

    def roster(self):
        return [{"id": v.id, "nickname": v.nickname, "online": self.online(v)}
                for v in self.visitors.values()]

    def _capacity(self):
        if sum(self.online(v) for v in self.visitors.values()) >= MAX_ONLINE:
            raise ShareError('share_full', '目前已有 20 位參與者在線，請稍後再試。', 409)

    def join(self, invitation, nickname, credential=None):
        self.require_active()
        if not secrets.compare_digest(invitation.encode(), self.invitation.encode()):
            raise ShareError('invalid_invitation', '分享連結已失效。', 403)
        nickname = nickname.strip()
        if not nickname or len(nickname) > 40 or any(ord(c) < 32 for c in nickname):
            raise ShareError('invalid_nickname', '請填寫 1–40 字暱稱（不可包含控制字元）。')
        if credential in self.visitors:
            visitor = self.authorize(credential)
            visitor.nickname = nickname
            return credential
        self._capacity()
        if len(self.visitors) >= MAX_VISITORS:
            oldest = min((key for key, v in self.visitors.items() if not self.online(v)),
                         key=lambda key: self.visitors[key].last_seen)
            del self.visitors[oldest]
        credential = secrets.token_urlsafe(32)
        self.visitors[credential] = Visitor(secrets.token_hex(8), nickname, self.clock())
        return credential

    def authorize(self, credential):
        self.require_active()
        visitor = self.visitors.get(credential)
        if visitor is None:
            raise ShareError('join_required', '請使用分享連結並填寫暱稱加入。', 401)
        if not self.online(visitor):
            self._capacity()
        visitor.connected = True
        visitor.last_seen = self.clock()
        return visitor

    def leave(self, credential):
        self.require_active()
        visitor = self.visitors.get(credential)
        if visitor:
            visitor.connected = False

    def close(self):
        self.active = False
        self.invitation = ''
        self.visitors.clear()
        self.cached = None

    def snapshot(self):
        self.require_active()
        now = self.clock()
        if self.cached is not None and now - self.cached_at < POLL_SECONDS:
            return self.cached
        try:
            detail = self.sessions.get(self.session_id)
        except (SessionStoreError, OSError):
            # Never forward local paths or internal errors to the LAN.
            raise ShareError('content_unavailable', '目前無法讀取分享內容，請稍後重試。', 503) from None
        session = detail['session']
        work_type = session.get('work_type') or 'lecture'
        if work_type not in {'lecture', 'meeting'}:
            raise ShareError('unsupported_work_type', '此場次類型尚不能分享。', 422)
        transcript = detail['transcript']
        secondary = detail.get('speaker_transcript') if work_type == 'meeting' else detail['notes']
        if secondary is None:
            raise ShareError('content_unavailable', '目前無法讀取分享內容，請稍後重試。', 503)
        digest = hashlib.sha256((transcript['content'] + '\0' + secondary['content']).encode()).hexdigest()[:16]
        self.cached = {
            'api_version': 1,
            'work_type': work_type,
            'course': session.get('course'),
            'phase': session.get('phase'),
            'transcript': transcript,
            'version': digest,
            'version_label': '目前版本',
            'captured_at': datetime.now(timezone.utc).isoformat(),
            'poll_seconds': POLL_SECONDS,
        }
        if work_type == 'meeting':
            self.cached['speaker_transcript'] = secondary
        else:
            self.cached['notes'] = secondary
        self.cached_at = now
        return self.cached
