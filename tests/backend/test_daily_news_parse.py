"""Pure synthetic feed contracts; never fetches a URL or starts a process."""
import hashlib
import json
import unittest
from datetime import datetime, timezone

from session_panel.workspace import news_parse as news

NOW = datetime(2026, 10, 17, 0, 0, tzinfo=timezone.utc)
FETCHED = NOW.isoformat()
BASE = 'https://example.com/feeds/current.xml'


def rss(rows):
    return ('<rss version="2.0"><channel>' + ''.join(rows) + '</channel></rss>').encode('utf-8')


def item(url='https://example.com/story', title='Synthetic AI', date='Fri, 16 Oct 2026 10:00:00 GMT', extra=''):
    return f'<item><title>{title}</title><link>{url}</link><pubDate>{date}</pubDate>{extra}</item>'


def parse(data, source_id='openai'):
    return news.parse_feed_result(data, source_id, 'Synthetic source', FETCHED, feed_url=BASE, now=NOW)


class DailyNewsParseTests(unittest.TestCase):
    def test_rss_direct_provenance_missing_summary_and_stable_id(self):
        result = parse(rss([item(extra='<media:description xmlns:media="urn:media">Not a source summary</media:description>')]))
        row = result['items'][0]
        self.assertEqual((result['format'], result['scanned_count'], result['valid_count'], result['reason']), ('rss2', 1, 1, None))
        self.assertEqual(row['id'], hashlib.sha256(b'openai:https://example.com/story').hexdigest()[:24])
        self.assertEqual((row['summary'], row['summary_reason'], row['published_at'], row['fetched_at']),
                         (None, 'missing', '2026-10-16T10:00:00+00:00', FETCHED))
        self.assertEqual(row['content_origin'], 'publisher_feed')
        self.assertIsNone(row['updated_at'])
        self.assertIsNone(row['submitted_at'])

    def test_html_summary_text_only_and_description_not_content(self):
        result = parse(rss([item(extra='<description><![CDATA[<p>Real &amp; public</p><script>hidden()</script><style>secret</style><p>next</p>]]></description>'),
                            item(url='https://example.com/missing', extra='<content>Not a summary</content>')]))
        by_url = {row['url']: row for row in result['items']}
        self.assertEqual(by_url['https://example.com/story']['summary'], 'Real & public next')
        self.assertEqual(by_url['https://example.com/story']['summary_kind'], 'rss_description')
        self.assertIsNone(by_url['https://example.com/missing']['summary'])

    def test_summary_and_title_limits_are_disclosed(self):
        result = parse(rss([item(title='中' * 301, extra='<description>' + '😀' * 601 + '</description>'),
                            item(url='https://example.com/huge', extra='<description>' + 'x' * 16385 + '</description>')]))
        rows = {row['url']: row for row in result['items']}
        row = rows['https://example.com/story']
        self.assertEqual((len(row['title']), row['title_truncated'], len(row['summary']), row['summary_truncated']), (300, True, 600, True))
        self.assertEqual((rows['https://example.com/huge']['summary'], rows['https://example.com/huge']['summary_reason']),
                         (None, 'summary_too_long'))

    def test_dates_are_strict_unknown_future_and_updated_are_separate(self):
        cases = [('', 'missing'), ('2026', 'invalid'), ('Fri, 16 Oct 2026 10:00:00', 'timezone_unknown'),
                 ('Fri, 16 Oct 2026 10:00:00 PST', 'timezone_unknown'), ('Fri, 16 Oct 2026 10:00:00 -0000', 'timezone_unknown'),
                 ('Fri, 31 Feb 2026 10:00:00 GMT', 'invalid'), ('Fri, 31 Feb 2026 10:00:00', 'invalid')]
        for raw, reason in cases:
            with self.subTest(raw=raw):
                row = parse(rss([item(date=raw)]))['items'][0]
                self.assertIsNone(row['published_at'])
                self.assertEqual(row['date_reason'], reason)
        rows = [item(url='https://example.com/future', date='Sun, 18 Oct 2026 10:00:00 +0000'),
                item(url='https://example.com/unknown', date=''), item(url='https://example.com/past')]
        self.assertEqual([row['url'].rsplit('/', 1)[1] for row in parse(rss(rows))['items']], ['past', 'unknown', 'future'])
        atom = b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Only update</title><link href="https://example.com/a"/><updated>2026-10-16T10:00:00Z</updated></entry></feed>'
        row = parse(atom)['items'][0]
        self.assertIsNone(row['published_at'])
        self.assertEqual(row['updated_at'], '2026-10-16T10:00:00+00:00')

    def test_atom_text_html_xhtml_and_base(self):
        for kind, summary, expected in [('text', '&lt;b&gt;literal&lt;/b&gt;', '<b>literal</b>'),
                                        ('html', '&lt;p&gt;HTML&lt;/p&gt;&lt;script&gt;hidden&lt;/script&gt;', 'HTML'),
                                        ('xhtml', '<div xmlns="http://www.w3.org/1999/xhtml"><p>XHTML</p><script>hidden</script></div>', 'XHTML')]:
            with self.subTest(kind=kind):
                data = f'''<feed xmlns="http://www.w3.org/2005/Atom" xml:base="https://example.com/root/">
                  <entry xml:base="nested/"><title>Atom</title><link rel="self" href="ignored"/>
                  <link href="plain"/><link type="text/html" href="chosen"/>
                  <published>2026-10-16T12:30:00+02:30</published><summary type="{kind}">{summary}</summary></entry></feed>'''.encode()
                result = parse(data)
                row = result['items'][0]
                self.assertEqual((result['format'], row['url'], row['summary'], row['summary_kind']),
                                 ('atom1', 'https://example.com/root/nested/chosen', expected, 'atom_summary'))
                self.assertEqual(row['published_at'], '2026-10-16T10:00:00+00:00')

    def test_relative_rss_link_and_duplicate_counts_and_cross_source_identity(self):
        data = rss([item(url='../story'), item(url='../story'), item(url='http://example.com/unsafe')])
        result = parse(data)
        self.assertEqual((result['scanned_count'], result['valid_count'], result['filtered_count'], result['duplicate_count']), (3, 1, 1, 1))
        self.assertEqual(result['items'][0]['url'], 'https://example.com/story')
        self.assertNotEqual(result['items'][0]['id'], parse(data, source_id='google_ai')['items'][0]['id'])

    def test_window_sort_covers_all_entries_and_caps_only_output(self):
        result = parse(rss([item(url=f'https://example.com/{index}', date=f'Fri, 16 Oct 2026 {index % 24:02d}:00:00 GMT') for index in range(1296)]))
        self.assertEqual((result['scanned_count'], result['valid_count'], result['returned_count'], result['truncated']), (1296, 1296, 20, True))
        self.assertTrue(all(row['published_at'] == '2026-10-16T23:00:00+00:00' for row in result['items']))
        empty = parse(rss([]))
        self.assertEqual((empty['items'], empty['reason'], empty['truncated']), ([], None, False))
        filtered = parse(rss([item(url='http://example.com/unsafe')]))
        self.assertEqual((filtered['items'], filtered['reason']), ([], 'filtered_all'))

    def test_security_and_explicit_format_failures(self):
        dtd = '<!DOCTYPE rss [<!ENTITY x "not allowed">]><rss version="2.0"><channel><item><title>&x;</title></item></channel></rss>'
        for data in (dtd.encode(), dtd.encode('utf-16'), b'<rss>', b'<rss version="2.0"><channel/><channel/></rss>'):
            with self.subTest(data=data[:20]), self.assertRaisesRegex(ValueError, 'invalid_feed'):
                parse(data)
        for data in (b'<html/>', b'<rss version="1.0"><channel/></rss>', b'<feed xmlns="urn:not-atom"/>'):
            with self.assertRaisesRegex(ValueError, 'unsupported_format'):
                parse(data)

    def test_parse_budgets_reject_instead_of_truncating_tree(self):
        for data in (rss([item(url=f'https://example.com/{i}') for i in range(2001)]),
                     b'<rss version="2.0"><channel>' + b'<x>' * 63 + b'</x>' * 63 + b'</channel></rss>',
                     b'<rss version="2.0"><channel>' + b'<x/>' * 49999 + b'</channel></rss>'):
            with self.assertRaisesRegex(ValueError, 'parse_budget'):
                parse(data)
        with self.assertRaisesRegex(ValueError, 'oversize'):
            parse(b' ' * (1024 * 1024 + 1))
        self.assertEqual(parse(rss([item(url=f'https://example.com/{i}') for i in range(2000)]))['valid_count'], 2000)

    def test_hn_submission_time_and_discussion_fallback_never_publication(self):
        raw = {'hits': [{'objectID': '123', 'title': 'Synthetic HN', 'url': 'http://example.com/unsafe',
                         'created_at': '2026-10-16T10:00:00Z', 'created_at_i': 1792144800}]}
        row = news.parse_hn_result(json.dumps(raw).encode(), FETCHED, now=NOW)['items'][0]
        self.assertEqual((row['url'], row['published_at'], row['submitted_at'], row['summary'], row['content_origin']),
                         ('https://news.ycombinator.com/item?id=123', None, '2026-10-16T10:00:00+00:00', None, 'hn_submission'))
        raw['hits'][0]['created_at_i'] += 1
        self.assertIsNone(news.parse_hn_result(json.dumps(raw).encode(), FETCHED, now=NOW)['items'][0]['submitted_at'])

    def test_hn_strict_json_counts_and_identity(self):
        for data in (b'{"hits":[],"hits":[]}', b'{"hits":[],"n":NaN}', b'{"hits":[],"n":1e999}',
                     b'{"hits":[', b'\xff', b'{"hits":{}}'):
            with self.assertRaisesRegex(ValueError, 'invalid_feed'):
                news.parse_hn_result(data, FETCHED, now=NOW)
        rows = [{'objectID': 'not-a-number', 'title': 'No link', 'url': None},
                {'objectID': '1', 'title': 'Safe', 'url': 'https://example.com/story'},
                {'objectID': '2', 'title': 'Duplicate', 'url': 'https://example.com/story'}]
        result = news.parse_hn_result(json.dumps({'hits': rows}).encode(), FETCHED, now=NOW)
        self.assertEqual((result['scanned_count'], result['valid_count'], result['filtered_count'], result['duplicate_count']), (3, 1, 1, 1))
        with self.assertRaisesRegex(ValueError, 'parse_budget'):
            news.parse_hn_result(json.dumps({'hits': rows * 2}).encode(), FETCHED, now=NOW)

    def test_missing_link_never_becomes_feed_and_raw_controls_are_rejected(self):
        data = b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>No target</title><link/></entry></feed>'
        self.assertEqual(parse(data)['filtered_count'], 1)
        rows = [item(url='https://example.com/a&#10;b'), item(url='https://example.com/' + 'x' * 2048)]
        self.assertEqual(parse(rss(rows))['filtered_count'], 2)

    def test_atom_unknown_date_and_summary_type_do_not_fabricate_fields(self):
        data = b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Unknown</title>
          <link href="https://example.com/a"/><published>2026-10-16T10:00:00</published>
          <summary type="unsupported">Not a text summary</summary></entry></feed>'''
        row = parse(data)['items'][0]
        self.assertEqual((row['published_at'], row['date_reason'], row['summary'], row['summary_reason']),
                         (None, 'timezone_unknown', None, 'unsupported_type'))

    def test_invalid_timezone_components_are_not_normalized_into_dates(self):
        for zone in ('+01:60', '+12:99', '+24:00', '-24:00'):
            data = f'''<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Bad offset</title>
              <link href="https://example.com/a"/><published>2026-10-16T10:00:00{zone}</published></entry></feed>'''.encode()
            with self.subTest(atom_zone=zone):
                row = parse(data)['items'][0]
                self.assertEqual((row['published_at'], row['date_reason']), (None, 'invalid'))
        for zone in ('+1260', '-0160', '+2400'):
            with self.subTest(rss_zone=zone):
                row = parse(rss([item(date='Fri, 16 Oct 2026 10:00:00 ' + zone)]))['items'][0]
                self.assertEqual((row['published_at'], row['date_reason']), (None, 'invalid'))

    def test_projection_is_repeatable_and_output_independent(self):
        data = rss([item()])
        first = parse(data)
        second = parse(data)
        self.assertEqual(first, second)
        first['items'][0]['title'] = 'Changed locally'
        self.assertEqual(parse(data), second)


if __name__ == '__main__':
    unittest.main()
