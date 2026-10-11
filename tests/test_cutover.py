from copy import deepcopy
from datetime import date, timedelta
import gzip
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.error import URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from collect_gsc import DataError, fingerprint, snapshot_of, tables
from collect_gsc import batch_request
from dashboard import aggregate
from cutover import (DriveCreateRead, TestSheetClient, classify_sheet, decode_backup,
                     encode_backup, restore_test_sheet, save_checked, update_test_sheet)
from test_collect_gsc import fixture, metadata


SHEET = 'test-sheet-only-12345678901234567890'
PROD = '1XrYckoVd3vurkLfhdrF9y1AQbg7muJioSSza30kn6J0'
VERSION = '4f1139657019c2b97cc1b04b4b426ed940742f21'
RUN = 'review-run-0001'


def meta():
    result = metadata()
    result['spreadsheetId'] = SHEET
    return result


class FakeStore:
    def __init__(self):
        self.files = {}
        self.events = []
        self.corrupt_read = False
        self.fail_on_create = 0

    def create(self, name, content, mime):
        self.events.append('create:' + name)
        if self.fail_on_create and len(self.files) + 1 == self.fail_on_create:
            raise DataError('BACKUP_WRITE: 模擬障害')
        file_id = 'file-' + str(len(self.files) + 1)
        self.files[file_id] = (name, content, mime)
        return file_id

    def read(self, file_id):
        self.events.append('read:' + file_id)
        blob = self.files[file_id][1]
        return blob[:-1] if self.corrupt_read else blob


class FakeSheet:
    def __init__(self, snapshot, next_snapshot):
        self.current = deepcopy(snapshot)
        self.next = deepcopy(next_snapshot)
        self.events = []
        self.meta = meta()
        self.fail_write = False
        self.commit_then_timeout = False
        self.drift_after_first = False
        self.snapshot_count = 0

    def snapshot(self):
        self.snapshot_count += 1
        self.events.append('snapshot')
        if self.drift_after_first and self.snapshot_count == 2:
            self.current['valueRanges'][2]['values'][1][1] = 'facilo drift'
            self.current['valueRanges'][2]['values'][1][5] = 'alpha_compound'
            self.current['valueRanges'][2]['values'][1][6] = 'drift'
        return deepcopy(self.current)

    def metadata(self):
        self.events.append('metadata')
        return deepcopy(self.meta)

    def batch_update(self, payload):
        self.events.append('batch_update')
        assert len(payload['requests']) == 3
        if self.fail_write:
            raise ConnectionError('simulated ambiguous failure')
        self.current = deepcopy(self.next)
        if self.commit_then_timeout:
            raise ConnectionError('simulated timeout after commit')


def report(source, candidate=None):
    candidate = candidate or fixture(113)
    old, new = tables(source), tables(candidate)
    summary = aggregate(candidate, date.fromisoformat(new['日次'][-1][0]))
    return {'sourceFingerprint': fingerprint(old), 'reviewRequired': False,
            'windowStart': (date.fromisoformat(old['日次'][-1][0]) + timedelta(days=1)).isoformat(),
            'windowEnd': new['日次'][-1][0],
            'sourceLatestBefore': old['日次'][-1][0],
            'sourceLatestCandidate': new['日次'][-1][0],
            'rowsBefore': {name: len(old[name]) for name in old},
            'rowsCandidate': {name: len(new[name]) for name in new},
            'weeks': summary['weeks'], 'impressions': summary['impressions'],
            'clicks': summary['clicks'],
            'existingPeriodDifferences': {'日次': {'added': 0, 'removed': 0, 'changed': 0},
                                          'キーワード': {'added': 0, 'removed': 0, 'changed': 0}}}


class CutoverTests(unittest.TestCase):
    def setUp(self):
        self.before = fixture(112)
        self.after = fixture(113)
        self.sheet = FakeSheet(self.before, self.after)
        self.store = FakeStore()

    def update(self):
        return update_test_sheet(self.sheet, self.store, SHEET, self.after,
                                 report(self.before), RUN, VERSION)

    def test_full_backup_roundtrip_and_manifest(self):
        blob, original = encode_backup(self.before, meta(), SHEET, RUN, 'before', VERSION,
                                       '2026-10-09T00:00:00Z')
        parsed = decode_backup(blob, meta(), SHEET)
        self.assertEqual(parsed, original)
        self.assertEqual(parsed['rows']['キーワード'], 224)
        self.assertEqual(parsed['fingerprint'], fingerprint(tables(self.before)))
        record = save_checked(self.store, RUN + '-before.json.gz', blob, meta(), SHEET)
        self.assertEqual(record['fingerprint'], parsed['fingerprint'])

    def test_production_sheet_is_blocked_for_backup_update_and_restore(self):
        bad = meta()
        bad['spreadsheetId'] = PROD
        with self.assertRaisesRegex(DataError, 'TARGET_LOCK'):
            encode_backup(self.before, bad, PROD, RUN, 'before', VERSION)
        with self.assertRaisesRegex(DataError, 'TARGET_LOCK'):
            update_test_sheet(self.sheet, self.store, PROD, self.after,
                              report(self.before), RUN, VERSION)
        with self.assertRaisesRegex(DataError, 'TARGET_LOCK'):
            restore_test_sheet(self.sheet, self.store, PROD, 'file-1', RUN, VERSION)
        self.assertNotIn('batch_update', self.sheet.events)

    def test_corrupt_and_wrong_sheet_or_tab_rejected(self):
        blob, _ = encode_backup(self.before, meta(), SHEET, RUN, 'before', VERSION)
        with self.assertRaisesRegex(DataError, 'BACKUP_CORRUPT'):
            decode_backup(blob[:-10], meta(), SHEET)
        changed = meta()
        changed['sheets'][0]['properties']['sheetId'] = 101
        with self.assertRaisesRegex(DataError, 'BACKUP_IDENTITY'):
            decode_backup(blob, changed, SHEET)
        changed = meta()
        changed['spreadsheetId'] = 'some-other-test-sheet-1234567890'
        with self.assertRaisesRegex(DataError, 'TARGET_LOCK'):
            decode_backup(blob, changed, SHEET)
        document = json.loads(gzip.decompress(blob))
        document['totals']['clicks'] += 1
        with self.assertRaisesRegex(DataError, 'BACKUP_INTEGRITY'):
            decode_backup(gzip.compress(json.dumps(document).encode(), mtime=0), meta(), SHEET)

    def test_success_requires_before_and_after_readback(self):
        result = self.update()
        self.assertEqual(result['state'], 'saved_and_verified')
        self.assertEqual(len(self.store.files), 3)
        self.assertEqual([e for e in self.store.events if e.startswith('read:')],
                         ['read:file-1', 'read:file-2', 'read:file-3'])
        self.assertEqual(classify_sheet(self.sheet.snapshot(),
                                        report(self.before)['sourceFingerprint'],
                                        fingerprint(tables(self.after))), 'candidate')
        self.assertEqual(self.sheet.events.count('batch_update'), 1)

    def test_backup_readback_failure_stops_before_sheet_write(self):
        self.store.corrupt_read = True
        with self.assertRaisesRegex(DataError, 'BACKUP_READBACK'):
            self.update()
        self.assertNotIn('batch_update', self.sheet.events)

    def test_source_change_stops_before_sheet_write(self):
        self.sheet.drift_after_first = True
        with self.assertRaisesRegex(DataError, 'SOURCE_CHANGED'):
            self.update()
        self.assertEqual(len(self.store.files), 1)
        self.assertNotIn('batch_update', self.sheet.events)

    def test_existing_period_difference_stops_before_backup(self):
        changed_report = report(self.before)
        changed_report['existingPeriodDifferences']['日次']['changed'] = 1
        with self.assertRaisesRegex(DataError, 'REVIEW_REQUIRED'):
            update_test_sheet(self.sheet, self.store, SHEET, self.after,
                              changed_report, RUN, VERSION)
        self.assertEqual(self.store.files, {})

    def test_old_history_outside_collection_window_cannot_be_changed(self):
        candidate = deepcopy(self.after)
        candidate['valueRanges'][2]['values'][1][1] = 'facilo revised'
        candidate['valueRanges'][2]['values'][1][5] = 'alpha_compound'
        candidate['valueRanges'][2]['values'][1][6] = 'revised'
        with self.assertRaisesRegex(DataError, 'HISTORY_CHANGED'):
            update_test_sheet(self.sheet, self.store, SHEET, candidate,
                              report(self.before), RUN, VERSION)
        self.assertEqual(self.store.files, {})
        self.assertNotIn('batch_update', self.sheet.events)

    def test_uncertain_write_is_not_repeated(self):
        self.sheet.fail_write = True
        with self.assertRaisesRegex(DataError, 'WRITE_UNCONFIRMED_BEFORE'):
            self.update()
        self.assertEqual(self.sheet.events.count('batch_update'), 1)
        self.assertEqual(len(self.store.files), 1)

    def test_commit_then_timeout_can_be_verified_without_retry(self):
        self.sheet.commit_then_timeout = True
        self.assertEqual(self.update()['state'], 'saved_and_verified')
        self.assertEqual(self.sheet.events.count('batch_update'), 1)

    def test_after_backup_failure_halts_without_undoing_sheet(self):
        self.store.fail_on_create = 2
        with self.assertRaisesRegex(DataError, 'BACKUP_WRITE'):
            self.update()
        self.assertEqual(self.sheet.events.count('batch_update'), 1)
        self.assertEqual(fingerprint(tables(self.sheet.current)), fingerprint(tables(self.after)))

    def test_restore_preserves_current_and_restores_all_history(self):
        blob, _ = encode_backup(self.before, meta(), SHEET, RUN, 'before', VERSION)
        generation = self.store.create(RUN + '-before.json.gz', blob, 'application/gzip')
        self.sheet.current = deepcopy(self.after)
        self.sheet.next = deepcopy(self.before)
        receipt = restore_test_sheet(self.sheet, self.store, SHEET, generation,
                                     'restore-run-0001', VERSION)
        self.assertEqual(receipt['state'], 'restored_and_verified')
        self.assertEqual(fingerprint(tables(self.sheet.current)), fingerprint(tables(self.before)))
        self.assertEqual(self.sheet.events.count('batch_update'), 1)
        self.assertEqual(len(self.store.files), 2)
        safety = decode_backup(self.store.files['file-2'][1], meta(), SHEET)
        self.assertEqual(safety['fingerprint'], fingerprint(tables(self.after)))

    def test_drive_adapter_only_creates_in_configured_folder_and_reads_there(self):
        folder = 'folder-id-12345678901234567890'
        file_id = 'file-id-12345678901234567890'
        responses = [json.dumps({'id': file_id, 'name': RUN + '-before.json.gz',
                                  'parents': [folder], 'mimeType': 'application/gzip'}).encode(),
                     json.dumps({'id': file_id, 'parents': [folder],
                                  'mimeType': 'application/gzip'}).encode(), b'private backup']
        with patch('cutover.urlopen', side_effect=[io.BytesIO(r) for r in responses]) as network:
            store = DriveCreateRead(folder, 'synthetic-token')
            self.assertEqual(store.create(RUN + '-before.json.gz', b'private backup',
                                          'application/gzip'), file_id)
            self.assertEqual(store.read(file_id), b'private backup')
            urls = [call.args[0].full_url for call in network.call_args_list]
            self.assertIn('uploadType=multipart', urls[0])
            self.assertIn('supportsAllDrives=true', urls[0])
            self.assertIn('alt=media', urls[2])
            self.assertEqual(network.call_count, 3)

    def test_drive_adapter_rejects_other_folder_before_data_read(self):
        folder = 'folder-id-12345678901234567890'
        file_id = 'file-id-12345678901234567890'
        response = json.dumps({'id': file_id, 'parents': ['different-folder-id-1234567890']}).encode()
        with patch('cutover.urlopen', return_value=io.BytesIO(response)) as network:
            with self.assertRaisesRegex(DataError, 'BACKUP_FOLDER'):
                DriveCreateRead(folder, 'synthetic-token').read(file_id)
            self.assertEqual(network.call_count, 1)

    def test_drive_create_timeout_is_not_retried(self):
        store = DriveCreateRead('folder-id-12345678901234567890', 'synthetic-token')
        with patch('cutover.urlopen', side_effect=URLError('secret')) as network:
            with self.assertRaisesRegex(DataError, 'BACKUP_NETWORK_UNKNOWN') as caught:
                store.create(RUN + '-before.json.gz', b'private backup', 'application/gzip')
            self.assertEqual(network.call_count, 1)
            self.assertNotIn('secret', str(caught.exception))

    def test_sheet_adapter_rejects_production_before_network(self):
        with patch('cutover.urlopen') as network:
            with self.assertRaisesRegex(DataError, 'TARGET_LOCK'):
                TestSheetClient(PROD, 'synthetic-token')
            network.assert_not_called()

    def test_sheet_adapter_allows_only_three_tab_value_update(self):
        client = TestSheetClient(SHEET, 'synthetic-token')
        client.metadata = meta
        payload = batch_request(self.after, meta())
        with patch('cutover.urlopen', return_value=io.BytesIO(b'{}')) as network:
            client.batch_update(payload)
            self.assertEqual(network.call_count, 1)
            self.assertTrue(network.call_args.args[0].full_url.endswith(SHEET + ':batchUpdate'))
            bad = deepcopy(payload)
            bad['requests'][0]['updateCells']['fields'] = 'userEnteredValue,userEnteredFormat'
            with self.assertRaisesRegex(DataError, 'WRITE_SCOPE'):
                client.batch_update(bad)
            self.assertEqual(network.call_count, 1)


if __name__ == '__main__':
    unittest.main()
