"""Manual rehearsal: update and restore a dedicated test Sheet, never publish.

No files containing source data are emitted. Durable receipts stay in company Drive.
This CLI is deliberately restricted to the existing main-branch workflow identity.
"""
import argparse
from datetime import datetime
import os
from pathlib import Path
import re
import sys

from collect_gsc import ReadOnlyGoogle, fingerprint, prepare, tables, validate_existing
from cutover import (DriveCreateRead, PRODUCTION_SHEET_ID, TestSheetClient,
                     restore_test_sheet, update_test_sheet)
from dashboard import DataError, JST, aggregate, json_text, render

DRIVE_ID = '0AGV3TNU5qZvGUk9PVA'
REPOSITORY = 'facilo-corp/branded-search-monitoring'
WORKFLOW_REF = REPOSITORY + '/.github/workflows/update.yml@refs/heads/main'
ROOT = Path(__file__).resolve().parents[1]


def configuration(env, require_tokens=True):
    if (env.get('GITHUB_EVENT_NAME') != 'workflow_dispatch'
            or env.get('GITHUB_REPOSITORY') != REPOSITORY
            or env.get('GITHUB_REF') != 'refs/heads/main'
            or env.get('GITHUB_WORKFLOW_REF') != WORKFLOW_REF
            or env.get('AUTOMATION_ENABLED') != 'true'
            or env.get('REHEARSAL_ENABLED') != 'true'
            or env.get('REHEARSE') != 'true'
            or env.get('PUBLISH') == 'true' or env.get('COMPARE') == 'true'
            or env.get('COMPARISON_END_DATE')):
        raise DataError('REHEARSAL_MODE: mainの手動検証だけを選択してください')
    sheet_id = env.get('REHEARSAL_SHEET_ID', '')
    folder_id = env.get('REHEARSAL_BACKUP_FOLDER_ID', '')
    if (env.get('SHEET_ID') != PRODUCTION_SHEET_ID
            or not re.fullmatch(r'[A-Za-z0-9_-]{20,}', sheet_id)
            or not re.fullmatch(r'[A-Za-z0-9_-]{20,}', folder_id)
            or sheet_id == PRODUCTION_SHEET_ID or sheet_id == folder_id or folder_id == DRIVE_ID):
        raise DataError('REHEARSAL_TARGET: 検証Sheetと専用保存先の設定を確認してください')
    revision = env.get('GITHUB_SHA', '')
    run, attempt = env.get('GITHUB_RUN_ID', ''), env.get('GITHUB_RUN_ATTEMPT', '')
    if not re.fullmatch(r'[0-9a-f]{40}', revision) or not run.isdecimal() or not attempt.isdecimal():
        raise DataError('REHEARSAL_RUN: 実行IDとコード版を確認してください')
    if require_tokens and not all(env.get(key) for key in
                                 ('GOOGLE_READER_TOKEN', 'GOOGLE_COLLECTOR_TOKEN', 'GOOGLE_BACKUP_TOKEN')):
        raise DataError('REHEARSAL_AUTH: 3アカウントの認証を確認してください')
    return sheet_id, folder_id, 'rehearsal-' + run + '-' + attempt, revision


def _same(client, expected, error):
    result = client.snapshot()
    if fingerprint(tables(result)) != expected:
        raise DataError(error + ': 全件照合が一致しないため停止しました')
    return result


def rehearse(source, target, reader, store, sheet_id, run_id, revision, now, template, previous_html):
    store.verify_folder(DRIVE_ID)
    original = source.snapshot()
    validate_existing(original, now.date())
    original_fp = fingerprint(tables(original))
    baseline = _same(target, original_fp, 'TEST_COPY_MISMATCH')
    _same(reader, original_fp, 'READER_BASELINE_MISMATCH')
    candidate, comparison = prepare(source, baseline, now.date(), previous_html=previous_html)
    # Validate the would-be page in memory before any update; never save to site/.
    render(aggregate(candidate, now.date()), template, now)
    _same(source, original_fp, 'PRODUCTION_CHANGED')
    updated = update_test_sheet(target, store, sheet_id, candidate, comparison, run_id, revision)
    candidate_fp = fingerprint(tables(candidate))
    observed = _same(reader, candidate_fp, 'READER_UPDATED_MISMATCH')
    render(aggregate(observed, now.date()), template, now)
    # Reverting the successful TEST update is an explicit part of this rehearsal.
    # Failure before this point stops for inspection; there is no blind rollback.
    restored = restore_test_sheet(target, store, sheet_id, updated['before']['id'],
                                  run_id + '-restore', revision)
    _same(reader, original_fp, 'READER_RESTORED_MISMATCH')
    _same(source, original_fp, 'PRODUCTION_CHANGED')
    public = {'mode': 'rehearsal', 'status': 'passed', 'testSheetUpdates': 2,
              'productionUnchanged': True, 'readerVerified': True,
              'backupAndRestoreVerified': True, 'candidatePageValidated': True,
              'pagesPublished': False, 'rowsBefore': comparison['rowsBefore'],
              'rowsCandidate': comparison['rowsCandidate']}
    private_receipt = dict(public, runId=run_id, codeVersion=revision,
                           update=updated, restore=restored)
    blob = json_text(private_receipt).encode()
    receipt_id = store.create(run_id + '-complete-manifest.json', blob, 'application/json')
    if store.read(receipt_id) != blob:
        raise DataError('REHEARSAL_RECEIPT: 検証完了記録を読み戻せません')
    return public


def main():
    parser = argparse.ArgumentParser(description='検証用Sheetだけで保存と復元を試す手動モード')
    parser.add_argument('--check-config', action='store_true')
    args = parser.parse_args()
    try:
        sheet_id, folder_id, run_id, revision = configuration(os.environ, not args.check_config)
        if args.check_config:
            print('手動検証の対象・モードを確認しました。')
            return 0
        collector_token = os.environ['GOOGLE_COLLECTOR_TOKEN']
        result = rehearse(
            ReadOnlyGoogle(PRODUCTION_SHEET_ID, collector_token),
            TestSheetClient(sheet_id, collector_token),
            ReadOnlyGoogle(sheet_id, os.environ['GOOGLE_READER_TOKEN']),
            DriveCreateRead(folder_id, os.environ['GOOGLE_BACKUP_TOKEN']),
            sheet_id, run_id, revision, datetime.now(JST),
            (ROOT / 'template.html').read_text(encoding='utf-8'),
            (ROOT / 'site/index.html').read_text(encoding='utf-8'))
        print(json_text(result))
        if os.environ.get('GITHUB_STEP_SUMMARY'):
            with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as output:
                output.write('検証用Sheetの保存→読戻し→復元→reader読取に成功。'
                             '元Sheetの全値は変化なし。Pages公開なし。\n')
        return 0
    except DataError as exc:
        print(str(exc), file=sys.stderr)
    except Exception:
        # API responses, queries and credentials must never reach public logs.
        print('REHEARSAL_STOPPED: 検証を停止しました。再送せず保存先と検証Sheetを確認してください',
              file=sys.stderr)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
