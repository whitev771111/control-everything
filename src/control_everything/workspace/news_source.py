"""One process-local public cache and a single, non-queued owned collection slot."""
import atexit
import copy
from dataclasses import dataclass, field
from datetime import datetime, timezone
import secrets
import threading
import time

from .news_worker import OwnedBatch, definitions


@dataclass
class Slot:
    stream: str
    token: str
    deadline: float
    stop: threading.Event = field(default_factory=threading.Event)
    thread: object = None
    owned: object = None


class Collector:
    def __init__(self, *, clock=time.monotonic, launch=None, runner=OwnedBatch):
        self.clock, self.launch, self.runner = clock, launch, runner
        self.lock = threading.Lock()
        self.slot = None
        self.blocked = False
        self.leases = set()
        self.cache = {}
        self.attempts = {}
        self.sampled = {}
        self.failures = {}

    def acquire(self):
        token = secrets.token_hex(16)
        with self.lock:
            self.leases.add(token)
        return token

    def release(self, token, *, wait=4):
        with self.lock:
            self.leases.discard(token)
            slot = self.slot if not self.leases else None
            if slot:
                slot.stop.set()
        if slot and slot.thread and slot.thread.ident is not None and slot.thread is not threading.current_thread():
            slot.thread.join(max(0, min(4, wait)))
        with self.lock:
            if slot and self.slot is slot:
                self.blocked = True

    def _launch(self, slot):
        try:
            if self.launch is not None:
                self.launch(slot)
                return
            slot.thread = threading.Thread(target=self._run, args=(slot,), name='news-collector', daemon=True)
            slot.thread.start()
        except Exception:
            self.finish(slot, [], 'worker_failed', slot.owned is None)

    def _run(self, slot):
        try:
            slot.owned = self.runner(slot.stream, slot.token, slot.deadline, slot.stop, clock=self.clock)
            frames, reason, cleaned = slot.owned.run()
        except Exception:
            # The fixed runner constructor only records fields, before any Win32 call.
            frames, reason, cleaned = [], 'worker_failed', slot.owned is None
        self.finish(slot, frames, reason, cleaned)

    def finish(self, slot, frames, reason, cleaned):
        with self.lock:
            if self.slot is not slot:
                return
            if not cleaned:
                self.blocked = True
                return
            if self.blocked:
                return
            now = self.clock()
            self.sampled[slot.stream] = datetime.now(timezone.utc).isoformat()
            by_id = {frame['source_id']: frame for frame in frames}
            for key, name, url in definitions(slot.stream):
                frame = by_id.get(key)
                failure = (frame['reason'] if frame else reason or 'worker_failed')
                if frame and frame['result'] is not None:
                    value = frame['result']
                    self.cache[key] = {'id': key, 'name': name, 'url': frame['url'], 'mime': frame['mime'],
                                       'items': copy.deepcopy(value['items']), 'result': copy.deepcopy(value),
                                       'success': now, 'last_success_at': frame['fetched_at'],
                                       'sampled_at': frame['fetched_at']}
                    failure = reason or value['reason']
                elif key in self.cache:
                    self.cache[key]['sampled_at'] = frame['fetched_at'] if frame else self.sampled[slot.stream]
                self.failures[key] = failure
            self.slot = None

    def get(self, stream='ai', *, refresh=False, cached_only=False):
        definitions(stream)
        start = None
        with self.lock:
            now = self.clock()
            if self.slot is not None and now >= self.slot.deadline + 2:
                self.slot.stop.set()
                self.blocked = True
            stale = any(key not in self.cache or now - self.cache[key]['success'] >= 900
                        or self.failures.get(key) not in (None, 'filtered_all') for key, _, _ in definitions(stream))
            elapsed = now - self.attempts.get(stream, float('-inf'))
            if not cached_only and self.leases and not self.blocked and self.slot is None and (refresh or stale) and elapsed >= 60:
                start = self.slot = Slot(stream, secrets.token_hex(16), now + 15)
                self.attempts[stream] = now
            value = self._view(stream, now, refresh)
        # No Windows/network/wait call runs while holding the cache lock.
        if start:
            self._launch(start)
        return value

    def _view(self, stream, now, refresh=False):
        sources, items = [], []
        for key, name, url in definitions(stream):
            cached = self.cache.get(key)
            reason = self.failures.get(key)
            age = max(0, int((now - cached['success']) * 1000)) if cached else 0
            if cached:
                expired = age >= 900000
                status = 'stale' if reason and reason != 'filtered_all' or expired else 'available'
                reason = reason or ('cache_expired' if expired else None)
                result = cached['result']
                source = {'id': key, 'name': name, 'url': cached['url'], 'status': status, 'reason': reason,
                          'sampled_at': cached['sampled_at'], 'last_success_at': cached['last_success_at'],
                          'item_count': len(cached['items']), 'cache_age_ms': age, 'fresh_for_ms': max(0, 900000 - age),
                          **{field: result[field] for field in ('scanned_count', 'valid_count', 'filtered_count',
                                   'duplicate_count', 'returned_count', 'truncated', 'format')}}
                items.extend(copy.deepcopy(cached['items']))
            else:
                source = {'id': key, 'name': name, 'url': url, 'status': 'unavailable' if reason or self.blocked else 'pending',
                          'reason': reason or ('cleanup_unverified' if self.blocked else 'not_loaded'),
                          'sampled_at': self.sampled.get(stream), 'last_success_at': None, 'item_count': 0,
                          'cache_age_ms': 0, 'fresh_for_ms': 0, 'scanned_count': 0, 'valid_count': 0,
                          'filtered_count': 0, 'duplicate_count': 0, 'returned_count': 0, 'truncated': False, 'format': None}
            sources.append(source)
        if all(row['status'] == 'available' for row in sources):
            status, reason = 'available', None
        elif any(row['last_success_at'] is not None for row in sources):
            status, reason = 'stale', 'partial_failure' if any(row['reason'] not in (None, 'cache_expired') for row in sources) else 'cache_expired'
        elif any(row['status'] == 'pending' for row in sources):
            status, reason = 'pending', 'not_loaded'
        else:
            status, reason = 'unavailable', 'partial_failure'
        slot = self.slot
        elapsed = now - self.attempts.get(stream, float('-inf'))
        retry = int(max(0, 60 - elapsed) * 1000)
        if self.blocked:
            state, refresh_reason, retry = 'blocked', 'cleanup_unverified', 0
            status, reason = ('stale' if items or any(row['last_success_at'] for row in sources) else 'unavailable'), refresh_reason
        elif slot:
            state, refresh_reason = ('pending', 'refreshing') if slot.stream == stream else ('idle', 'worker_busy')
            retry = max(1, int((slot.deadline - now) * 1000)) if slot.stream != stream else 0
        else:
            state, refresh_reason = 'idle', 'cooldown' if retry else None
        utc_now = datetime.now(timezone.utc)
        def order(row):
            date = datetime.fromisoformat(row['published_at']) if row['published_at'] else None
            return (0 if date <= utc_now else 2, -date.timestamp(), row['id']) if date else (1, 0, row['id'])
        return {'schema': 1, 'stream': stream, 'status': status, 'reason': reason, 'sampled_at': self.sampled.get(stream),
                'items': sorted(items, key=order),
                'sources': copy.deepcopy(sources), 'refresh': {'state': state, 'reason': refresh_reason, 'retry_after_ms': retry}}


PUBLIC_COLLECTOR = Collector()


class NewsSource:
    def __init__(self, collector=None):
        self.collector = collector or PUBLIC_COLLECTOR

    def get(self, stream='ai'):
        return self.collector.get(stream)

    def refresh(self, stream='ai'):
        return self.collector.get(stream, refresh=True)

    def cached(self, stream='ai'):
        return self.collector.get(stream, cached_only=True)

    def acquire(self):
        return self.collector.acquire()

    def release(self, token):
        return self.collector.release(token)


def _shutdown():
    with PUBLIC_COLLECTOR.lock:
        leases = list(PUBLIC_COLLECTOR.leases)
    for token in leases:
        PUBLIC_COLLECTOR.release(token)


atexit.register(_shutdown)
