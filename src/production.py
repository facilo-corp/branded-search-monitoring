"""Company collection, verified publication and explicitly selected restoration.

Deployment alone enables nothing; targets are fixed, credentials short-lived.
"""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sys

from collect_gsc import ReadOnlyGoogle, fingerprint, prepare, tables, validate_existing
from cutover import (DriveCreateRead, PRODUCTION_SHEET_ID, _SheetClient,
                     _sheet_identity, decode_backup, encode_backup, restore_sheet,
                     save_checked, save_manifest, update_sheet)
from dashboard import DataError, JST, aggregate, guard_history, json_text, render

REPOSITORY = 'facilo-corp/branded-search-monitoring'
WORKFLOW_REF = REPOSITORY + '/.github/workflows/update.yml@refs/heads/main'
DRIVE_ID = '0AGV3TNU5qZvGUk9PVA'
BACKUP_FOLDER = '1Aigwimya_m0rG-ZmNT93MYvt2FAtQWJH'
ROOT = Path(__file__).resolve().parents[1]


class ProductionSheetClient(_SheetClient):
    production = True


def route(env):
    """Resolve mode before credentials. New flags unset preserves legacy reads."""
    event = env.get('GITHUB_EVENT_NAME')
    if (env.get('GITHUB_REPOSITORY') != REPOSITORY
            or env.get('GITHUB_REF') != 'refs/heads/main'
            or env.get('GITHUB_WORKFLOW_REF') != WORKFLOW_REF
            or event not in ('schedule', 'workflow_dispatch')
            or env.get('AUTOMATION_ENABLED') != 'true'
            or (event == 'schedule' and env.get('SCHEDULE_ENABLED') != 'true')):
        raise DataError('PRODUCTION_MODE: 実行元と稼働設定を確認してください')
    publish = env.get('PUBLISH') == 'true'
    collect = env.get('COLLECT') == 'true'
    compare = env.get('COMPARE') == 'true'
    restore = bool(env.get('RESTORE_GENERATION'))
    manifest = bool(env.get('BACKUP_MANIFEST'))
    if env.get('REHEARSE') == 'true':
        raise DataError('PRODUCTION_MODE: 検証シートの操作とは同時実行できません')
    if compare:
        if event != 'workflow_dispatch' or publish or collect or restore or manifest or env.get('RESTORE_FINGERPRINT'):
            raise DataError('MODE_CONFLICT: 比較と書込・公開は同時指定できません')
        return 'compare'
    if env.get('COMPARISON_END_DATE'):
        raise DataError('MODE_CONFLICT: 取得終端の手動指定は比較専用です')
    if env.get('RESTORE_FINGERPRINT') and not restore:
        raise DataError('RESTORE_MODE: 復元元の指定がありません')
    if env.get('PRODUCTION_ENABLED') != 'true':
        if collect or restore or manifest:
            raise DataError('PRODUCTION_DISABLED: 本番取得はまだ有効になっていません')
        return 'legacy'
    if env.get('LEGACY_GAS_STOPPED') != 'true':
        raise DataError('LEGACY_WRITER_ACTIVE: 旧GAS停止の確認が必要です')
    if env.get('SHEET_ID') != PRODUCTION_SHEET_ID or env.get('PRODUCTION_BACKUP_FOLDER_ID') != BACKUP_FOLDER:
        raise DataError('PRODUCTION_TARGET: 承認済みのシート・保存先を指定してください')
    if restore:
        if (event != 'workflow_dispatch' or publish or collect or manifest
                or env.get('SCHEDULE_ENABLED') != 'false' or env.get('RESTORE_ENABLED') != 'true'
                or not re.fullmatch(r'[A-Za-z0-9_-]{10,}', env.get('RESTORE_GENERATION', ''))
                or not re.fullmatch(r'[0-9a-f]{64}', env.get('RESTORE_FINGERPRINT', ''))):
            raise DataError('RESTORE_MODE: 定期停止・復元世代・確認済み全件指紋を確認してください')
        return 'restore'
    if env.get('RESTORE_ENABLED') == 'true':
        raise DataError('RESTORE_MODE: 復元モードを終了してから通常運用に戻してください')
    if event == 'schedule' or collect:
        if manifest:
            raise DataError('MODE_CONFLICT: 取得と公開再実行は別の操作です')
        return 'collect'
    if publish:
        if not re.fullmatch(r'[A-Za-z0-9_-]{10,}', env.get('BACKUP_MANIFEST', '')):
            raise DataError('VERIFIED_MANIFEST_REQUIRED: 公開だけ再実行する場合は完了記録を指定してください')
        return 'publish'
    if manifest:
        raise DataError('MODE_CONFLICT: 完了記録は公開再実行時に指定してください')
    return 'preview'


def configuration(env, require_tokens=True):
    mode = route(env)
    if mode not in ('collect', 'publish', 'restore'):
        return mode, None, None
    revision = env.get('GITHUB_SHA', '')
    run, attempt = env.get('GITHUB_RUN_ID', ''), env.get('GITHUB_RUN_ATTEMPT', '')
    if not re.fullmatch(r'[0-9a-f]{40}', revision) or not run.isdecimal() or not attempt.isdecimal():
        raise DataError('PRODUCTION_RUN: 実行IDとコード版を確認してください')
    required = ['GOOGLE_READER_TOKEN', 'GOOGLE_BACKUP_TOKEN']
    if mode != 'publish':
        required.append('GOOGLE_COLLECTOR_TOKEN')
    if require_tokens and not all(env.get(k) for k in required):
        raise DataError('PRODUCTION_AUTH: 専用アカウントの認証を確認してください')
    return mode, 'production-' + run + '-' + attempt, revision


def same(reader, expected):
    result = reader.snapshot()
    if fingerprint(tables(result)) != expected:
        raise DataError('READER_MISMATCH: 保存内容と閲覧結果が一致しません')
    return result


def page(snapshot, now, template, previous):
    summary = aggregate(snapshot, now.date())
    guard_history(summary, previous)
    return render(summary, template, now)


def collect_production(source, reader, store, run_id, revision, now, template, previous):
    store.verify_folder(DRIVE_ID)
    _sheet_identity(source.metadata(), PRODUCTION_SHEET_ID, production=True)
    original = source.snapshot()
    validate_existing(original, now.date())
    same(reader, fingerprint(tables(original)))
    candidate, comparison = prepare(source.reader, original, now.date(), previous_html=previous)
    page(candidate, now, template, previous)
    saved = update_sheet(source, store, PRODUCTION_SHEET_ID, candidate, comparison,
                         run_id, revision, production=True)
    actual = same(reader, fingerprint(tables(candidate)))
    html = page(actual, now, template, previous)
    return html, {'mode': 'collect', 'state': 'saved_and_verified',
                  'sheetWrites': saved['sheetWrites'], 'backup': saved,
                  'sourceLatestDay': tables(actual)['日次'][-1][0]}


def publish_verified(reader, store, manifest_id, now, template, previous):
    """Only publish a complete backup generation identical to the current Sheet."""
    store.verify_folder(DRIVE_ID, require_write=False)
    metadata = reader.metadata()
    _sheet_identity(metadata, PRODUCTION_SHEET_ID, production=True)
    try:
        manifest = json.loads(store.read(manifest_id))
        if (manifest['schema'] != 1 or manifest['sheetId'] != PRODUCTION_SHEET_ID
                or manifest['state'] != 'saved_and_verified'
                or not re.fullmatch(r'production-[0-9]+-[0-9]+(?:-restore)?', manifest['runId'])
                or not re.fullmatch(r'[0-9a-f]{40}', manifest['codeVersion'])):
            raise ValueError
        record = manifest['after']
        blob = store.read(record['id'])
        if hashlib.sha256(blob).hexdigest() != record['sha256']:
            raise ValueError
        generation = decode_backup(blob, metadata, PRODUCTION_SHEET_ID, production=True)
        if (generation['stage'] != 'after' or generation['runId'] != manifest['runId']
                or generation['codeVersion'] != manifest['codeVersion']
                or generation['fingerprint'] != record['fingerprint']
                or generation['rows'] != record['rows']):
            raise ValueError
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise DataError('VERIFIED_MANIFEST_INVALID: 完了記録と保存内容を確認してください') from None
    current = same(reader, generation['fingerprint'])
    validate_existing(current, now.date())
    # An explicitly approved restoration may intentionally lower published counts.
    # Only its verified completion receipt permits that regression; freshness and
    # all data-integrity checks still apply. Normal collection keeps history guards.
    restored = manifest['runId'].endswith('-restore')
    return page(current, now, template, None if restored else previous), {
        'mode': 'publish', 'state': 'verified_existing', 'sheetWrites': 0,
        'manifestId': manifest_id, 'sourceLatestDay': generation['latestDay']}


def restore_production(source, reader, store, generation_id, expected, run_id, revision):
    store.verify_folder(DRIVE_ID)
    same(reader, expected)
    restoration = restore_sheet(source, store, PRODUCTION_SHEET_ID, generation_id,
                                run_id + '-restore', revision, production=True,
                                expected_fingerprint=expected)
    metadata = source.metadata()
    restored = source.snapshot()
    actual = same(reader, fingerprint(tables(restored)))
    restore_id = run_id + '-restore'
    blob, _ = encode_backup(actual, metadata, PRODUCTION_SHEET_ID,
                            restore_id, 'after', revision, production=True)
    after = save_checked(store, restore_id + '-after.json.gz', blob, metadata,
                         PRODUCTION_SHEET_ID, production=True)
    complete = save_manifest(store, restore_id, PRODUCTION_SHEET_ID, revision,
                             restoration['preRestore'], after)
    return {'mode': 'restore', 'state': 'restored_and_verified', 'sheetWrites': 1,
            'restore': restoration, 'after': after, 'manifest': complete, 'pagesPublished': False}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--check-config', action='store_true')
    args = parser.parse_args()
    try:
        env = os.environ
        mode, run, revision = configuration(env, require_tokens=not args.check_config)
        if args.check_config:
            publish = (mode not in ('compare', 'restore')
                       and (env['GITHUB_EVENT_NAME'] == 'schedule' or env.get('PUBLISH') == 'true'))
            if env.get('GITHUB_OUTPUT'):
                with open(env['GITHUB_OUTPUT'], 'a', encoding='utf-8') as f:
                    f.write('mode=' + mode + '\npublish=' + str(publish).lower() + '\n')
            print(json_text({'mode': mode, 'publish': publish, 'configurationOnly': True}))
            return 0
        if mode not in ('collect', 'publish', 'restore'):
            raise DataError('PRODUCTION_MODE: この操作は既存の読取処理で実行してください')
        reader = ReadOnlyGoogle(PRODUCTION_SHEET_ID, env['GOOGLE_READER_TOKEN'])
        store = DriveCreateRead(BACKUP_FOLDER, env['GOOGLE_BACKUP_TOKEN'])
        if mode == 'restore':
            source = ProductionSheetClient(PRODUCTION_SHEET_ID, env['GOOGLE_COLLECTOR_TOKEN'])
            receipt = restore_production(source, reader, store, env['RESTORE_GENERATION'],
                                         env['RESTORE_FINGERPRINT'], run, revision)
        else:
            now = datetime.now(JST)
            template = (ROOT / 'template.html').read_text(encoding='utf-8')
            previous = (ROOT / 'site/index.html').read_text(encoding='utf-8')
            if mode == 'collect':
                source = ProductionSheetClient(PRODUCTION_SHEET_ID, env['GOOGLE_COLLECTOR_TOKEN'])
                html, receipt = collect_production(source, reader, store, run, revision, now, template, previous)
            else:
                html, receipt = publish_verified(reader, store, env['BACKUP_MANIFEST'], now, template, previous)
            (ROOT / 'site/index.html').write_text(html, encoding='utf-8')
        print(json_text(receipt))
        if env.get('GITHUB_STEP_SUMMARY'):
            with open(env['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as f:
                fence = chr(96) * 3
                f.write('取得・保存の結果（公開・配信結果は後続工程を確認）\n\n')
                f.write(fence + 'json\n' + json_text(receipt) + '\n' + fence + '\n')
        return 0
    except DataError as exc:
        print(str(exc), file=sys.stderr)
    except Exception:
        print('PRODUCTION_FAILED: 状態を読み戻して確認してください。自動再送・自動復元は行いません',
              file=sys.stderr)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
