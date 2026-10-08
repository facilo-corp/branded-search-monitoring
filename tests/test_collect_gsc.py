from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from datetime import date, timedelta
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from collect_gsc import (DataError, DOW, GSC_URL, HEADERS, ReadOnlyGoogle, batch_request,
                         classify, collect, collection_window, fingerprint, keyword_row,
                         main, prepare, private_directory, query_rows, snapshot_of,
                         tables, weekly_rows)
from dashboard import aggregate, render

START = date(2026, 5, 4)
TODAY = date(2026, 8, 27)


def fixture(count=112):
    daily, queries = [], []
    for i in range(count):
        dt = START + timedelta(days=i)
        d = dt.isoformat()
        daily.append([d, 10, 3, DOW[dt.weekday()]])
        queries += [keyword_row(d, 'facilo', 6, 2), keyword_row(d, 'ファシロ ログイン', 4, 1)]
    return snapshot_of({'日次': daily, 'キーワード': queries, '週次': weekly_rows(daily)})


def metadata(row_count=1000):
    return {'properties': {'timeZone': 'Asia/Tokyo'}, 'sheets': [
        {'properties': {'title': name, 'sheetId': i, 'sheetType': 'GRID',
                        'gridProperties': {'rowCount': row_count, 'columnCount': 26}}}
        for i, name in enumerate(HEADERS)]}


class ReplayAPI:
    """Only synthetic/saved data is served; no network or write method."""
    def __init__(self, source, fail_at=None, unavailable=()):
        self.source = tables(source)
        self.calls = []
        self.fail_at, self.unavailable = fail_at, set(unavailable)

    def query(self, payload):
        self.calls.append(deepcopy(payload))
        if len(self.calls) == self.fail_at:
            raise DataError('API_NETWORK: synthetic interruption')
        start, end = payload['startDate'], payload['endDate']
        if payload['dimensions'] == ['date']:
            filtered = 'dimensionFilterGroups' in payload
            rows = [dict(keys=[r[0]], impressions=r[1] if filtered else 1,
                         clicks=r[2] if filtered else 0) for r in self.source['日次']
                    if start <= r[0] <= end and r[0] not in self.unavailable and (not filtered or r[1])]
        else:
            rows = [dict(keys=r[:2], impressions=r[2], clicks=r[3]) for r in self.source['キーワード']
                    if start <= r[0] <= end]
        offset, limit = payload['startRow'], payload['rowLimit']
        return {'rows': rows[offset:offset + limit]}


class CollectionTests(unittest.TestCase):
    def test_replay_same_period_is_unchanged(self):
        source = fixture()
        candidate, report = prepare(ReplayAPI(source), source, TODAY, '2026-08-23')
        self.assertFalse(report['reviewRequired'])
        self.assertEqual(fingerprint(tables(candidate)), fingerprint(tables(source)))
        self.assertEqual((report['weeks'], report['impressions'], report['clicks']), (16, 1120, 336))

    def test_new_days_added_and_old_history_preserved_exactly(self):
        old = fixture()
        candidate, report = prepare(ReplayAPI(fixture(113)), old, TODAY)
        self.assertEqual(report['sourceLatestCandidate'], '2026-08-24')
        self.assertEqual(tables(old)['日次'][:50], tables(candidate)['日次'][:50])
        self.assertEqual(tables(old)['キーワード'][:100], tables(candidate)['キーワード'][:100])
        self.assertFalse(report['reviewRequired'])
        self.assertEqual(len(tables(candidate)['日次']), 113)

    def test_same_result_on_repeated_preparation(self):
        source = fixture(113)
        first, _ = prepare(ReplayAPI(source), fixture(), TODAY)
        second, _ = prepare(ReplayAPI(source), first, TODAY)
        self.assertEqual(first, second)

    def test_removed_query_replaced_in_window_not_left_behind(self):
        revised = tables(fixture())
        day = revised['日次'][-1][0]
        revised['キーワード'] = [r for r in revised['キーワード'] if r[0] != day]
        revised['キーワード'].append(keyword_row(day, 'facilo 新検索', 10, 3))
        candidate, report = prepare(ReplayAPI(snapshot_of(revised)), fixture(), TODAY, day)
        counts = report['existingPeriodDifferences']['キーワード']
        self.assertEqual(counts, {'added': 1, 'removed': 2, 'changed': 0})
        self.assertEqual(len([r for r in tables(candidate)['キーワード'] if r[0] == day]), 1)
        self.assertTrue(report['reviewRequired'])

    def test_revised_totals_require_human_review(self):
        revised = tables(fixture())
        revised['日次'][-1][1] += 1
        revised['キーワード'][-1][2] += 1
        _, report = prepare(ReplayAPI(snapshot_of(revised)), fixture(), TODAY, '2026-08-23')
        self.assertEqual(report['existingPeriodDifferences']['日次']['changed'], 1)
        self.assertTrue(report['reviewRequired'])

    def test_middle_failure_returns_no_candidate_and_does_not_mutate_source(self):
        source = fixture()
        before = deepcopy(source)
        with self.assertRaisesRegex(DataError, 'API_NETWORK'):
            prepare(ReplayAPI(source, fail_at=8), source, TODAY, '2026-08-23')
        self.assertEqual(source, before)

    def test_missing_final_day_not_zero_filled(self):
        with self.assertRaisesRegex(DataError, 'MISSING_FINAL_DAY'):
            prepare(ReplayAPI(fixture()), fixture(), TODAY)

    def test_confirmed_zero_brand_day_is_recorded(self):
        source = tables(fixture())
        day = source['日次'][-1][0]
        source['日次'][-1][1:3] = [0, 0]
        source['キーワード'] = [r for r in source['キーワード'] if r[0] != day]
        daily, queries = collect(ReplayAPI(snapshot_of(source)), day, day)
        self.assertEqual(daily[0][1:3], [0, 0])
        self.assertEqual(queries, [])

    def test_missing_property_day_rejected(self):
        with self.assertRaisesRegex(DataError, 'MISSING_FINAL_DAY'):
            collect(ReplayAPI(fixture(), unavailable=['2026-08-23']), '2026-08-23', '2026-08-23')

    def test_query_total_mismatch_stops(self):
        source = tables(fixture())
        source['キーワード'].pop()
        with self.assertRaisesRegex(DataError, 'GSC_TOTAL_MISMATCH'):
            prepare(ReplayAPI(snapshot_of(source)), fixture(), TODAY, '2026-08-23')

    def test_pagination_and_request_conditions(self):
        api = ReplayAPI(fixture())
        rows = query_rows(api, '2026-08-23', '2026-08-23', ['date', 'query'], limit=1)
        self.assertEqual(len(rows), 2)
        self.assertEqual([r['startRow'] for r in api.calls], [0, 1, 2])
        for req in api.calls:
            self.assertEqual((req['dataState'], req['type'], req['aggregationType']), ('final', 'web', 'auto'))
            self.assertEqual(req['dimensionFilterGroups'][0]['filters'][0]['expression'], 'facilo|ファシロ')

    def test_duplicate_page_rejected(self):
        class Bad:
            def query(self, payload):
                return {'rows': [{'keys': ['2026-08-23', 'facilo'], 'impressions': 1, 'clicks': 0}]}
        with self.assertRaisesRegex(DataError, 'API_DUPLICATE'):
            query_rows(Bad(), '2026-08-23', '2026-08-23', ['date', 'query'], limit=1)

    def test_malformed_counts_and_query_rejected_without_raw_row(self):
        for row in [{'keys': ['2026-08-23', 'facilo private phrase'], 'impressions': -1, 'clicks': 0},
                    {'keys': ['2026-08-23', 'private unrelated phrase'], 'impressions': 1, 'clicks': 0},
                    {'keys': ['2026-08-24', 'facilo private phrase'], 'impressions': 1, 'clicks': 0},
                    {'keys': ['2026-08-23', 'facilo private phrase'], 'impressions': 1, 'clicks': 2}]:
            class Bad:
                def query(self, payload):
                    return {'rows': [row]}
            with self.subTest(row=row), self.assertRaises(DataError) as caught:
                query_rows(Bad(), '2026-08-23', '2026-08-23', ['date', 'query'])
            self.assertNotIn('private', str(caught.exception))

    def test_date_serial_duplicate_rejected(self):
        source = fixture()
        row = deepcopy(source['valueRanges'][1]['values'][1])
        row[0] = (START - date(1899, 12, 30)).days
        source['valueRanges'][1]['values'].append(row)
        with self.assertRaisesRegex(DataError, 'DUPLICATE'):
            prepare(ReplayAPI(fixture()), source, TODAY)

    def test_historical_weekly_inconsistency_rejected(self):
        source = fixture()
        source['valueRanges'][0]['values'][1][1] += 1
        with self.assertRaisesRegex(DataError, 'WEEKLY_MISMATCH'):
            prepare(ReplayAPI(fixture()), source, TODAY)

    def test_cutoff_and_long_gap_rejected(self):
        for today, end, message in [(TODAY, '2026-08-25', 'CUTOFF'),
                                    (TODAY, '2026-08-22', 'CUTOFF'),
                                    (date(2027, 1, 1), None, 'LONG_GAP')]:
            with self.subTest(end=end), self.assertRaisesRegex(DataError, message):
                collection_window(tables(fixture()), today, end)

    def test_classification_matches_gas(self):
        for query, expected in [('FACILO', ('alpha_exact', '-')), (' ファシロ ', ('kana_exact', '-')),
                                ('facilo ファシロ', ('both', '-')), ('Facilo  CRM', ('alpha_compound', 'CRM'))]:
            self.assertEqual(classify(query), expected)

    def test_partial_weeks_same_as_existing_gas_policy(self):
        data = tables(fixture(113))
        data['日次'].insert(0, ['2026-05-03', 1, 0, '日'])
        result = weekly_rows(data['日次'])
        self.assertEqual(len(result), 17)
        self.assertEqual(result[0][0], '2026-05-04')
        self.assertEqual(result[-1][3], 1)

    def test_batch_is_one_atomic_payload_and_clears_old_tail(self):
        result = batch_request(fixture(), metadata(1000))
        self.assertEqual(len(result['requests']), 3)
        for req in result['requests']:
            update = req['updateCells']
            self.assertEqual(update['range']['endRowIndex'], 1000)
            self.assertEqual(update['fields'], 'userEnteredValue')
            self.assertGreater(update['range']['endRowIndex'], len(update['rows']))

    def test_batch_grows_grid_and_keeps_formula_like_query_as_text(self):
        source = fixture()
        source['valueRanges'][2]['values'][1][1] = '=facilo'
        requests = batch_request(source, metadata(5))['requests']
        self.assertEqual(sum('appendDimension' in r for r in requests), 3)
        cells = requests[-1]['updateCells']['rows'][1]['values']
        self.assertEqual(cells[1]['userEnteredValue'], {'stringValue': '=facilo'})

    def test_timezone_and_missing_tab_rejected(self):
        meta = metadata()
        meta['properties']['timeZone'] = 'UTC'
        with self.assertRaises(DataError):
            batch_request(fixture(), meta)
        meta = metadata()
        meta['sheets'].pop()
        with self.assertRaises(DataError):
            batch_request(fixture(), meta)

    def test_readonly_client_rejects_all_write_urls_before_network(self):
        api = ReadOnlyGoogle('x' * 25, 'synthetic-credential')
        with patch('collect_gsc.urlopen') as network:
            for url in [api.sheet_url + ':batchUpdate', api.sheet_url + '/values:batchUpdate',
                        'https://www.googleapis.com/drive/v3/files']:
                with self.assertRaisesRegex(DataError, 'WRITE_FORBIDDEN'):
                    api.request(url, {})
            network.assert_not_called()

    def test_http_failure_is_redacted_and_not_retried_for_403(self):
        api = ReadOnlyGoogle('x' * 25, 'synthetic-credential')
        error = HTTPError(GSC_URL, 403, 'private sensitive body', {}, io.BytesIO(b'private sensitive body'))
        with patch('collect_gsc.urlopen', side_effect=error) as network:
            with self.assertRaisesRegex(DataError, 'API_HTTP_403') as caught:
                api.query({})
            self.assertEqual(network.call_count, 1)
            self.assertNotIn('private', str(caught.exception))

    def test_private_output_cannot_be_in_repo(self):
        with self.assertRaisesRegex(DataError, 'PRIVATE_OUTPUT'):
            private_directory(Path(__file__).resolve().parents[1] / 'site/private')

    def test_fingerprint_ignores_order_but_detects_edit(self):
        data = tables(fixture())
        before = fingerprint(data)
        data['キーワード'].reverse()
        self.assertEqual(fingerprint(data), before)
        data['日次'][0][1] = float(data['日次'][0][1])
        self.assertEqual(fingerprint(data), before)
        data['キーワード'][0][6] = 'changed'
        self.assertNotEqual(fingerprint(data), before)

    def test_cli_refuses_nonempty_output_without_network(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, 'old.json').write_text('old')
            with patch.object(sys, 'argv', ['collect', '--output-dir', folder]), patch('collect_gsc.urlopen') as net:
                with redirect_stderr(io.StringIO()):
                    self.assertEqual(main(), 1)
                net.assert_not_called()

    def test_failed_collection_cli_emits_no_partial_candidate(self):
        api = Mock()
        api.snapshot.return_value = fixture()
        api.metadata.return_value = metadata()
        with tempfile.TemporaryDirectory() as folder, patch.object(sys, 'argv', ['collect', '--output-dir', folder]):
            with patch('collect_gsc.ReadOnlyGoogle', return_value=api), patch('collect_gsc.prepare', side_effect=DataError('API_NETWORK')):
                with redirect_stderr(io.StringIO()):
                    self.assertEqual(main(), 1)
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_source_changed_during_cli_stops_before_outputs(self):
        source = fixture()
        changed = deepcopy(source)
        changed['valueRanges'][2]['values'][1][6] = 'changed'
        api = Mock()
        api.snapshot.side_effect = [source, changed]
        api.metadata.return_value = metadata()
        report = {'sourceFingerprint': fingerprint(tables(source)), 'reviewRequired': False}
        with tempfile.TemporaryDirectory() as folder, patch.object(sys, 'argv', ['collect', '--output-dir', folder]):
            with patch('collect_gsc.ReadOnlyGoogle', return_value=api), patch('collect_gsc.prepare', return_value=(source, report)):
                with redirect_stderr(io.StringIO()) as errors:
                    self.assertEqual(main(), 1)
                self.assertIn('SOURCE_CHANGED', errors.getvalue())
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_cli_success_and_difference_exit_codes_and_private_files(self):
        source = fixture()
        api = Mock()
        api.snapshot.return_value = source
        api.metadata.return_value = metadata()
        for review, code in [(False, 0), (True, 2)]:
            report = {'sourceFingerprint': fingerprint(tables(source)), 'reviewRequired': review}
            with tempfile.TemporaryDirectory() as folder, patch.object(sys, 'argv', ['collect', '--output-dir', folder]):
                with patch('collect_gsc.ReadOnlyGoogle', return_value=api), patch('collect_gsc.prepare', return_value=(source, report)):
                    with redirect_stdout(io.StringIO()) as output:
                        self.assertEqual(main(), code)
                    self.assertNotIn('ログイン', output.getvalue())
                self.assertEqual(len(list(Path(folder).iterdir())), 4)
                payload = json.loads(Path(folder, 'sheet-request-review-only.json').read_text(encoding='utf-8'))
                self.assertEqual(len(payload['requests']), 3)

    def test_retry_limit_is_finite(self):
        api = ReadOnlyGoogle('x' * 25, 'synthetic-credential')
        errors = [HTTPError(GSC_URL, 429, 'private body', {}, io.BytesIO()) for _ in range(3)]
        with patch('collect_gsc.urlopen', side_effect=errors) as net, patch('collect_gsc.time.sleep') as sleep:
            with self.assertRaisesRegex(DataError, 'API_HTTP_429'):
                api.query({})
            self.assertEqual(net.call_count, 3)
            self.assertEqual(sleep.call_count, 2)

    def test_pagination_ceiling_stops(self):
        class Bad:
            def query(self, payload):
                return {'rows': [{'keys': ['2026-08-23', 'facilo ' + str(payload['startRow'])],
                                  'impressions': 1, 'clicks': 0}]}
        with self.assertRaisesRegex(DataError, 'PAGE_LIMIT'):
            query_rows(Bad(), '2026-08-23', '2026-08-23', ['date', 'query'], limit=1)

    def test_nonfinal_response_rejected(self):
        class Bad:
            def query(self, payload):
                return {'metadata': {'first_incomplete_date': '2026-08-23'}}
        with self.assertRaisesRegex(DataError, 'INCOMPLETE_DATA'):
            query_rows(Bad(), '2026-08-23', '2026-08-23', ['date'])

    def test_invalid_existing_ctr_rejected(self):
        for value in [float('nan'), float('inf'), True]:
            source = fixture()
            source['valueRanges'][2]['values'][1][4] = value
            with self.assertRaisesRegex(DataError, 'CLASSIFICATION_MISMATCH'):
                prepare(ReplayAPI(fixture()), source, TODAY)


if __name__ == '__main__':
    unittest.main()
