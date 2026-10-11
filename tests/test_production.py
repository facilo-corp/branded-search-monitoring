from copy import deepcopy
from datetime import datetime
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.error import URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from collect_gsc import batch_request, fingerprint, tables
from cutover import (PRODUCTION_SHEET_ID, TestSheetClient, decode_backup,
                     encode_backup, update_test_sheet)
from dashboard import DataError, JST
from production import (BACKUP_FOLDER, REPOSITORY, WORKFLOW_REF, ProductionSheetClient,
                        collect_production, configuration, publish_verified,
                        restore_production, route)
from rehearse_cutover import configuration as rehearsal_configuration
from test_collect_gsc import ReplayAPI, fixture, metadata
from test_cutover import FakeStore, VERSION
from test_rehearse_cutover import Target

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 27, 10, tzinfo=JST)
RUN = 'production-123456-1'


class ProductionTarget(Target):
    def __init__(self):
        super().__init__()
        self.reader = ReplayAPI(fixture(113))
        self.commit_then_timeout = False
        self.fail_before_commit = False
        self.meta = metadata()
        self.meta['spreadsheetId'] = PRODUCTION_SHEET_ID

    def metadata(self):
        return deepcopy(self.meta)

    def batch_update(self, payload):
        if self.fail_before_commit:
            self.writes += 1
            raise URLError('synthetic failure')
        super().batch_update(payload)
        if self.commit_then_timeout:
            raise URLError('synthetic failure after commit')


class Store(FakeStore):
    def __init__(self):
        super().__init__()
        self.reject_folder = False
        self.fail_read_number = 0
        self.read_count = 0

    def verify_folder(self, drive, *, require_write=True):
        self.events.append('verify_folder')
        if self.reject_folder:
            raise DataError('BACKUP_FOLDER')

    def read(self, key):
        self.read_count += 1
        if self.read_count == self.fail_read_number:
            raise DataError('BACKUP_READBACK')
        return super().read(key)


class ProductionTests(unittest.TestCase):
    def setUp(self):
        self.target, self.store = ProductionTarget(), Store()
        self.template = (ROOT / 'template.html').read_text(encoding='utf-8')
        self.env = {
            'GITHUB_EVENT_NAME':'workflow_dispatch', 'GITHUB_REPOSITORY':REPOSITORY,
            'GITHUB_REF':'refs/heads/main', 'GITHUB_WORKFLOW_REF':WORKFLOW_REF,
            'GITHUB_SHA':VERSION, 'GITHUB_RUN_ID':'123456', 'GITHUB_RUN_ATTEMPT':'1',
            'AUTOMATION_ENABLED':'true', 'SCHEDULE_ENABLED':'true',
            'PRODUCTION_ENABLED':'true', 'LEGACY_GAS_STOPPED':'true',
            'RESTORE_ENABLED':'false', 'SHEET_ID':PRODUCTION_SHEET_ID,
            'PRODUCTION_BACKUP_FOLDER_ID':BACKUP_FOLDER,
            'COLLECT':'true', 'PUBLISH':'true'
        }

    def collect(self, reader=None):
        return collect_production(self.target, reader or self.target, self.store,
                                  RUN, VERSION, NOW, self.template, None)

    def test_normal_collection_backs_up_and_renders_only_verified_data(self):
        html, receipt = self.collect()
        self.assertEqual(self.target.writes, 1)
        self.assertEqual(len(self.store.files), 3)
        self.assertEqual(receipt['sheetWrites'], 1)
        self.assertEqual(receipt['sourceLatestDay'], '2026-08-24')
        self.assertIn('noindex', html)
        self.assertIn('2026-08-24', html)
        self.assertNotIn('ファシロ ログイン', json.dumps(receipt, ensure_ascii=False))
        before = decode_backup(self.store.files['file-1'][1], self.target.metadata(),
                               PRODUCTION_SHEET_ID, production=True)
        self.assertEqual(before['rows']['日次'], 112)

    def test_repeat_same_day_verifies_backup_but_does_not_write_again(self):
        self.collect()
        _, receipt = self.collect()
        self.assertEqual(self.target.writes, 1)
        self.assertEqual(receipt['sheetWrites'], 0)
        self.assertEqual(len(self.store.files), 6)

    def test_folder_or_reader_failure_blocks_all_writes(self):
        self.store.reject_folder = True
        with self.assertRaisesRegex(DataError, 'BACKUP_FOLDER'):
            self.collect()
        self.assertEqual(self.target.writes, 0)
        self.assertFalse(self.store.files)
        self.store.reject_folder = False
        reader = ProductionTarget()
        reader.current = fixture(111)
        with self.assertRaisesRegex(DataError, 'READER_MISMATCH'):
            self.collect(reader)
        self.assertEqual(self.target.writes, 0)
        self.assertFalse(self.store.files)

    def test_backup_before_failure_blocks_sheet_write(self):
        self.store.fail_on_create = 1
        with self.assertRaisesRegex(DataError, 'BACKUP_WRITE'):
            self.collect()
        self.assertEqual(self.target.writes, 0)

    def test_backup_readback_failure_blocks_sheet_write(self):
        self.store.corrupt_read = True
        with self.assertRaisesRegex(DataError, 'BACKUP_READBACK'):
            self.collect()
        self.assertEqual(self.target.writes, 0)

    def test_backup_after_and_manifest_failures_do_not_return_html_or_restore(self):
        for number in (2, 3):
            with self.subTest(number=number):
                self.target, self.store = ProductionTarget(), Store()
                self.store.fail_on_create = number
                with self.assertRaisesRegex(DataError, 'BACKUP_WRITE'):
                    self.collect()
                self.assertEqual(self.target.writes, 1)
                self.assertEqual(tables(self.target.current)['日次'][-1][0], '2026-08-24')

    def test_manifest_readback_failure_blocks_publication(self):
        self.store.fail_read_number = 3
        with self.assertRaisesRegex(DataError, 'BACKUP_READBACK'):
            self.collect()
        self.assertEqual(self.target.writes, 1)

    def test_recovery_from_after_backup_failure_never_rewrites_same_data(self):
        self.store.fail_on_create = 2
        with self.assertRaises(DataError):
            self.collect()
        self.store.fail_on_create = 0
        _, receipt = self.collect()
        self.assertEqual(receipt['sheetWrites'], 0)
        self.assertEqual(self.target.writes, 1)

    def test_write_committed_then_timeout_is_read_back_not_retried(self):
        self.target.commit_then_timeout = True
        _, receipt = self.collect()
        self.assertEqual(receipt['sheetWrites'], 1)
        self.assertEqual(self.target.writes, 1)

    def test_uncommitted_timeout_does_not_blindly_retry(self):
        self.target.fail_before_commit = True
        with self.assertRaisesRegex(DataError, 'WRITE_UNCONFIRMED_BEFORE'):
            self.collect()
        self.assertEqual(self.target.writes, 1)
        self.assertEqual(len(self.store.files), 1)

    def test_query_missing_or_existing_difference_prevents_writes(self):
        self.target.reader = ReplayAPI(fixture(113), unavailable=('2026-08-24',))
        with self.assertRaisesRegex(DataError, 'MISSING_FINAL_DAY'):
            self.collect()
        self.assertEqual(self.target.writes, 0)
        self.target.reader = ReplayAPI(fixture(113))
        row = self.target.reader.source['キーワード'][0]
        # Within the re-fetch window; retain the total but change search phrase.
        row = self.target.reader.source['キーワード'][-4]
        row[1], row[5], row[6] = 'facilo changed', 'alpha_compound', 'changed'
        with self.assertRaisesRegex(DataError, 'REVIEW_REQUIRED'):
            self.collect()
        self.assertEqual(self.target.writes, 0)

    def test_no_page_when_post_write_reader_lags(self):
        reader = ProductionTarget()
        with self.assertRaisesRegex(DataError, 'READER_MISMATCH'):
            self.collect(reader)
        self.assertEqual(self.target.writes, 1)

    def test_publish_only_never_queries_or_writes(self):
        _, receipt = self.collect()
        writes, files = self.target.writes, len(self.store.files)
        self.target.reader.query = lambda _: self.fail('unexpected GSC query')
        html, public = publish_verified(self.target, self.store, receipt['backup']['manifest']['id'],
                                        NOW, self.template, None)
        self.assertIn('noindex', html)
        self.assertEqual(public['sheetWrites'], 0)
        self.assertEqual((self.target.writes, len(self.store.files)), (writes, files))

    def test_publish_only_rejects_changed_sheet_and_corrupt_receipt(self):
        _, receipt = self.collect()
        manifest_id = receipt['backup']['manifest']['id']
        self.target.current = fixture(112)
        with self.assertRaisesRegex(DataError, 'READER_MISMATCH'):
            publish_verified(self.target, self.store, manifest_id, NOW, self.template, None)
        self.target.current = fixture(113)
        name, payload, mime = self.store.files[manifest_id]
        doc = json.loads(payload)
        doc['after']['sha256'] = '0' * 64
        self.store.files[manifest_id] = (name, json.dumps(doc).encode(), mime)
        with self.assertRaisesRegex(DataError, 'VERIFIED_MANIFEST_INVALID'):
            publish_verified(self.target, self.store, manifest_id, NOW, self.template, None)

    def test_restore_requires_approved_current_fingerprint_and_saves_after_receipt(self):
        previous, receipt = self.collect()
        old_fp = fingerprint(tables(fixture(112)))
        current_fp = fingerprint(tables(fixture(113)))
        generation = receipt['backup']['before']['id']
        with self.assertRaisesRegex(DataError, 'READER_MISMATCH'):
            restore_production(self.target, self.target, self.store, generation, old_fp, RUN, VERSION)
        self.assertEqual(self.target.writes, 1)
        restored = restore_production(self.target, self.target, self.store, generation, current_fp, RUN, VERSION)
        self.assertEqual(self.target.writes, 2)
        self.assertFalse(restored['pagesPublished'])
        self.assertEqual(fingerprint(tables(self.target.current)), old_fp)
        self.assertEqual(len(self.store.files), 6)
        public_html, _ = publish_verified(self.target, self.store, restored['manifest']['id'],
                                          NOW, self.template, previous)
        self.assertIn('noindex', public_html)

    def test_restore_wrong_generation_or_pre_restore_failure_never_writes(self):
        _, receipt = self.collect()
        expected = fingerprint(tables(self.target.current))
        generation = receipt['backup']['before']['id']
        self.store.fail_on_create = 4
        with self.assertRaisesRegex(DataError, 'BACKUP_WRITE'):
            restore_production(self.target, self.target, self.store, generation, expected, RUN, VERSION)
        self.assertEqual(self.target.writes, 1)
        self.store.fail_on_create = 0
        name, blob, mime = self.store.files[generation]
        self.store.files[generation] = (name, blob[:20], mime)
        with self.assertRaisesRegex(DataError, 'BACKUP_CORRUPT'):
            restore_production(self.target, self.target, self.store, generation, expected, RUN, VERSION)
        self.assertEqual(self.target.writes, 1)

    def test_legacy_defaults_and_preview_never_select_a_writer(self):
        env = dict(self.env, PRODUCTION_ENABLED='', COLLECT='false', PUBLISH='false')
        self.assertEqual(route(env), 'legacy')
        self.assertEqual(route(dict(env, GITHUB_EVENT_NAME='schedule')), 'legacy')
        self.assertEqual(route(dict(self.env, COLLECT='false', PUBLISH='false')), 'preview')
        self.assertEqual(route(dict(self.env, GITHUB_EVENT_NAME='schedule', COLLECT='', PUBLISH='')), 'collect')

    def test_guard_matrix_blocks_wrong_origin_target_disabled_and_second_writer(self):
        cases = [('PRODUCTION_ENABLED', 'false'), ('LEGACY_GAS_STOPPED', 'false'),
                 ('AUTOMATION_ENABLED', 'false'), ('GITHUB_REF', 'refs/heads/other'),
                 ('GITHUB_REPOSITORY', 'personal/other'), ('GITHUB_WORKFLOW_REF', 'other'),
                 ('GITHUB_EVENT_NAME', 'pull_request'), ('SHEET_ID', 'other'),
                 ('PRODUCTION_BACKUP_FOLDER_ID', 'other'), ('REHEARSE', 'true'),
                 ('RESTORE_ENABLED', 'true'), ('COMPARE', 'true'),
                 ('COMPARISON_END_DATE', '2026-08-24'), ('BACKUP_MANIFEST', 'manifest-file-123'),
                 ('GITHUB_SHA', 'invalid'), ('GITHUB_RUN_ID', 'not-a-number')]
        for key, value in cases:
            with self.subTest(key=key), self.assertRaises(DataError):
                configuration(dict(self.env, **{key:value}), False)

    def test_restore_is_manual_separate_nonpublishing_and_schedule_off(self):
        env = dict(self.env, COLLECT='false', PUBLISH='false', SCHEDULE_ENABLED='false',
                   RESTORE_ENABLED='true', RESTORE_GENERATION='backup-file-1234',
                   RESTORE_FINGERPRINT='1'*64)
        self.assertEqual(route(env), 'restore')
        for key, value in [('COLLECT','true'), ('PUBLISH','true'), ('SCHEDULE_ENABLED','true'),
                           ('RESTORE_ENABLED','false'), ('GITHUB_EVENT_NAME','schedule'),
                           ('RESTORE_FINGERPRINT',''), ('BACKUP_MANIFEST','manifest-file-123')]:
            with self.subTest(key=key), self.assertRaises(DataError):
                route(dict(env, **{key:value}))

    def test_verified_publish_requires_manifest_and_no_collector_token(self):
        env = dict(self.env, COLLECT='false')
        with self.assertRaisesRegex(DataError, 'VERIFIED_MANIFEST_REQUIRED'):
            configuration(env, False)
        env.update(BACKUP_MANIFEST='manifest-file-123', GOOGLE_READER_TOKEN='synthetic',
                   GOOGLE_BACKUP_TOKEN='synthetic')
        self.assertEqual(configuration(env)[0], 'publish')
        with self.assertRaisesRegex(DataError, 'PRODUCTION_AUTH'):
            configuration(self.env)

    def test_test_client_stays_blocked_and_production_client_rejects_other_sheets(self):
        with patch('cutover.urlopen') as network:
            for cls, target in [(TestSheetClient, PRODUCTION_SHEET_ID),
                                (ProductionSheetClient, 'test-sheet-only-1234567890')]:
                with self.assertRaisesRegex(DataError, 'TARGET_LOCK'):
                    cls(target, 'synthetic')
            network.assert_not_called()
        m = self.target.metadata()
        with self.assertRaisesRegex(DataError, 'TARGET_LOCK'):
            encode_backup(self.target.current, m, PRODUCTION_SHEET_ID, RUN, 'before', VERSION)

    def test_production_adapter_writes_only_fixed_sheet_values(self):
        client = ProductionSheetClient(PRODUCTION_SHEET_ID, 'synthetic')
        client.metadata = self.target.metadata
        payload = batch_request(fixture(113), client.metadata())
        with patch('cutover.urlopen', return_value=io.BytesIO(b'{}')) as network:
            client.batch_update(payload)
            request = network.call_args.args[0]
            self.assertEqual(request.method, 'POST')
            self.assertTrue(request.full_url.endswith(PRODUCTION_SHEET_ID + ':batchUpdate'))
            payload['requests'][0]['updateCells']['fields'] = '*'
            with self.assertRaisesRegex(DataError, 'WRITE_SCOPE'):
                client.batch_update(payload)
            self.assertEqual(network.call_count, 1)

    def test_source_changes_during_backup_abort_before_sheet_write(self):
        original_read = self.store.read
        def read_and_drift(key):
            result = original_read(key)
            self.target.current = fixture(111)
            return result
        self.store.read = read_and_drift
        with self.assertRaisesRegex(DataError, 'SOURCE_CHANGED'):
            self.collect()
        self.assertEqual(self.target.writes, 0)

    def test_template_failure_cannot_update_sheet(self):
        self.template = '<html>@@UNRESOLVED@@</html>'
        with self.assertRaises(DataError):
            self.collect()
        self.assertEqual(self.target.writes, 0)
        self.assertFalse(self.store.files)

    def test_rehearsal_cannot_ignore_production_inputs(self):
        base = dict(self.env, REHEARSE='true', REHEARSAL_ENABLED='true',
                    COLLECT='false', PUBLISH='false', COMPARE='false',
                    REHEARSAL_SHEET_ID='test-sheet-only-1234567890',
                    REHEARSAL_BACKUP_FOLDER_ID=BACKUP_FOLDER)
        rehearsal_configuration(base, False)
        for key, value in [('COLLECT','true'), ('BACKUP_MANIFEST','manifest-file-123'),
                           ('RESTORE_GENERATION','backup-file-123'), ('RESTORE_FINGERPRINT','1'*64)]:
            with self.subTest(key=key), self.assertRaisesRegex(DataError, 'REHEARSAL_MODE'):
                rehearsal_configuration(dict(base, **{key:value}), False)


if __name__ == '__main__':
    unittest.main()
