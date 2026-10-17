"""Fixed public-source worker and its narrowly owned Windows Job boundary."""
import ctypes as c
from ctypes import wintypes as w
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

# Isolated script execution adds only this file's fixed package import root.
if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from control_everything.platform.windows_jobs import WindowsAPI, ExtendedLimits, SecurityAttributes, ProcessError
from control_everything.workspace.news_parse import parse_feed_result, parse_hn_result, safe_news_url

FEEDS = (('google_ai', 'Google AI', 'https://blog.google/technology/ai/rss/'),
         ('openai', 'OpenAI News', 'https://openai.com/news/rss.xml'),
         ('huggingface', 'Hugging Face Blog', 'https://huggingface.co/blog/feed.xml'))
HN = ('hn', 'Hacker News', 'https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage=4')
LINE_BYTES = 256 * 1024
REASONS = {'timeout', 'http_error', 'oversize', 'invalid_feed', 'unsupported_format', 'parse_budget',
           'redirect_rejected', 'encoding_unsupported', 'worker_failed', 'platform_unsupported'}


def definitions(stream):
    if stream not in ('ai', 'hn'):
        raise ValueError('invalid_stream')
    return FEEDS if stream == 'ai' else (HN,)


def empty_result():
    return {'format': 'rss2', 'items': [], 'reason': None, 'scanned_count': 0, 'valid_count': 0,
            'filtered_count': 0, 'duplicate_count': 0, 'returned_count': 0, 'truncated': False}


def source_frame(stream, token, source_id, result, reason, url, mime, fetched_at):
    return {'schema': 1, 'type': 'source', 'stream': stream, 'token': token, 'source_id': source_id,
            'result': result, 'reason': reason, 'url': url, 'mime': mime, 'fetched_at': fetched_at}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def source_url(value, original):
    try:
        parsed = urlsplit(value)
        return safe_news_url(value) and len(value.encode('utf-8')) <= 2048 and parsed.hostname == urlsplit(original).hostname and parsed.port in (None, 443)
    except (ValueError, UnicodeError):
        return False


def fetch(definition, stream, token, deadline, *, opener=None, clock=time.monotonic):
    source_id, name, original = definition
    url, mime, result, reason = original, None, None, None
    opener = opener or build_opener(ProxyHandler({}), NoRedirect())
    try:
        for hop in range(4):
            if not source_url(url, original):
                raise ValueError('redirect_rejected')
            remaining = deadline - clock()
            if remaining <= 0:
                raise TimeoutError()
            request = Request(url, headers={'User-Agent': 'MianbanWorkspace/1.0', 'Accept-Encoding': 'identity'})
            try:
                response = opener.open(request, timeout=min(8, remaining))
            except HTTPError as error:
                if error.code in (301, 302, 303, 307, 308):
                    location = error.headers.get('Location')
                    error.close()
                    if not location or hop == 3:
                        raise ValueError('redirect_rejected') from None
                    if any(ord(char) < 33 for char in location) or '\\' in location:
                        raise ValueError('redirect_rejected')
                    try:
                        url = urljoin(url, location)
                    except ValueError:
                        raise ValueError('redirect_rejected') from None
                    continue
                error.close()
                raise
            with response:
                if response.headers.get('Content-Encoding', 'identity').lower() not in ('', 'identity'):
                    raise ValueError('encoding_unsupported')
                mime = response.headers.get_content_type()[:128]
                data = bytearray()
                while True:
                    remaining = deadline - clock()
                    if remaining <= 0:
                        raise TimeoutError()
                    chunk = response.read(min(65536, 1048577 - len(data)))
                    if not chunk:
                        break
                    data.extend(chunk)
                    if len(data) > 1048576:
                        raise ValueError('oversize')
            fetched = datetime.now(timezone.utc).isoformat()
            result = (parse_hn_result(bytes(data), fetched) if stream == 'hn' else
                      parse_feed_result(bytes(data), source_id, name, fetched, feed_url=url))
            fetched = datetime.now(timezone.utc).isoformat()
            for item in result['items']:
                item['fetched_at'] = fetched
            break
    except (TimeoutError, URLError, OSError, ValueError) as error:
        reason = ('timeout' if isinstance(error, TimeoutError) or isinstance(error, URLError)
                  and isinstance(error.reason, TimeoutError) else
                  str(error) if isinstance(error, ValueError) and str(error) in REASONS else 'http_error')
        if not source_url(url, original):
            url = original
    return source_frame(stream, token, source_id, result, reason, url, mime, datetime.now(timezone.utc).isoformat())


class NewsWindowsAPI:
    """Checked leaves retain every handle in OwnedBatch before another fallible call."""
    def __init__(self):
        self.base = WindowsAPI()
        self.kernel = self.base.kernel
        self.kernel.GetExitCodeProcess.argtypes = [w.HANDLE, c.POINTER(w.DWORD)]
        self.kernel.GetExitCodeProcess.restype = w.BOOL

    def create_job(self, owned):
        owned.job = self.base.checked(self.kernel.CreateJobObjectW(None, None))

    def limits(self, job):
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000 | 0x8 | 0x200
        limits.basic.active_limit = 2
        limits.job_memory = 256 * 1024 * 1024
        self.base.checked(self.kernel.SetInformationJobObject(job, 9, c.byref(limits), c.sizeof(limits)))

    def pipes(self, owned):
        attributes = SecurityAttributes(c.sizeof(SecurityAttributes), None, True)
        def pipe():
            read, write = w.HANDLE(), w.HANDLE()
            self.base.checked(self.kernel.CreatePipe(c.byref(read), c.byref(write), c.byref(attributes), 0))
            owned.handles.extend((read.value, write.value))
            return read.value, write.value
        child_in, parent_in = pipe()
        parent_out, child_out = pipe()
        owned.stdout = parent_out
        self.base.checked(self.kernel.SetHandleInformation(parent_in, 1, 0))
        self.base.checked(self.kernel.SetHandleInformation(parent_out, 1, 0))
        nul = self.base.checked(self.kernel.CreateFileW('NUL', 0x40000000, 3, c.byref(attributes), 3, 0x80, None))
        owned.handles.append(nul)
        owned.child_handles = (child_in, child_out, nul)
        owned.parent_in = parent_in

    def spawn(self, owned, stream, token):
        root = Path(__file__).resolve().parents[2]
        argv = ['-I', '-B', '-X', 'utf8', str(Path(__file__).resolve()), '1', stream, token]
        owned.process, owned.thread, owned.pid = self.base.spawn(sys.executable, argv, root, owned.child_handles)

    def assign(self, job, process):
        self.base.assign(job, process)

    def resume(self, thread):
        self.base.resume(thread)

    def active(self, job):
        return self.base.active(job)

    def signaled(self, handle, timeout=0):
        result = self.kernel.WaitForSingleObject(handle, max(0, int(timeout * 1000)))
        if result not in (0, 258):
            raise ProcessError('cleanup_unverified')
        return result == 0

    def terminate_process(self, process):
        self.base.checked(self.kernel.TerminateProcess(process, 1))

    def terminate_job(self, job):
        self.base.checked(self.kernel.TerminateJobObject(job, 1))

    def close(self, handle):
        self.base.checked(self.kernel.CloseHandle(handle))

    def read(self, pipe):
        return self.base.read_available(pipe)

    def exit_code(self, process):
        code = w.DWORD()
        self.base.checked(self.kernel.GetExitCodeProcess(process, c.byref(code)))
        return code.value


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('worker_failed')
        result[key] = value
    return result


class Frames:
    def __init__(self, stream, token):
        self.stream, self.token = stream, token
        self.pending = bytearray()
        self.total = 0
        self.frames, self.seen = [], set()
        self.done = False

    def feed(self, data):
        self.total += len(data)
        if self.total > len(definitions(self.stream)) * LINE_BYTES + 256:
            raise ValueError('oversize')
        self.pending.extend(data)
        while b'\n' in self.pending:
            line, _, self.pending = self.pending.partition(b'\n')
            if len(line) > LINE_BYTES or self.done:
                raise ValueError('worker_failed')
            value = json.loads(line.decode('utf-8'), object_pairs_hook=_pairs,
                               parse_constant=lambda _: (_ for _ in ()).throw(ValueError('worker_failed')))
            self.accept(value)
        if len(self.pending) > LINE_BYTES:
            raise ValueError('oversize')

    def accept(self, value):
        common = {'schema', 'type', 'stream', 'token'}
        if not isinstance(value, dict) or type(value.get('schema')) is not int or value.get('schema') != 1 or value.get('stream') != self.stream or value.get('token') != self.token:
            raise ValueError('worker_failed')
        if value.get('type') == 'done' and set(value) == common:
            self.done = True
            return
        if set(value) != common | {'source_id', 'result', 'reason', 'url', 'mime', 'fetched_at'} or value.get('type') != 'source':
            raise ValueError('worker_failed')
        known = {row[0]: row for row in definitions(self.stream)}
        key = value['source_id']
        if key not in known or key in self.seen or value['reason'] not in REASONS | {None}:
            raise ValueError('worker_failed')
        if not source_url(value['url'], known[key][2]) or not isinstance(value['fetched_at'], str) or len(value['fetched_at']) > 64:
            raise ValueError('worker_failed')
        if datetime.fromisoformat(value['fetched_at']).tzinfo is None:
            raise ValueError('worker_failed')
        mime = value['mime']
        if mime is not None and (not isinstance(mime, str) or len(mime) > 128):
            raise ValueError('worker_failed')
        result = value['result']
        if value['reason'] is None:
            validate_result(result, key, known[key][1], self.stream)
        elif result is not None:
            raise ValueError('worker_failed')
        self.seen.add(key)
        self.frames.append(value)


def validate_result(result, key, name, stream):
    expected = set(empty_result())
    if not isinstance(result, dict) or set(result) != expected or result['format'] not in (('hn',) if stream == 'hn' else ('rss2', 'atom1')):
        raise ValueError('worker_failed')
    for field in ('scanned_count', 'valid_count', 'filtered_count', 'duplicate_count', 'returned_count'):
        if type(result[field]) is not int or not 0 <= result[field] <= (4 if stream == 'hn' else 2000):
            raise ValueError('worker_failed')
    items = result['items']
    if not isinstance(items, list) or len(items) > (4 if stream == 'hn' else 20):
        raise ValueError('worker_failed')
    if result['scanned_count'] != result['valid_count'] + result['filtered_count'] + result['duplicate_count'] or result['returned_count'] != len(items):
        raise ValueError('worker_failed')
    if type(result['truncated']) is not bool or result['truncated'] != (result['valid_count'] > len(items)) or result['reason'] not in (None, 'filtered_all'):
        raise ValueError('worker_failed')
    fields = {'id', 'source_id', 'source_name', 'title', 'url', 'published_at', 'fetched_at', 'published_raw', 'date_reason',
              'updated_at', 'submitted_at', 'content_origin', 'summary', 'summary_kind', 'summary_truncated', 'summary_reason', 'title_truncated'}
    seen = set()
    for item in items:
        if not isinstance(item, dict) or set(item) != fields or item['source_id'] != key or item['source_name'] != name:
            raise ValueError('worker_failed')
        if not isinstance(item['id'], str) or not re.fullmatch('[a-f0-9]{24}', item['id']) or item['id'] in seen:
            raise ValueError('worker_failed')
        seen.add(item['id'])
        if not isinstance(item['title'], str) or not 1 <= len(item['title']) <= 300 or not safe_news_url(item['url']) or len(item['url'].encode('utf-8')) > 2048:
            raise ValueError('worker_failed')
        for field, cap in (('published_raw', 128), ('summary', 600)):
            if item[field] is not None and (not isinstance(item[field], str) or len(item[field]) > cap):
                raise ValueError('worker_failed')
        for field in ('published_at', 'updated_at', 'submitted_at', 'fetched_at'):
            value = item[field]
            if field == 'fetched_at' and value is None:
                raise ValueError('worker_failed')
            if value is not None:
                if not isinstance(value, str) or len(value) > 64 or datetime.fromisoformat(value).tzinfo is None:
                    raise ValueError('worker_failed')
        if item['date_reason'] not in (None, 'missing', 'invalid', 'timezone_unknown') or item['summary_kind'] not in (None, 'rss_description', 'atom_summary') or item['summary_reason'] not in (None, 'missing', 'empty', 'summary_too_long', 'unsupported_type'):
            raise ValueError('worker_failed')
        if item['content_origin'] != ('hn_submission' if stream == 'hn' else 'publisher_feed') or stream == 'hn' and item['published_at'] is not None:
            raise ValueError('worker_failed')
        if any(type(item[field]) is not bool for field in ('summary_truncated', 'title_truncated')):
            raise ValueError('worker_failed')


class OwnedBatch:
    def __init__(self, stream, token, deadline, stop, *, api=None, clock=time.monotonic):
        self.stream, self.token, self.deadline, self.stop = stream, token, deadline, stop
        self.api, self.clock = api, clock
        self.job = self.process = self.thread = self.stdout = self.parent_in = None
        self.child_handles = ()
        self.handles = []
        self.assigned = False
        self.pid = None
        self.frames = Frames(stream, token)

    def run(self):
        reason = None
        try:
            self.api = self.api or NewsWindowsAPI()
            self.api.create_job(self)
            self.api.limits(self.job)
            self.api.pipes(self)
            self.api.spawn(self, self.stream, self.token)
            self.api.assign(self.job, self.process)
            self.assigned = True
            if self.stop.is_set() or self.clock() >= self.deadline:
                raise TimeoutError()
            # Release child pipe duplicates before reading, so EOF is observable.
            for handle in (*self.child_handles, self.parent_in):
                if handle is None:
                    continue
                self.api.close(handle)
                self.handles.remove(handle)
            self.api.resume(self.thread)
            while True:
                if self.stop.is_set() or self.clock() >= self.deadline:
                    raise TimeoutError()
                data = self.api.read(self.stdout)
                if data:
                    self.frames.feed(data)
                elif data == b'':
                    break
                else:
                    self.stop.wait(.01)
            for handle in (self.process, self.thread):
                while not self.api.signaled(handle):
                    if self.stop.is_set() or self.clock() >= self.deadline:
                        raise TimeoutError()
                    self.stop.wait(.01)
            # The venv launcher handle alone cannot prove its interpreter tree exited.
            while self.api.active(self.job):
                if self.stop.is_set() or self.clock() >= self.deadline:
                    raise TimeoutError()
                self.stop.wait(.01)
            if self.frames.pending or not self.frames.done or self.api.exit_code(self.process) != 0:
                reason = 'worker_failed'
        except TimeoutError:
            reason = 'timeout'
        except (ProcessError, OSError, ValueError, UnicodeError, TypeError, KeyError):
            reason = 'platform_unsupported' if self.api is None else 'worker_failed'
        finally:
            cleaned = self.cleanup()
        return self.frames.frames, reason, cleaned

    def cleanup(self):
        if self.api is None:
            return True
        deadline = self.clock() + 2
        try:
            if self.process:
                if self.assigned:
                    if self.api.active(self.job):
                        self.api.terminate_job(self.job)
                else:
                    self.api.terminate_process(self.process)
                for handle in (self.process, self.thread):
                    if not self.api.signaled(handle, max(0, deadline - self.clock())):
                        return False
                while self.assigned and self.api.active(self.job) != 0:
                    if self.clock() >= deadline:
                        return False
                    time.sleep(.01)
            # No resource is closed until all required process terminal facts are proven.
            for name in ('process', 'thread'):
                handle = getattr(self, name)
                if handle:
                    self.api.close(handle)
                    setattr(self, name, None)
            for handle in list(self.handles):
                self.api.close(handle)
                self.handles.remove(handle)
            if self.job:
                self.api.close(self.job)
                self.job = None
            return True
        except (OSError, ProcessError):
            return False


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 3 or argv[0] != '1' or argv[1] not in ('ai', 'hn') or not re.fullmatch('[a-f0-9]{32}', argv[2]):
        return 2
    _, stream, token = argv
    deadline = time.monotonic() + 15
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(fetch, definition, stream, token, deadline) for definition in definitions(stream)]
        for future in as_completed(futures):
            frame = future.result()
            data = (json.dumps(frame, ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8')
            if len(data) > LINE_BYTES:
                definition = next(row for row in definitions(stream) if row[0] == frame['source_id'])
                frame = source_frame(stream, token, definition[0], None, 'oversize', definition[2], None, frame['fetched_at'])
                data = (json.dumps(frame, ensure_ascii=False) + '\n').encode('utf-8')
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
    print(json.dumps({'schema': 1, 'type': 'done', 'stream': stream, 'token': token}), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
