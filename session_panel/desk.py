"""Today's local activity and a short headline list. Missing sources stay empty."""

import json
import os
import time
from pathlib import Path
from urllib.request import Request, urlopen
from xml.etree import ElementTree

HOME = Path.home()
PULSE_ROOTS = (
    ("Grok", HOME / ".grok" / "sessions"),
    ("Codex", HOME / ".codex" / "sessions"),
    ("Claude", HOME / ".claude" / "projects"),
)
FEEDS = ("https://hnrss.org/frontpage?count=5",)
_NEWS = {"at": 0, "items": []}


def pulse(now=None):
    started = now if now is not None else time.time() - 24 * 60 * 60
    rows = []
    for name, root in PULSE_ROOTS:
        count = seen = 0
        ready = root.is_dir()
        stack = [root] if ready else []
        while stack and seen < 600:
            current = stack.pop()
            try:
                with os.scandir(current) as entries:
                    for entry in entries:
                        seen += 1
                        if entry.is_dir(follow_symlinks=False) and seen < 600:
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False) and entry.stat().st_mtime >= started:
                            count += 1
                        if seen >= 600:
                            break
            except OSError:
                continue
        rows.append({"name": name, "count": count, "ready": ready})
    return rows


def _titles(xml_text):
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return []
    titles = []
    for node in root.iter():
        if node.tag.endswith("title") and node.text and node.text.strip().lower() != "hacker news":
            titles.append(node.text.strip())
    return titles[1:5] if titles and titles[0].lower() in {"hacker news", "localllama"} else titles[:4]


def headlines(force=False):
    if not force and time.time() - _NEWS["at"] < 900 and _NEWS["items"]:
        return _NEWS["items"]
    found = []
    try:
        request = Request("https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage=4",
                          headers={"User-Agent": "mianban-desk"})
        with urlopen(request, timeout=4) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
        for hit in payload.get("hits", []) if isinstance(payload, dict) else []:
            title = hit.get("title") if isinstance(hit, dict) else None
            link = hit.get("url") if isinstance(hit, dict) else None
            if isinstance(title, str) and title.strip():
                found.append({"title": title.strip(), "url": link if isinstance(link, str) and link.startswith("https://") else "https://news.ycombinator.com/"})
    except Exception:
        found = []
    _NEWS["at"] = time.time()
    _NEWS["items"] = found[:4]
    return _NEWS["items"]
