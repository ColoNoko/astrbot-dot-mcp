"""Bounded, ephemeral group-file matching; never an independent access grant.

The host validates actual raw events and owns network/cache work. This module
only retains immutable metadata and exact source handles for a short window.
"""
from collections import OrderedDict
from dataclasses import dataclass, field
import asyncio
import json
import re
import time
import uuid

CANDIDATE_TTL_SECONDS = 600
MAX_CANDIDATES = 32
MAX_SCOPE_CANDIDATES = 8
MAX_METADATA_BYTES = 256 * 1024
MAX_SEEN_SOURCES = 256
MAX_REQUESTS = 32
MAX_SCOPE_REQUESTS = 1
REQUEST_WAIT_SECONDS = 30

# Conservative, local intent recognition. Attachment names and quoted/nested
# messages are never fed back into this authorization decision.
_ACTION = re.compile(r'读取|读一下|读一读|读下|读这个|读文件|读附件|分析|解析|总结|处理|提取|翻译|检查|审阅|打开|查看|看一下|看下|看看|阅读|\b(?:inspect|read|review|analy[sz]e|summari[sz]e|process|extract|translate|check|open)\b', re.I)
_FILE = re.compile(r'文件|附件|文档|表格|压缩包|报告|PDF|WORD|EXCEL|PPT|file|attachment|document|spreadsheet|archive', re.I)
_NEGATED = re.compile(r'不要|不用|无需|别(?:读|看|处理|分析|打开)|do\s+not|don.t\s+(?:read|review|analy[sz]e|process|open)', re.I)
# ASCII suffix boundaries let Chinese request text follow an extension, but
# reject extended suffixes and path continuations. A sentence-ending dot is OK.
_FILENAME_END = r'(?![a-z0-9_-]|\.[a-z0-9]|[/\\])'
_FILENAME = re.compile(r'[^\s《》“”\"\'<>，。！？、,:;]+\.(?:pdf|docx?|xlsx?|pptx?|csv|txt|md|json|zip|tar|gz|log|xml|html)' + _FILENAME_END, re.I)


_REFERENCE = re.compile(r'这个|这份|这个|这张|刚才|刚刚|上面|上述|上传的|发的|发给你|这[个份张]?文件|\b(?:this|that|uploaded|previous)\b', re.I)
_CAPABILITY = re.compile(r'你能|能否|是否(?:能|可以|支持)|支持.*(?:吗|么)|怎样|怎么(?:样)?(?:才能|去|打开|读取|处理)|如何|\b(?:can you|could you|do you support|how (?:do|can|to))\b', re.I)

def file_intent(value):
    """Require an action plus explicit file reference in actual top-level text."""
    text = value.get('intent_text', value.get('text', ''))
    return (isinstance(text, str) and not _NEGATED.search(text) and
            not _CAPABILITY.search(text) and bool(_ACTION.search(text)) and
            bool((_REFERENCE.search(text) and _FILE.search(text)) or
                 _FILENAME.search(text) or value.get('reply_source_ids')))


def scope(value):
    return value['group_id'], value['sender_id']


def identity(value):
    return (*scope(value), value['source_id'])


@dataclass
class Candidate:
    id: str
    value: dict
    event: object
    bot: object
    host: object
    created_at: float
    expires_at: float
    expires_wall: int
    metadata_bytes: int
    specs: list
    tokens: dict = field(default_factory=dict)
    states: dict = field(default_factory=dict)
    errors: dict = field(default_factory=dict)
    timings: dict = field(default_factory=dict)
    task: object = None
    linked: bool = False

    def choices(self):
        return {'filenames': [item['filename'] for item in self.specs],
                'received_at': int(self.created_at), 'untrusted': True}


@dataclass
class Request:
    value: dict
    event: object
    bot: object
    host: object
    captured_at: object
    deadline: float
    wake: object = field(default_factory=asyncio.Event)
    task: object = None


class GroupFileCandidates:
    def __init__(self, *, clock=time.monotonic, wall=time.time):
        self.clock, self.wall = clock, wall
        self.candidates = OrderedDict()
        self.requests = OrderedDict()
        self.seen_sources = OrderedDict()
        self.expired_count = 0
        self.rejected_count = 0
        self.cache_failures = 0
        self.closed = False

    def expire(self):
        expired = []
        for key, item in list(self.candidates.items()):
            if item.expires_at <= self.clock():
                expired.append(self.candidates.pop(key))
                self.expired_count += 1
        return expired

    def add(self, value, event, host):
        if self.closed:
            return None, False
        key = identity(value)
        existing = self.candidates.get(key)
        if existing:
            # A replay cannot refresh expiry or replace a captured source.
            return (existing, False) if existing.value == value else (None, False)
        if key in self.seen_sources:
            return None, False
        specs = [dict(item) for item in value.get('attachments', []) if item['kind'] == 'file']
        if not specs:
            return None, False
        immutable = json.loads(json.dumps(value, ensure_ascii=False))
        size = len(json.dumps(immutable, ensure_ascii=False).encode('utf-8'))
        if (len(self.candidates) >= MAX_CANDIDATES or
                sum(scope(c.value) == scope(value) for c in self.candidates.values()) >= MAX_SCOPE_CANDIDATES or
                size + sum(c.metadata_bytes for c in self.candidates.values()) > MAX_METADATA_BYTES):
            self.rejected_count += 1
            return None, False
        now, wall = self.clock(), self.wall()
        item = Candidate('candidate_' + uuid.uuid4().hex, immutable, event,
                         getattr(event, 'bot', None), host, wall,
                         now + CANDIDATE_TTL_SECONDS, int(wall + CANDIDATE_TTL_SECONDS), size, specs)
        self.candidates[key] = item
        self.seen_sources[key] = True
        while len(self.seen_sources) > MAX_SEEN_SOURCES:
            self.seen_sources.popitem(last=False)
        return item, True

    def live(self, candidate):
        return (not self.closed and self.candidates.get(identity(candidate.value)) is candidate and
                candidate.expires_at > self.clock())

    def resolve(self, value):
        candidates = [c for c in self.candidates.values()
                      if self.live(c) and scope(c.value) == scope(value)]
        replies = value.get('reply_source_ids', [])
        if replies:
            candidates = [c for c in candidates if c.value['source_id'] in replies]
        selections = [(c, spec) for c in candidates for spec in c.specs]
        text = value.get('intent_text', value['text']).casefold()
        named = [(c, spec) for c, spec in selections if
                 len(spec['filename']) > 3 and re.search(r'(?<![a-z0-9_.-])' +
                    re.escape(spec['filename'].casefold()) + _FILENAME_END, text)]
        if named:
            selections = named
        elif _FILENAME.search(text):
            selections = []
        if len(selections) == 1:
            return 'matched', selections
        return ('ambiguous' if selections else 'waiting'), selections

    def add_request(self, value, event, host, captured_at):
        key = identity(value)
        if key in self.requests:
            return self.requests[key], False
        if (self.closed or len(self.requests) >= MAX_REQUESTS or
                sum(scope(r.value) == scope(value) for r in self.requests.values()) >= MAX_SCOPE_REQUESTS):
            self.rejected_count += 1
            return None, False
        request = Request(json.loads(json.dumps(value)), event, getattr(event, 'bot', None),
                          host, captured_at, self.clock() + REQUEST_WAIT_SECONDS)
        self.requests[key] = request
        return request, True

    def wake_scope(self, value):
        for request in self.requests.values():
            if scope(request.value) == scope(value):
                request.wake.set()

    def status(self):
        return {'enabled': not self.closed, 'candidate_messages': len(self.candidates),
                'pending_requests': len(self.requests), 'max_candidate_messages': MAX_CANDIDATES,
                'max_candidates_per_sender_group': MAX_SCOPE_CANDIDATES,
                'candidate_ttl_seconds': CANDIDATE_TTL_SECONDS,
                'request_wait_seconds': REQUEST_WAIT_SECONDS,
                'metadata_bytes': sum(c.metadata_bytes for c in self.candidates.values()),
                'max_metadata_bytes': MAX_METADATA_BYTES,
                'expired_count': self.expired_count, 'rejected_count': self.rejected_count,
                'cache_failures': self.cache_failures}
