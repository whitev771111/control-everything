"""Explicit Codex quota reads and a fixed, bounded public RSS catalog."""

import copy
import hashlib
import ipaddress
import json
import math
import queue
import re
import shutil
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

MAX_RPC_LINE = 256 * 1024
MAX_FEED_BYTES = 1024 * 1024
FEEDS = (("google_ai", "Google AI", "https://blog.google/technology/ai/rss/"),
         ("openai", "OpenAI News", "https://openai.com/news/rss.xml"),
         ("huggingface", "Hugging Face Blog", "https://huggingface.co/blog/feed.xml"))


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def invalid_constant(_value):
    raise ValueError("invalid_response")


class ProbeError(Exception):
    def __init__(self, reason, code=None):
        super().__init__(reason)
        self.reason = reason
        self.code = (code if type(code) is int or isinstance(code, str)
                     and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", code) else None)


def native_codex():
    direct = shutil.which("codex.exe")
    if direct and Path(direct).suffix.lower() == ".exe":
        return Path(direct)
    shim = shutil.which("codex")
    if not shim:
        raise ProbeError("cli_not_found")
    if Path(shim).suffix.lower() == ".exe":
        return Path(shim)
    candidate = (Path(shim).parent / "node_modules/@openai/codex/node_modules/@openai/"
                 "codex-win32-x64/vendor/x86_64-pc-windows-msvc/codex/codex.exe")
    if not candidate.is_file():
        raise ProbeError("native_cli_not_found")
    return candidate


def normalize_window(value):
    if not isinstance(value, dict):
        return None
    used, minutes, reset = (value.get(key) for key in ("usedPercent", "windowDurationMins", "resetsAt"))
    try:
        valid_used = type(used) in (int, float) and math.isfinite(used) and 0 <= used <= 100
    except OverflowError:
        valid_used = False
    reset_at = None
    if type(reset) is int and reset >= 0:
        try:
            reset_at = datetime.fromtimestamp(reset, timezone.utc).isoformat()
        except (ValueError, OverflowError, OSError):
            pass
    return {"used_percent": used if valid_used else None,
            "window_minutes": minutes if type(minutes) is int and minutes > 0 else None,
            "resets_at": reset_at}


def normalize_limits(result):
    if not isinstance(result, dict):
        raise ProbeError("invalid_response")
    buckets = result.get("rateLimitsByLimitId")
    if buckets is None:
        legacy = result.get("rateLimits")
        if not isinstance(legacy, dict):
            raise ProbeError("invalid_response")
        buckets = {"codex": legacy}
    if not isinstance(buckets, dict) or any(not isinstance(row, dict) for row in buckets.values()):
        raise ProbeError("invalid_response")
    return [{"id": key, "primary": normalize_window(row.get("primary")),
             "secondary": normalize_window(row.get("secondary"))} for key, row in buckets.items()]


class CodexQuotaSource:
    def __init__(self):
        self._condition = threading.Condition()
        self._refreshing = False
        self._cached = {"status": "not_checked", "source": "codex_app_server", "sampled_at": None,
                        "last_success_at": None, "reason": None, "error_code": None, "buckets": []}

    def get_cached(self):
        with self._condition:
            return copy.deepcopy(self._cached)

    def refresh(self):
        with self._condition:
            if self._refreshing:
                self._condition.wait_for(lambda: not self._refreshing)
                return copy.deepcopy(self._cached)
            self._refreshing = True
        try:
            buckets = self._probe()
            now = timestamp()
            result = {"status": "available", "source": "codex_app_server", "sampled_at": now,
                      "last_success_at": now, "reason": None, "error_code": None, "buckets": buckets}
        except (ProbeError, OSError, ValueError, subprocess.SubprocessError) as error:
            previous = self.get_cached()
            result = {**previous, "status": "stale" if previous["last_success_at"] else "unavailable",
                      "sampled_at": timestamp(), "reason": error.reason if isinstance(error, ProbeError) else "process_failed",
                      "error_code": error.code if isinstance(error, ProbeError) else None}
        finally:
            with self._condition:
                # Unexpected errors still release waiters; callers receive the original error.
                if "result" in locals():
                    self._cached = result
                self._refreshing = False
                self._condition.notify_all()
        return self.get_cached()

    def _probe(self):
        executable = native_codex()
        deadline = time.monotonic() + 18
        process = subprocess.Popen([str(executable), "app-server", "--listen", "stdio://"],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        events, stop = queue.Queue(maxsize=16), threading.Event()

        def put(value):
            while not stop.is_set():
                try:
                    events.put(value, timeout=0.05)
                    return
                except queue.Full:
                    pass

        def collect():
            try:
                while not stop.is_set():
                    line = process.stdout.readline(MAX_RPC_LINE + 1)
                    if not line:
                        put(ProbeError("process_failed"))
                        return
                    if len(line) > MAX_RPC_LINE:
                        put(ProbeError("invalid_response"))
                        return
                    try:
                        value = json.loads(line, parse_constant=invalid_constant)
                    except (ValueError, UnicodeDecodeError):
                        put(ProbeError("invalid_response"))
                        return
                    if not isinstance(value, dict):
                        put(ProbeError("invalid_response"))
                        return
                    put(value)
            except OSError:
                put(ProbeError("process_failed"))

        reader = threading.Thread(target=collect, daemon=True)
        reader.start()

        def send(value):
            if time.monotonic() >= deadline:
                raise ProbeError("probe_timeout")
            process.stdin.write((json.dumps(value) + "\n").encode("utf-8"))
            process.stdin.flush()

        def receive(request_id):
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ProbeError("probe_timeout")
                try:
                    value = events.get(timeout=remaining)
                except queue.Empty:
                    raise ProbeError("probe_timeout") from None
                if isinstance(value, ProbeError):
                    raise value
                if type(value.get("id")) is int and value["id"] == request_id:
                    return value

        try:
            send({"id": 1, "method": "initialize", "params": {
                "clientInfo": {"name": "mianban_workspace", "version": "1.0"}}})
            initialized = receive(1)
            if "error" in initialized:
                error = initialized["error"]
                raise ProbeError("initialize_failed", error.get("code") if isinstance(error, dict) else None)
            if not isinstance(initialized.get("result"), dict):
                raise ProbeError("initialize_failed")
            send({"method": "initialized"})
            send({"id": 2, "method": "account/rateLimits/read"})
            response = receive(2)
            if "error" in response:
                error = response["error"]
                raise ProbeError("rpc_error", error.get("code") if isinstance(error, dict) else None)
            return normalize_limits(response.get("result"))
        finally:
            stop.set()
            try:
                process.stdin.close()
            except OSError:
                pass
            try:
                process.wait(timeout=min(2, max(0, deadline - time.monotonic())))
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
            finally:
                process.stdout.close()
                reader.join(timeout=1)


def safe_news_url(value):
    if not value or any(ord(char) < 33 for char in value) or "\\" in value:
        return False
    try:
        url = urlsplit(value)
        if url.scheme != "https" or not url.hostname or url.username or url.password:
            return False
        if url.hostname.lower() == "localhost" or url.hostname.lower().endswith((".localhost", ".local")):
            return False
        try:
            return ipaddress.ip_address(url.hostname).is_global
        except ValueError:
            return "." in url.hostname
    except ValueError:
        return False


def parse_feed(data, source_id, source_name, fetched_at):
    if b"<!DOCTYPE" in data.upper():
        raise ValueError("invalid_feed")
    root = ET.fromstring(data)
    channel = root.find("channel") if root.tag == "rss" else None
    if channel is None:
        raise ValueError("invalid_feed")
    items, seen = [], set()
    for row in channel.findall("item"):
        title, url = ("".join(row.find(name).itertext()).strip() if row.find(name) is not None else ""
                      for name in ("title", "link"))
        if not title or not safe_news_url(url) or url in seen:
            continue
        seen.add(url)
        published_at = None
        try:
            published = parsedate_to_datetime(row.findtext("pubDate", ""))
            if published.tzinfo is not None:
                published_at = published.astimezone(timezone.utc).isoformat()
        except (ValueError, TypeError, OverflowError):
            pass
        items.append({"id": hashlib.sha256((source_id + ":" + url).encode()).hexdigest()[:24],
                      "source_id": source_id, "source_name": source_name, "title": title,
                      "url": url, "published_at": published_at, "fetched_at": fetched_at})
    return sorted(items, key=lambda row: row["published_at"] or "", reverse=True)[:5]


class NewsSource:
    def __init__(self):
        self._locks = {source_id: threading.Lock() for source_id, _, _ in FEEDS}
        self._cache = {}

    def _get_feed(self, definition):
        source_id, name, url = definition
        with self._locks[source_id]:
            cached = self._cache.get(source_id)
            if cached and time.monotonic() - cached["checked"] < 900:
                return copy.deepcopy(cached)
            now, reason = timestamp(), None
            try:
                request = Request(url, headers={"User-Agent": "MianbanWorkspace/1.0"})
                with urlopen(request, timeout=10) as response:
                    data = response.read(MAX_FEED_BYTES + 1)
                if len(data) > MAX_FEED_BYTES:
                    reason = "oversize"
                    raise ValueError("oversize")
                items = parse_feed(data, source_id, name, now)
                source = {"id": source_id, "name": name, "status": "available", "reason": None,
                          "sampled_at": now, "last_success_at": now, "item_count": len(items)}
            except (HTTPError, URLError, TimeoutError, OSError, ValueError, ET.ParseError) as error:
                if reason is None:
                    reason = ("timeout" if isinstance(error, TimeoutError) or
                              isinstance(error, URLError) and isinstance(error.reason, TimeoutError) else
                              "invalid_feed" if isinstance(error, (ValueError, ET.ParseError)) else "http_error")
                items = cached["items"] if cached else []
                last_success = cached["source"]["last_success_at"] if cached else None
                source = {"id": source_id, "name": name, "status": "stale" if last_success else "unavailable",
                          "reason": reason, "sampled_at": now, "last_success_at": last_success,
                          "item_count": len(items)}
            result = {"source": source, "items": items, "checked": time.monotonic()}
            self._cache[source_id] = result
            return copy.deepcopy(result)

    def get(self):
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(self._get_feed, FEEDS))
        sources = [row["source"] for row in results]
        items = sorted([item for row in results for item in row["items"]],
                       key=lambda row: row["published_at"] or "", reverse=True)[:15]
        status = ("available" if all(row["status"] == "available" for row in sources) else
                  "stale" if items or any(row["status"] == "available" for row in sources) else "unavailable")
        return {"status": status, "sampled_at": timestamp(), "items": items, "sources": sources}
