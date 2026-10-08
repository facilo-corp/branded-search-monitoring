from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from dashboard import DataError, aggregate, build, date_key, extract_metadata, guard_history, render, script_json, validate_html

ROOT = Path(__file__).resolve().parents[1]
START = date(2026, 5, 4) # Monday; fixtures are entirely synthetic.
NOW = datetime(2026, 8, 27, 0, 17, tzinfo=timezone.utc)


def fixture(count=112):
    daily = [['date', 'impressions', 'clicks', 'dow']]
    queries = [['date', 'query', 'impressions', 'clicks', 'ctr_%', 'type', 'modifier']]
    for i in range(count):
        d = (START + timedelta(days=i)).isoformat()
        daily.append([d, 10, 3, ''])
        queries.append([d, 'facilo', 6, 2])
        queries.append([d, 'ファシロ ログイン', 4, 1])
    return {'valueRanges': [{'range': "'日次'!A1:D", 'values': daily},
                            {'range': "'キーワード'!A1:G", 'values': queries}]}


class AggregationTests(unittest.TestCase):
    def test_known_totals_and_same_period(self):
        result = aggregate(fixture(), NOW.date())
        self.assertEqual((result['weeks'], result['impressions'], result['clicks']), (16, 1120, 336))
        self.assertEqual(sum(c['imp'] for c in result['categories']), 1120)

    def test_partial_edges_not_mixed_into_categories(self):
        data = fixture(114)
        data['valueRanges'][0]['values'].insert(1, ['2026-05-03', 999, 1, ''])
        data['valueRanges'][1]['values'].insert(1, ['2026-05-03', 'facilo', 999, 1])
        result = aggregate(data, NOW.date())
        self.assertEqual((result['impressions'], result['excludedStartDays'], result['excludedEndDays']), (1120, 1, 2))

    def test_serial_and_text_date_duplicate_rejected(self):
        data = fixture()
        duplicate = data['valueRanges'][0]['values'][1].copy()
        duplicate[0] = (START - date(1899, 12, 30)).days
        data['valueRanges'][0]['values'].append(duplicate)
        with self.assertRaisesRegex(DataError, 'DUPLICATE'):
            aggregate(data, NOW.date())

    def test_duplicate_keyword_rejected(self):
        data = fixture()
        data['valueRanges'][1]['values'].append(data['valueRanges'][1]['values'][1])
        with self.assertRaisesRegex(DataError, 'DUPLICATE'):
            aggregate(data, NOW.date())

    def test_invalid_date_rejected(self):
        for value in ['2026-02-30', '2026-05-04 garbage', True, 46000.5]:
            with self.subTest(value=value), self.assertRaises(DataError):
                date_key(value)

    def test_missing_internal_day_rejected(self):
        data = fixture()
        del data['valueRanges'][0]['values'][30]
        with self.assertRaisesRegex(DataError, 'MISSING_DAY'):
            aggregate(data, NOW.date())

    def test_mismatch_rejected(self):
        data = fixture()
        data['valueRanges'][1]['values'][1][2] += 1
        with self.assertRaisesRegex(DataError, 'TOTAL_MISMATCH'):
            aggregate(data, NOW.date())

    def test_zero_impression_day_without_keyword_rows(self):
        data = fixture()
        data['valueRanges'][0]['values'][1][1:3] = [0, 0]
        del data['valueRanges'][1]['values'][1:3]
        self.assertEqual(aggregate(data, NOW.date())['impressions'], 1110)

    def test_stale_or_future_rejected(self):
        for today, message in [(date(2026, 10, 8), 'STALE_DATA'), (date(2026, 7, 1), 'FUTURE_DATE')]:
            with self.subTest(today=today), self.assertRaisesRegex(DataError, message):
                aggregate(fixture(), today)

    def test_failure_preserves_existing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'index.html'
            build(fixture(), ROOT / 'template.html', target, NOW)
            before = target.read_bytes()
            data = fixture()
            data['valueRanges'][0]['values'][10][1] += 1
            with self.assertRaises(DataError):
                build(data, ROOT / 'template.html', target, NOW)
            self.assertEqual(target.read_bytes(), before)

    def test_history_regression_rejected(self):
        result = aggregate(fixture(), NOW.date())
        old = render(result, (ROOT / 'template.html').read_text(encoding='utf-8'), NOW)
        result['periodStart'] = '2026-05-11'
        with self.assertRaisesRegex(DataError, 'HISTORY_LOSS'):
            guard_history(result, old)

    def test_script_breakout_escaped(self):
        attack = '</script><img src=x onerror=alert(1)>'
        self.assertNotIn('<', script_json([attack]))
        data = fixture()
        data['valueRanges'][1]['values'][1][1] = 'facilo ' + attack
        html = render(aggregate(data, NOW.date()), (ROOT / 'template.html').read_text(encoding='utf-8'), NOW)
        self.assertNotIn(attack, html)
        self.assertIn('escapeHtml(k)', html)

    def test_noindex_or_dependency_failure(self):
        html = render(aggregate(fixture(), NOW.date()), (ROOT / 'template.html').read_text(encoding='utf-8'), NOW)
        for broken in [html.replace('noindex, nofollow, noarchive', 'index'),
                       html.replace('</head>', '<script src="https://example.invalid/a.js"></script></head>')]:
            with self.assertRaises(DataError):
                validate_html(broken)

    def test_deterministic_and_rerunnable(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'index.html'
            first = build(fixture(), ROOT / 'template.html', target, NOW)
            second = build(fixture(), ROOT / 'template.html', target, NOW)
            self.assertEqual(first, second)
            self.assertEqual(extract_metadata(target.read_text(encoding='utf-8'))['weeks'], 16)

    def test_next_complete_week_updates_totals_and_period(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'index.html'
            build(fixture(), ROOT / 'template.html', target, NOW)
            next_time = datetime(2026, 9, 1, tzinfo=timezone.utc)
            result = build(fixture(119), ROOT / 'template.html', target, next_time)
            self.assertEqual((result['weeks'], result['impressions'], result['clicks']), (17, 1190, 357))
            self.assertEqual(result['periodEnd'], '2026-08-30')


if __name__ == '__main__':
    unittest.main()
