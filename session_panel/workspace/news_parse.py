"""Bounded, offline projections of RSS 2, Atom 1 and HN submission facts."""

import hashlib
import ipaddress
import json
import math
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from io import BytesIO
from urllib.parse import urljoin, urlsplit
from xml.etree.ElementTree import ParseError

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

MAX_FEED_BYTES = 1024 * 1024
ATOM = '{http://www.w3.org/2005/Atom}'
XHTML = '{http://www.w3.org/1999/xhtml}'
XML_BASE = '{http://www.w3.org/XML/1998/namespace}base'
RFC3339 = r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?'
RSS_DATE = r'(?:[A-Za-z]{3},\s*)?\d{1,2} [A-Za-z]{3} \d{4} \d{2}:\d{2}:\d{2}'


def safe_news_url(value):
    if not isinstance(value, str) or not value or any(ord(char) < 33 for char in value) or '\\' in value:
        return False
    try:
        url = urlsplit(value)
        if url.scheme != 'https' or not url.hostname or url.username or url.password:
            return False
        if url.hostname.lower() == 'localhost' or url.hostname.lower().endswith(('.localhost', '.local')):
            return False
        try:
            return ipaddress.ip_address(url.hostname).is_global
        except ValueError:
            return '.' in url.hostname
    except ValueError:
        return False


def _date(value, atom=False):
    raw = value.strip() if isinstance(value, str) else ''
    if not raw:
        return None, None, 'missing'
    if len(raw) > 128:
        return None, None, 'invalid'
    pattern = RFC3339 if atom else RSS_DATE
    zone = r'(Z|[+-]\d{2}:\d{2})' if atom else r' (GMT|UTC|UT|[+-]\d{4})'
    offset = re.search(r'[+-](\d{2}):?(\d{2})$', raw)
    if offset and (int(offset[1]) > 23 or int(offset[2]) > 59):
        return None, raw, 'invalid'
    if not re.fullmatch(pattern + zone, raw) or raw.endswith('-0000'):
        without_zone = re.fullmatch(pattern + (r'(?: ?(?:[A-Za-z]+|[+-]\d{2}:?\d{2}))?' if not atom else ''), raw)
        if without_zone:
            # A missing/unsupported zone does not make an impossible calendar date valid.
            try:
                if atom:
                    datetime.fromisoformat(raw)
                else:
                    parsedate_to_datetime(raw)
            except (ValueError, TypeError, OverflowError):
                without_zone = None
        return None, raw, 'timezone_unknown' if without_zone else 'invalid'
    try:
        value = datetime.fromisoformat(raw.replace('Z', '+00:00')) if atom else parsedate_to_datetime(raw)
        return value.astimezone(timezone.utc).isoformat(), raw, None
    except (ValueError, TypeError, OverflowError):
        return None, raw, 'invalid'


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden += 1
        elif not self.hidden:
            self.parts.append(' ')

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.hidden = max(0, self.hidden - 1)
        elif not self.hidden:
            self.parts.append(' ')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _text(element):
    return ''.join(element.itertext()).strip() if element is not None else ''


def _xhtml_text(element):
    if not element.tag.startswith(XHTML) or element.tag in (XHTML + 'script', XHTML + 'style'):
        return ''
    parts = [element.text or '']
    for child in element:
        parts.extend((_xhtml_text(child), child.tail or ''))
    return ' '.join(parts)


def _summary(element, atom):
    kind = 'atom_summary' if atom else 'rss_description'
    if element is None:
        return None, None, False, 'missing'
    raw = _text(element)
    if len(raw) > 16384:
        return None, kind, False, 'summary_too_long'
    content_type = element.get('type', 'text') if atom else 'html'
    if content_type == 'xhtml':
        children = list(element)
        if len(children) != 1 or children[0].tag != XHTML + 'div':
            return None, kind, False, 'unsupported_type'
        if len(ElementTree.tostring(children[0], encoding='unicode')) > 16384:
            return None, kind, False, 'summary_too_long'
        text = _xhtml_text(children[0])
    elif content_type == 'html':
        parser = _Text()
        parser.feed(raw)
        parser.close()
        text = ''.join(parser.parts)
    elif content_type == 'text':
        text = raw
    else:
        return None, kind, False, 'unsupported_type'
    text = ' '.join(text.split())
    return text[:600] or None, kind, len(text) > 600, None if text else 'empty'


def _tree(data):
    if len(data) > MAX_FEED_BYTES:
        raise ValueError('oversize')
    depth = nodes = 0
    root = None
    try:
        for event, element in ElementTree.iterparse(BytesIO(data), events=('start', 'end'),
                                                  forbid_dtd=True, forbid_entities=True, forbid_external=True):
            if event == 'start':
                depth += 1
                nodes += 1
                if depth > 64 or nodes > 50000:
                    raise ValueError('parse_budget')
                if root is None:
                    root = element
            else:
                depth -= 1
    except (ParseError, DefusedXmlException):
        raise ValueError('invalid_feed') from None
    return root


def _url(value, base):
    if not value or any(ord(char) < 33 for char in value) or '\\' in value:
        return None
    try:
        resolved = urljoin(base or '', value)
        return resolved if safe_news_url(resolved) and len(resolved.encode('utf-8')) <= 2048 else None
    except (ValueError, UnicodeError):
        return None


def _base(parent, element):
    return urljoin(parent or '', element.get(XML_BASE, ''))


def _item(source_id, source_name, title, url, fetched_at):
    return {'id': hashlib.sha256((source_id + ':' + url).encode('utf-8')).hexdigest()[:24],
            'source_id': source_id, 'source_name': source_name, 'title': title[:300], 'url': url,
            'published_at': None, 'fetched_at': fetched_at, 'published_raw': None, 'date_reason': 'missing',
            'updated_at': None, 'submitted_at': None, 'content_origin': 'publisher_feed',
            'summary': None, 'summary_kind': None, 'summary_truncated': False, 'summary_reason': 'missing',
            'title_truncated': len(title) > 300}


def _result(rows, scanned, filtered, duplicates, format_name, now, cap=20):
    now = now or datetime.now(timezone.utc)

    def order(row):
        published = datetime.fromisoformat(row['published_at']) if row['published_at'] else None
        return (0, -published.timestamp(), row['id']) if published and published <= now else (
            (2, -published.timestamp(), row['id']) if published else (1, 0, row['id']))

    rows.sort(key=order)
    return {'format': format_name, 'items': rows[:cap], 'reason': 'filtered_all' if scanned and not rows else None,
            'scanned_count': scanned, 'valid_count': len(rows), 'filtered_count': filtered,
            'duplicate_count': duplicates, 'returned_count': min(len(rows), cap), 'truncated': len(rows) > cap}


def parse_feed_result(data, source_id, source_name, fetched_at, *, feed_url=None, now=None):
    root = _tree(data)
    atom = root.tag == ATOM + 'feed'
    base = _base(feed_url, root)
    if atom:
        entries = root.findall(ATOM + 'entry')
        prefix = ATOM
    elif root.tag == 'rss' and root.get('version') == '2.0':
        channels = root.findall('channel')
        if len(channels) != 1:
            raise ValueError('invalid_feed')
        entries = channels[0].findall('item')
        base = _base(base, channels[0])
        prefix = ''
    else:
        raise ValueError('unsupported_format')
    if len(entries) > 2000:
        raise ValueError('parse_budget')
    rows, seen, filtered, duplicates = [], set(), 0, 0
    for element in entries:
        title = _text(element.find(prefix + 'title'))
        entry_base = _base(base, element)
        links = element.findall(prefix + 'link')
        if atom:
            links = [link for link in links if link.get('rel', 'alternate') == 'alternate'
                     and link.get('type') in (None, 'text/html')]
            links.sort(key=lambda link: link.get('type') != 'text/html')
        url = None
        for link in links:
            url = _url(link.get('href', '') if atom else _text(link), _base(entry_base, link))
            if url:
                break
        if not title or not url:
            filtered += 1
            continue
        if url in seen:
            duplicates += 1
            continue
        seen.add(url)
        row = _item(source_id, source_name, title, url, fetched_at)
        row['published_at'], row['published_raw'], row['date_reason'] = _date(
            _text(element.find(prefix + ('published' if atom else 'pubDate'))), atom)
        if atom:
            row['updated_at'] = _date(_text(element.find(ATOM + 'updated')), True)[0]
        row['summary'], row['summary_kind'], row['summary_truncated'], row['summary_reason'] = _summary(
            element.find(prefix + ('summary' if atom else 'description')), atom)
        rows.append(row)
    return _result(rows, len(entries), filtered, duplicates, 'atom1' if atom else 'rss2', now)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('invalid_feed')
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError('invalid_feed')


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError('invalid_feed')
    return number


def parse_hn_result(data, fetched_at, *, now=None):
    if len(data) > MAX_FEED_BYTES:
        raise ValueError('oversize')
    try:
        document = json.loads(data.decode('utf-8'), object_pairs_hook=_unique_object,
                              parse_constant=_invalid_constant, parse_float=_finite_float)
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError('invalid_feed') from None
    if not isinstance(document, dict) or not isinstance(document.get('hits'), list):
        raise ValueError('invalid_feed')
    hits = document['hits']
    if len(hits) > 4:
        raise ValueError('parse_budget')
    rows, seen, filtered, duplicates = [], set(), 0, 0
    for hit in hits:
        if not isinstance(hit, dict):
            filtered += 1
            continue
        title = hit.get('title')
        title = title.strip() if isinstance(title, str) else ''
        url = _url(hit.get('url'), None) if isinstance(hit.get('url'), str) else None
        identifier = hit.get('objectID')
        if not url and isinstance(identifier, str) and re.fullmatch(r'[0-9]{1,20}', identifier):
            url = 'https://news.ycombinator.com/item?id=' + identifier
        if not title or not url:
            filtered += 1
            continue
        if url in seen:
            duplicates += 1
            continue
        seen.add(url)
        row = _item('hn', 'Hacker News', title, url, fetched_at)
        row['content_origin'] = 'hn_submission'
        submitted = _date(hit.get('created_at'), True)[0]
        if submitted and 'created_at_i' in hit:
            epoch = hit['created_at_i']
            if type(epoch) is not int or int(datetime.fromisoformat(submitted).timestamp()) != epoch:
                submitted = None
        row['submitted_at'] = submitted
        rows.append(row)
    return _result(rows, len(hits), filtered, duplicates, 'hn', now, cap=4)
