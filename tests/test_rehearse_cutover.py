from copy import deepcopy
from datetime import datetime
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from collect_gsc import fingerprint, tables
from cutover import DriveCreateRead, PRODUCTION_SHEET_ID
from dashboard import DataError, JST
from rehearse_cutover import REPOSITORY, WORKFLOW_REF, configuration, rehearse
from test_collect_gsc import ReplayAPI, fixture
from test_cutover import FakeStore, SHEET, VERSION, meta

ROOT = Path(__file__).resolve().parents[1]


class Source(ReplayAPI):
    def snapshot(self):
        return fixture(112)


class Target:
    def __init__(self):
        self.current = fixture(112)
        self.writes = 0

    def snapshot(self):
        return deepcopy(self.current)

    def metadata(self):
        return meta()

    def batch_update(self, payload):
        self.writes += 1
        names = {s['properties']['sheetId']: s['properties']['title'] for s in meta()['sheets']}
        blocks = []
        for item in payload['requests']:
            if 'updateCells' in item:
                cells = item['updateCells']
                rows = [[next(iter(c['userEnteredValue'].values())) for c in r['values']]
                        for r in cells['rows']]
                blocks.append({'range': names[cells['range']['sheetId']] + '!A:G', 'values': rows})
        self.current = {'valueRanges': blocks}


class Store(FakeStore):
    def verify_folder(self, expected):
        self.events.append('verify_folder')


class RehearsalTests(unittest.TestCase):
    def setUp(self):
        self.env = dict(GITHUB_EVENT_NAME='workflow_dispatch', GITHUB_REPOSITORY=REPOSITORY,
                        GITHUB_REF='refs/heads/main', GITHUB_WORKFLOW_REF=WORKFLOW_REF,
                        AUTOMATION_ENABLED='true', REHEARSAL_ENABLED='true', REHEARSE='true',
                        PUBLISH='false', COMPARE='false', COMPARISON_END_DATE='',
                        SHEET_ID=PRODUCTION_SHEET_ID, REHEARSAL_SHEET_ID=SHEET,
                        REHEARSAL_BACKUP_FOLDER_ID='backup-folder-12345678901234567890',
                        GITHUB_SHA=VERSION, GITHUB_RUN_ID='123456', GITHUB_RUN_ATTEMPT='1')
        self.source = Source(fixture(113))
        self.target, self.store = Target(), Store()

    def run_rehearsal(self, reader=None):
        return rehearse(self.source, self.target, reader or self.target, self.store, SHEET,
                        'rehearsal-test-001', VERSION, datetime(2026, 8, 27, tzinfo=JST),
                        (ROOT / 'template.html').read_text(encoding='utf-8'), None)

    def test_mode_rejects_schedule_publish_compare_or_disabled(self):
        for key, value in [('GITHUB_EVENT_NAME', 'schedule'), ('PUBLISH', 'true'),
                           ('COMPARE', 'true'), ('COMPARISON_END_DATE', '2026-08-24'),
                           ('REHEARSAL_ENABLED', 'false'), ('AUTOMATION_ENABLED', 'false'),
                           ('GITHUB_REF', 'refs/heads/other'), ('GITHUB_WORKFLOW_REF', 'other')]:
            with self.subTest(key=key), self.assertRaisesRegex(DataError, 'REHEARSAL_MODE'):
                configuration(dict(self.env, **{key: value}), False)

    def test_target_cannot_be_production_and_config_check_needs_no_tokens(self):
        self.assertEqual(configuration(self.env, False)[0], SHEET)
        with self.assertRaisesRegex(DataError, 'REHEARSAL_AUTH'):
            configuration(self.env)
        with self.assertRaisesRegex(DataError, 'REHEARSAL_TARGET'):
            configuration(dict(self.env, REHEARSAL_SHEET_ID=PRODUCTION_SHEET_ID), False)

    def test_complete_rehearsal_restores_original_and_saves_five_private_files(self):
        before = fingerprint(tables(self.target.snapshot()))
        result = self.run_rehearsal()
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(self.target.writes, 2)
        self.assertEqual(fingerprint(tables(self.target.snapshot())), before)
        self.assertEqual(len(self.store.files), 5)
        final = json.loads(self.store.files['file-5'][1])
        self.assertEqual(final['restore']['state'], 'restored_and_verified')
        self.assertNotIn('ログイン', json.dumps(result, ensure_ascii=False))
        self.assertFalse(result['pagesPublished'])

    def test_reader_failure_stops_before_any_data_write(self):
        class RestrictedReader:
            def snapshot(self):
                raise DataError('API_HTTP_403')
        with self.assertRaisesRegex(DataError, 'API_HTTP_403'):
            self.run_rehearsal(RestrictedReader())
        self.assertEqual(self.target.writes, 0)
        self.assertEqual(self.store.files, {})

    def test_bad_copy_stops_before_backup_or_update(self):
        self.target.current = fixture(111)
        with self.assertRaisesRegex(DataError, 'TEST_COPY_MISMATCH'):
            self.run_rehearsal()
        self.assertEqual(self.target.writes, 0)
        self.assertEqual(self.store.files, {})

    def test_update_failure_does_not_trigger_blind_restore(self):
        self.store.fail_on_create = 2
        with self.assertRaisesRegex(DataError, 'BACKUP_WRITE'):
            self.run_rehearsal()
        self.assertEqual(self.target.writes, 1)

    def test_no_new_days_is_not_reported_as_complete_rehearsal(self):
        self.source = Source(fixture(112))
        # This date makes the cutoff equal to the source's last day.
        with self.assertRaisesRegex(DataError, 'NO_CHANGE'):
            rehearse(self.source, self.target, self.target, self.store, SHEET,
                     'rehearsal-test-001', VERSION, datetime(2026, 8, 26, tzinfo=JST),
                     (ROOT / 'template.html').read_text(encoding='utf-8'), None)
        self.assertEqual(self.target.writes, 0)

    def test_backup_preflight_requires_expected_shared_drive_and_write_capability(self):
        folder = 'backup-folder-12345678901234567890'
        for drive, can_add in [('other', True), ('expected-drive', False)]:
            response = json.dumps({'id': folder, 'mimeType': 'application/vnd.google-apps.folder',
                                   'driveId': drive, 'capabilities': {'canAddChildren': can_add}}).encode()
            with patch('cutover.urlopen', return_value=io.BytesIO(response)):
                with self.assertRaisesRegex(DataError, 'BACKUP_FOLDER'):
                    DriveCreateRead(folder, 'synthetic-token').verify_folder('expected-drive')


if __name__ == '__main__':
    unittest.main()
