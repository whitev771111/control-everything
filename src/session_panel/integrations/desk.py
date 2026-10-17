"""Today's local activity and a short headline list. Missing sources stay empty."""

import os
import time
from pathlib import Path
from xml.etree import ElementTree

HOME = Path.home()
PULSE_ROOTS = (
    ("Grok", HOME / ".grok" / "sessions"),
    ("Codex", HOME / ".codex" / "sessions"),
    ("Claude", HOME / ".claude" / "projects"),
)
FEEDS = ("https://hnrss.org/frontpage?count=5",)
from session_panel.workspace.news_source import NewsSource


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
    source = NewsSource()
    value = source.refresh('hn') if force else source.cached('hn')
    return [{'title': row['title'], 'url': row['url']} for row in value['items'][:4]]
