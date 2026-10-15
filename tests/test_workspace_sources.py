import copy
import io
import json
import queue
import subprocess
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from session_panel.workspace_sources import (CodexQuotaSource, FEEDS, MAX_FEED_BYTES, MAX_RPC_LINE,
                                             NewsSource, ProbeError, native_codex, normalize_limits,
                                             parse_feed, safe_news_url)

RESULT = {"rateLimits": {"primary": {"usedPercent": 36, "windowDurationMins": 10080, "resetsAt": 1791907200},
                         "secondary": None}}


class CaptureInput(io.BytesIO):
    def close(self):
        self.saved = self.getvalue()
        super().close()


class FakeProcess:
    def __init__(self, messages=(), raw=None, hang_on_exit=False):
        self.stdin = CaptureInput()
        self.stdout = io.BytesIO(raw if raw is not None else b"".join(json.dumps(message).encode() + b"\n" for message in messages))
        self.wait = Mock(side_effect=[subprocess.TimeoutExpired("fixture", 2), 0] if hang_on_exit else None, return_value=0)
        self.kill = Mock()


class QuotaSourceTests(unittest.TestCase):
    def source_with_process(self, messages=(), **kwargs):
        process = FakeProcess(messages, **kwargs)
        source = CodexQuotaSource()
        with patch("session_panel.workspace_sources.native_codex", return_value=Path("C:/fixture/codex.exe")), \
                patch("session_panel.workspace_sources.subprocess.Popen", return_value=process) as spawn:
            result = source.refresh()
        return source, process, result, spawn

    def test_get_does_not_spawn_and_uses_copy(self):
        with patch("session_panel.workspace_sources.subprocess.Popen") as spawn:
            source = CodexQuotaSource()
            first = source.get_cached()
            first["buckets"].append({})
            self.assertEqual(source.get_cached()["status"], "not_checked")
            self.assertEqual(source.get_cached()["buckets"], [])
            spawn.assert_not_called()

    def test_native_resolver_uses_executable_or_only_verified_npm_layout(self):
        with patch("session_panel.workspace_sources.shutil.which", return_value=None):
            with self.assertRaisesRegex(ProbeError, "cli_not_found"):
                native_codex()
        with patch("session_panel.workspace_sources.shutil.which", side_effect=["C:/fixture/codex.exe"]):
            self.assertEqual(native_codex(), Path("C:/fixture/codex.exe"))
        with TemporaryDirectory() as directory:
            shim = Path(directory) / "codex.cmd"
            native = Path(directory) / "node_modules/@openai/codex/node_modules/@openai/codex-win32-x64/vendor/x86_64-pc-windows-msvc/codex/codex.exe"
            with patch("session_panel.workspace_sources.shutil.which", side_effect=lambda name: None if name == "codex.exe" else str(shim)):
                with self.assertRaisesRegex(ProbeError, "native_cli_not_found"):
                    native_codex()
                native.parent.mkdir(parents=True)
                native.write_bytes(b"fixture only; never execute")
                self.assertEqual(native_codex(), native)

    def test_protocol_whitelist_success_and_owned_cleanup(self):
        _, process, result, spawn = self.source_with_process([
            {"method": "notification"}, {"id": 999, "result": {}},
            {"id": 1, "result": {"userAgent": "fixture"}}, {"id": 2, "result": RESULT}])
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["buckets"][0]["primary"]["used_percent"], 36)
        self.assertIsNone(result["buckets"][0]["secondary"])
        sent = [json.loads(line) for line in process.stdin.saved.splitlines()]
        self.assertEqual([item["method"] for item in sent], ["initialize", "initialized", "account/rateLimits/read"])
        self.assertEqual(spawn.call_args.args[0][1:], ["app-server", "--listen", "stdio://"])
        self.assertEqual(spawn.call_args.kwargs["stderr"], subprocess.DEVNULL)
        self.assertNotIn("shell", spawn.call_args.kwargs)
        process.wait.assert_called_once()
        process.kill.assert_not_called()
        self.assertTrue(process.stdin.closed and process.stdout.closed)

    def test_initialize_error_never_sends_read_and_does_not_leak_message(self):
        _, process, result, _ = self.source_with_process([{"id": 1, "error": {"code": -123, "message": "private auth token"}}])
        self.assertEqual((result["status"], result["reason"], result["error_code"]), ("unavailable", "initialize_failed", -123))
        self.assertEqual([json.loads(line)["method"] for line in process.stdin.saved.splitlines()], ["initialize"])
        self.assertNotIn("private", json.dumps(result))
        self.assertTrue(process.stdout.closed)

    def test_rpc_error_eof_invalid_json_and_oversized_line_cleanup(self):
        cases = [([{"id": 1, "result": {}}, {"id": 2, "error": {"code": -32603, "message": "private"}}], None, "rpc_error"),
                 ([], None, "process_failed"),
                 ([], b"not-json\n", "invalid_response"),
                 ([], b'{"id":1,"result":NaN}\n', "invalid_response"),
                 ([], b"x" * (MAX_RPC_LINE + 1), "invalid_response"),
                 ([{"id": 1, "result": {}}, {"id": 2, "result": None}], None, "invalid_response")]
        for messages, raw, reason in cases:
            with self.subTest(reason=reason):
                _, process, result, _ = self.source_with_process(messages, raw=raw, hang_on_exit=True)
                self.assertEqual((result["status"], result["reason"]), ("unavailable", reason))
                process.kill.assert_called_once_with()
                self.assertEqual(process.wait.call_count, 2)
                self.assertTrue(process.stdout.closed)
                self.assertNotIn("private", json.dumps(result))

    def test_timeout_uses_one_deadline_and_reclaims_owned_process(self):
        events = Mock()
        events.get.side_effect = queue.Empty
        with patch("session_panel.workspace_sources.queue.Queue", return_value=events):
            _, process, result, _ = self.source_with_process([], hang_on_exit=True)
        self.assertEqual(result["reason"], "probe_timeout")
        self.assertLessEqual(events.get.call_args.kwargs["timeout"], 18)
        process.kill.assert_called_once_with()
        self.assertTrue(process.stdout.closed)

    def test_success_then_failure_preserves_last_success_and_buckets(self):
        source = CodexQuotaSource()
        with patch.object(source, "_probe", return_value=normalize_limits(RESULT)):
            success = source.refresh()
        with patch.object(source, "_probe", side_effect=ProbeError("rpc_error", -32603)):
            stale = source.refresh()
        self.assertEqual(stale["status"], "stale")
        self.assertEqual(stale["buckets"], success["buckets"])
        self.assertEqual(stale["last_success_at"], success["last_success_at"])
        self.assertEqual(stale["error_code"], -32603)

    def test_refresh_merges_concurrent_calls(self):
        source, entered, release = CodexQuotaSource(), threading.Event(), threading.Event()
        def probe():
            entered.set()
            self.assertTrue(release.wait(2))
            return normalize_limits(RESULT)
        with patch.object(source, "_probe", side_effect=probe) as mocked, ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(source.refresh)
            self.assertTrue(entered.wait(2))
            second = pool.submit(source.refresh)
            time.sleep(0.03)
            release.set()
            self.assertEqual(first.result(), second.result())
            mocked.assert_called_once_with()

    def test_multi_bucket_precedence_and_invalid_values_stay_null(self):
        result = normalize_limits({**RESULT, "rateLimitsByLimitId": {
            "codex": {"primary": {"usedPercent": 0, "windowDurationMins": 300, "resetsAt": None}},
            "review": {"primary": {"usedPercent": True, "windowDurationMins": True, "resetsAt": -1},
                       "secondary": {"usedPercent": 101}}}})
        self.assertEqual([row["id"] for row in result], ["codex", "review"])
        self.assertEqual(result[0]["primary"]["used_percent"], 0)
        self.assertEqual(result[1]["primary"], {"used_percent": None, "window_minutes": None, "resets_at": None})
        self.assertIsNone(result[1]["secondary"]["used_percent"])
        for value in ({}, {"rateLimitsByLimitId": []}, {"rateLimitsByLimitId": {"bad": None}}):
            with self.assertRaisesRegex(ProbeError, "invalid_response"):
                normalize_limits(value)


def rss(items):
    return ("<rss><channel>" + "".join(items) + "</channel></rss>").encode()


def item(url="https://example.com/story", title="AI news", day="Tue, 13 Oct 2026 10:00:00 GMT"):
    return f"<item><title><![CDATA[{title}]]></title><link>{url}</link><pubDate>{day}</pubDate></item>"


class NewsSourceTests(unittest.TestCase):
    def test_parse_cdata_skip_unsafe_deduplicate_sort_and_cap(self):
        data = rss([item(title="<b>plain title</b>"), item(), item(url="http://example.com/insecure"),
                    item(url="https://localhost/secret"), item(url="https://example.com/unknown", day="unknown")] +
                   [item(url=f"https://example.com/{index}", day=f"Tue, 13 Oct 2026 {index:02d}:00:00 GMT") for index in range(8)])
        rows = parse_feed(data, "openai", "OpenAI", "2026-10-13T10:00:00+00:00")
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[0]["title"], "<b>plain title</b>")
        self.assertEqual(len({row["url"] for row in rows}), 5)
        self.assertEqual(set(rows[0]), {"id", "source_id", "source_name", "title", "url", "published_at", "fetched_at"})
        unknown = parse_feed(rss([item(day="invalid")]), "openai", "OpenAI", "now")
        self.assertIsNone(unknown[0]["published_at"])

    def test_unsafe_links_and_invalid_xml(self):
        for url in ("javascript:alert(1)", "https://127.0.0.1/", "https://[::1]/", "https://user:secret@example.com/",
                    "https://example.com/\n", "https://example.com\\@localhost", "http://example.com/"):
            self.assertFalse(safe_news_url(url), url)
        for data in (b"<html></html>", b"<rss>", b'<!DOCTYPE rss [<!ENTITY test "value">]><rss><channel/></rss>'):
            with self.assertRaises(ValueError if b"<!DOCTYPE" in data or b"html" in data else Exception):
                parse_feed(data, "openai", "OpenAI", "now")

    def test_fixed_sources_timeout_limit_and_cache(self):
        responses = []
        def opener(request, timeout):
            self.assertEqual(timeout, 10)
            self.assertIn(request.full_url, [row[2] for row in FEEDS])
            self.assertEqual(request.get_header("User-agent"), "MianbanWorkspace/1.0")
            response = Mock()
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock(return_value=False)
            response.read.return_value = rss([item()])
            responses.append(response)
            return response
        source = NewsSource()
        with patch("session_panel.workspace_sources.urlopen", side_effect=opener) as open_mock:
            first = source.get()
            second = source.get()
        self.assertEqual((first["status"], len(first["items"])), ("available", 3))
        self.assertEqual(first["items"], second["items"])
        self.assertEqual(open_mock.call_count, 3)
        for response in responses:
            response.read.assert_called_once_with(MAX_FEED_BYTES + 1)

    def test_partial_failure_preserves_old_feed_and_reports_attempt(self):
        source = NewsSource()
        with patch("session_panel.workspace_sources.urlopen", side_effect=lambda *args, **kwargs: io.BytesIO(rss([item()]))):
            old = source.get()
        old_times = {row["id"]: row["last_success_at"] for row in old["sources"]}
        for entry in source._cache.values():
            entry["checked"] -= 901
        def opener(request, timeout):
            if request.full_url == FEEDS[0][2]:
                raise TimeoutError("private network detail")
            return io.BytesIO(rss([item(url="https://example.com/new")]))
        with patch("session_panel.workspace_sources.urlopen", side_effect=opener):
            result = source.get()
        self.assertEqual(result["status"], "stale")
        first = result["sources"][0]
        self.assertEqual((first["status"], first["reason"], first["last_success_at"]), ("stale", "timeout", old_times[first["id"]]))
        self.assertNotIn("private", json.dumps(result))
        self.assertIn("https://example.com/story", [row["url"] for row in result["items"]])

    def test_feed_errors_are_explicit_and_empty_feeds_are_available(self):
        failures = [(TimeoutError(), "timeout"), (URLError(TimeoutError()), "timeout"),
                    (HTTPError("https://example.com", 500, "private", {}, None), "http_error"),
                    (b"x" * (MAX_FEED_BYTES + 1), "oversize"), (b"invalid", "invalid_feed")]
        for failure, reason in failures:
            with self.subTest(reason=reason):
                effect = failure if isinstance(failure, Exception) else lambda *args, **kwargs: io.BytesIO(failure)
                with patch("session_panel.workspace_sources.urlopen", side_effect=effect):
                    result = NewsSource().get()
                self.assertEqual(result["status"], "unavailable")
                self.assertEqual({row["reason"] for row in result["sources"]}, {reason})
        with patch("session_panel.workspace_sources.urlopen", side_effect=lambda *args, **kwargs: io.BytesIO(rss([]))):
            result = NewsSource().get()
        self.assertEqual((result["status"], result["items"]), ("available", []))

    def test_same_source_concurrent_requests_share_refresh(self):
        source = NewsSource()
        def opener(*args, **kwargs):
            time.sleep(0.02)
            return io.BytesIO(rss([item()]))
        with patch("session_panel.workspace_sources.urlopen", side_effect=opener) as mock_open, ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: source.get(), range(2)))
        self.assertEqual(mock_open.call_count, 3)
        self.assertEqual(results[0]["items"], results[1]["items"])


if __name__ == "__main__":
    unittest.main()
