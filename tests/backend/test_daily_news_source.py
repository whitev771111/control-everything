"""Fake-only news ownership, framing, transport and cache contracts."""
import json
import io
import threading
import unittest
from email.message import Message
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError
from unittest.mock import Mock

from session_panel.workspace import news_source as source
from session_panel.workspace import news_worker as worker


class FakeAPI:
    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail
        self.running = True

    def call(self, name, *args):
        self.calls.append((name, *args))
        if name == self.fail:
            raise OSError('synthetic failure')

    def create_job(self, owned):
        owned.job = 1
        self.call('job')

    def limits(self, job):
        self.call('limits', job)

    def pipes(self, owned):
        owned.handles.extend([4, 5, 6, 7, 8])
        owned.stdout = 4
        self.call('pipes')

    def spawn(self, owned, stream, token):
        owned.process, owned.thread = 2, 3
        self.call('spawn')

    def assign(self, job, process):
        self.call('assign', job, process)

    def resume(self, thread):
        self.call('resume', thread)

    def signaled(self, handle, timeout=0):
        self.call('wait', handle)
        return not self.running

    def active(self, job):
        self.call('active', job)
        return int(self.running)

    def terminate_process(self, process):
        self.call('terminate_process', process)
        self.running = False

    def terminate_job(self, job):
        self.call('terminate_job', job)
        self.running = False

    def close(self, handle):
        self.call('close', handle)

    def read(self, pipe):
        return b''

    def exit_code(self, process):
        return 0


class NewsOwnershipTests(unittest.TestCase):
    def batch(self, api, cancelled=False):
        stop = threading.Event()
        if cancelled:
            stop.set()
        batch = worker.OwnedBatch('ai', 'a' * 32, 100, stop, api=api, clock=lambda: 0)
        return batch, batch.run()

    def test_limits_failure_never_spawns_and_closes_after_no_process_proof(self):
        api = FakeAPI('limits')
        owned, result = self.batch(api)
        self.assertEqual((result[1], result[2]), ('worker_failed', True))
        self.assertNotIn(('spawn',), api.calls)
        self.assertIsNone(owned.job)

    def test_assign_failure_terminates_exact_process_then_waits_both_before_close(self):
        api = FakeAPI('assign')
        owned, result = self.batch(api)
        self.assertTrue(result[2])
        self.assertNotIn(('resume', 3), api.calls)
        terminate = api.calls.index(('terminate_process', 2))
        close = next(i for i, call in enumerate(api.calls) if call[0] == 'close')
        self.assertLess(terminate, api.calls.index(('wait', 2), terminate))
        self.assertLess(api.calls.index(('wait', 3), terminate), close)
        self.assertIsNone(owned.process)

    def test_failed_termination_or_wait_or_close_retains_required_resources(self):
        for fail in ('terminate_process', 'wait', 'close'):
            api = FakeAPI('assign')
            original = api.call
            def call(name, *args):
                original(name, *args) if name != 'assign' else api.calls.append((name, *args))
                if name in ('assign', fail):
                    raise OSError('synthetic failure')
            api.call = call
            with self.subTest(fail=fail):
                owned, result = self.batch(api)
                self.assertFalse(result[2])
                self.assertTrue(owned.process or owned.handles)

    def test_resume_failure_uses_job_not_bare_process(self):
        api = FakeAPI('resume')
        _, result = self.batch(api)
        self.assertTrue(result[2])
        self.assertIn(('terminate_job', 1), api.calls)
        self.assertNotIn(('terminate_process', 2), api.calls)

    def test_cancel_during_start_never_resumes(self):
        api = FakeAPI()
        _, result = self.batch(api, True)
        self.assertNotIn(('resume', 3), api.calls)
        self.assertTrue(result[2])

    def test_wait_timeout_retains_exact_unassigned_handles(self):
        api = FakeAPI('assign')
        api.signaled = Mock(return_value=False)
        owned, result = self.batch(api)
        self.assertFalse(result[2])
        self.assertEqual((owned.process, owned.thread), (2, 3))
        self.assertFalse(any(call[0] == 'close' for call in api.calls))

    def test_eof_precedes_exit_and_code_is_read_only_after_both_terminal_handles(self):
        api = FakeAPI()
        done = json.dumps({'schema': 1, 'type': 'done', 'stream': 'ai', 'token': 'a' * 32}).encode() + b'\n'
        reads = iter((done, b''))
        api.read = lambda _: next(reads)
        process_waits = []
        def signaled(handle, timeout=0):
            api.calls.append(('wait', handle))
            if handle == 2 and not process_waits:
                process_waits.append(False)
                return False
            api.running = False
            return True
        api.signaled = signaled
        def exit_code(handle):
            self.assertIn(('wait', 3), api.calls)
            api.calls.append(('exit_code', handle))
            return 0
        api.exit_code = exit_code
        _, result = self.batch(api)
        self.assertTrue(result[2])
        self.assertIsNone(result[1])
        self.assertNotIn(('terminate_job', 1), api.calls)

    def test_eof_that_never_exits_reaches_deadline_then_owned_job_cleanup(self):
        api = FakeAPI()
        done = json.dumps({'schema': 1, 'type': 'done', 'stream': 'ai', 'token': 'a' * 32}).encode() + b'\n'
        reads = iter((done, b''))
        api.read = lambda _: next(reads)
        tick = [0]
        def clock():
            tick[0] += 1
            return tick[0]
        batch = worker.OwnedBatch('ai', 'a' * 32, 5, threading.Event(), api=api, clock=clock)
        frames, reason, cleaned = batch.run()
        self.assertEqual((reason, cleaned), ('timeout', True))
        self.assertIn(('terminate_job', 1), api.calls)


class NewsCacheTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.jobs = []
        self.collector = source.Collector(clock=lambda: self.now, launch=self.jobs.append)
        self.lease = self.collector.acquire()

    def tearDown(self):
        self.collector.release(self.lease, wait=0)

    def test_many_requests_reserve_one_and_other_stream_is_busy(self):
        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(lambda _: self.collector.get(), range(100)))
        self.assertTrue(all(row['status'] == 'pending' for row in results))
        self.assertEqual(len(self.jobs), 1)
        self.assertEqual(self.collector.get('hn')['refresh']['reason'], 'worker_busy')

    def test_empty_success_is_cached_copied_expires_and_error_preserves_old(self):
        self.collector.get()
        slot = self.jobs[0]
        frames = [worker.source_frame('ai', slot.token, key, worker.empty_result(), None,
                                      url, None, '2026-10-17T00:00:00+00:00') for key, _, url in worker.FEEDS]
        self.collector.finish(slot, frames, None, True)
        first = self.collector.get()
        self.assertEqual(first['status'], 'available')
        first['sources'].clear()
        self.assertEqual(len(self.collector.get()['sources']), 3)
        self.now = 901
        self.assertEqual(self.collector.get()['status'], 'stale')
        self.collector.finish(self.jobs[-1], [], 'timeout', True)
        result = self.collector.get()
        self.assertEqual(result['status'], 'stale')
        self.assertTrue(all(row['last_success_at'] for row in result['sources']))
        self.assertEqual(result['refresh']['reason'], 'cooldown')

    def test_failed_cleanup_sticky_slot_never_restarts_on_get_refresh_or_lease(self):
        self.collector.get()
        slot = self.jobs[0]
        self.collector.finish(slot, [], 'worker_failed', False)
        for _ in range(100):
            self.collector.get(refresh=True)
        self.collector.release(self.lease, wait=0)
        self.lease = self.collector.acquire()
        self.assertEqual(self.collector.get()['refresh']['state'], 'blocked')
        self.assertEqual(len(self.jobs), 1)
        self.assertIs(self.collector.slot, slot)

    def test_last_lease_cancels_only_own_slot_and_second_lease_keeps_it(self):
        other = self.collector.acquire()
        self.collector.get()
        slot = self.jobs[0]
        self.collector.release(other, wait=0)
        self.assertFalse(slot.stop.is_set())
        self.collector.release(self.lease, wait=0)
        self.assertTrue(slot.stop.is_set())

    def test_late_cleanup_keeps_sticky_block_and_original_slot(self):
        self.collector.get()
        slot = self.jobs[0]
        self.collector.release(self.lease, wait=0)
        self.collector.finish(slot, [], 'timeout', True)
        self.assertIs(self.collector.slot, slot)
        self.assertTrue(self.collector.blocked)

    def test_cached_read_without_lease_never_launches(self):
        self.collector.release(self.lease)
        for _ in range(10):
            self.collector.get('hn', cached_only=True)
            self.collector.get('hn', refresh=True)
        self.assertEqual(self.jobs, [])

    def test_pure_runner_constructor_failure_ends_pending(self):
        self.collector.runner = Mock(side_effect=ValueError('synthetic constructor'))
        self.collector.get()
        slot = self.jobs[0]
        self.collector._run(slot)
        self.assertIsNone(self.collector.slot)
        self.assertEqual(self.collector.get()['status'], 'unavailable')

    def test_last_close_between_thread_publication_and_start_is_bounded_blocked(self):
        self.collector.get()
        slot = self.jobs[0]
        slot.thread = threading.Thread(target=lambda: None)
        self.collector.release(self.lease, wait=0)
        self.assertTrue(self.collector.blocked)
        self.assertTrue(slot.stop.is_set())

    def test_overdue_leaf_marks_blocked_on_read_and_late_completion_cannot_unlock(self):
        self.collector.get()
        slot = self.jobs[0]
        marker = object()
        slot.owned = marker
        self.now = 16.999
        self.assertNotEqual(self.collector.get(cached_only=True)['refresh']['state'], 'blocked')
        self.now = 17.001
        value = self.collector.get(cached_only=True)
        self.assertEqual((value['status'], value['reason'], value['refresh']['state']),
                         ('unavailable', 'cleanup_unverified', 'blocked'))
        self.assertTrue(slot.stop.is_set())
        self.assertIs(self.collector.slot, slot)
        self.assertIs(slot.owned, marker)
        self.collector.finish(slot, [], None, True)
        for _ in range(100):
            self.collector.get(refresh=True)
        self.assertIs(self.collector.slot, slot)
        self.assertTrue(self.collector.blocked)
        self.assertEqual(len(self.jobs), 1)


class NewsFramesTests(unittest.TestCase):
    def frame(self, key='openai'):
        definition = next(row for row in worker.FEEDS if row[0] == key)
        return worker.source_frame('ai', 'a' * 32, key, worker.empty_result(), None,
                                   definition[2], 'application/rss+xml', '2026-10-17T00:00:00+00:00')

    def line(self, value):
        return (json.dumps(value) + '\n').encode()

    def test_split_frame_done_and_exact_identity(self):
        parser = worker.Frames('ai', 'a' * 32)
        data = self.line(self.frame())
        parser.feed(data[:10]); parser.feed(data[10:])
        parser.feed(self.line({'schema': 1, 'type': 'done', 'stream': 'ai', 'token': 'a' * 32}))
        self.assertTrue(parser.done)
        self.assertEqual(len(parser.frames), 1)
        with self.assertRaises(ValueError):
            parser.feed(data)

    def test_bad_frames_duplicates_and_output_limits_are_rejected(self):
        for mutate in (lambda row: row.update(token='b' * 32), lambda row: row.update(extra=True),
                       lambda row: row.update(source_id='unknown'), lambda row: row.update(reason='private exception'),
                       lambda row: row['result'].update(returned_count=1), lambda row: row.update(url='https://evil.invalid/')):
            row = self.frame(); mutate(row)
            with self.subTest(row=row), self.assertRaises(ValueError):
                worker.Frames('ai', 'a' * 32).feed(self.line(row))
        parser = worker.Frames('ai', 'a' * 32)
        parser.feed(self.line(self.frame()))
        with self.assertRaises(ValueError):
            parser.feed(self.line(self.frame()))
        for data in (b'x' * (worker.LINE_BYTES + 1), b'{"x":1,"x":2}\n', b'{"x":NaN}\n', b'\xff\n'):
            with self.assertRaises((ValueError, UnicodeError)):
                worker.Frames('ai', 'a' * 32).feed(data)

    def test_timeout_keeps_completed_source_but_missing_done_is_failure(self):
        class API(FakeAPI):
            def __init__(self, data):
                super().__init__(); self.data = [data, b'']
            def read(self, pipe):
                data = self.data.pop(0)
                if not data:
                    self.running = False
                return data
        batch = worker.OwnedBatch('ai', 'a' * 32, 100, threading.Event(),
                                  api=API(self.line(self.frame())), clock=lambda: 0)
        frames, reason, cleaned = batch.run()
        self.assertEqual((len(frames), reason, cleaned), (1, 'worker_failed', True))


class NewsTransportTests(unittest.TestCase):
    def response(self, data, encoding=None):
        response = io.BytesIO(data)
        response.headers = Message()
        response.headers['Content-Type'] = 'application/rss+xml'
        if encoding:
            response.headers['Content-Encoding'] = encoding
        return response

    def test_fixed_request_no_auth_identity_encoding_and_bounded_read(self):
        opener = Mock()
        opener.open.return_value = self.response(b'<rss version="2.0"><channel/></rss>')
        frame = worker.fetch(worker.FEEDS[0], 'ai', 'a' * 32, 100, opener=opener, clock=lambda: 0)
        self.assertIsNone(frame['reason'])
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_header('Accept-encoding'), 'identity')
        self.assertIsNone(request.get_header('Authorization'))
        self.assertEqual(opener.open.call_args.kwargs['timeout'], 8)

    def test_same_host_redirect_allowed_cross_host_rejected_before_second_open(self):
        for location, expected in (('/new/rss/', None), ('https://evil.invalid/feed', 'redirect_rejected'),
                                   ('http://blog.google/feed', 'redirect_rejected'), ('https://user:secret@blog.google/feed', 'redirect_rejected')):
            headers = Message(); headers['Location'] = location
            error = HTTPError(worker.FEEDS[0][2], 301, 'synthetic', headers, io.BytesIO())
            opener = Mock()
            opener.open.side_effect = [error, self.response(b'<rss version="2.0"><channel/></rss>')]
            frame = worker.fetch(worker.FEEDS[0], 'ai', 'a' * 32, 100, opener=opener, clock=lambda: 0)
            self.assertEqual(frame['reason'], expected)
            self.assertEqual(opener.open.call_count, 2 if expected is None else 1)

    def test_expiry_oversize_and_encoding_fail_without_partial_result(self):
        for data, encoding, reason in ((b'x' * 1048577, None, 'oversize'), (b'xml', 'gzip', 'encoding_unsupported')):
            opener = Mock(); opener.open.return_value = self.response(data, encoding)
            frame = worker.fetch(worker.FEEDS[0], 'ai', 'a' * 32, 100, opener=opener, clock=lambda: 0)
            self.assertEqual((frame['reason'], frame['result']), (reason, None))
        opener = Mock()
        frame = worker.fetch(worker.FEEDS[0], 'ai', 'a' * 32, 0, opener=opener, clock=lambda: 0)
        self.assertEqual(frame['reason'], 'timeout')
        opener.open.assert_not_called()


if __name__ == '__main__':
    unittest.main()
