"""Test-Sheet-only cutover primitives used by the manual rehearsal CLI.

The live source Sheet is deliberately blocked until a separately reviewed release.
Backup payloads contain private search queries: never commit or upload as artifacts.
"""

from datetime import date, datetime, timezone
import gzip
import hashlib
import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

from collect_gsc import (HEADERS, ReadOnlyGoogle, batch_request, fingerprint,
                         snapshot_of, tables, validate_existing)
from dashboard import DataError, aggregate, json_text


PRODUCTION_SHEET_ID = '1XrYckoVd3vurkLfhdrF9y1AQbg7muJioSSza30kn6J0'
SCHEMA = 1


class DriveCreateRead:
    """Only create in one approved folder and read files in that same folder."""

    def __init__(self, folder_id, token):
        if not re.fullmatch(r'[A-Za-z0-9_-]{20,}', folder_id or '') or not token:
            raise DataError('BACKUP_CONFIG: 保存先と認証を確認してください')
        self.folder_id, self.token = folder_id, token

    def _request(self, url, method='GET', data=None, content_type=None):
        headers = {'Authorization': 'Bearer ' + self.token}
        if content_type:
            headers['Content-Type'] = content_type
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=60) as response:
                return response.read()
        except HTTPError as exc:
            status = exc.code
            exc.close()
            raise DataError(f'BACKUP_HTTP_{status}: 保存先の権限とAPIを確認してください') from None
        except (URLError, TimeoutError):
            # Create may have succeeded before timeout. No automatic retry.
            raise DataError('BACKUP_NETWORK_UNKNOWN: 保存先を確認し、同じ処理を再送しないでください') from None

    def create(self, name, content, mime):
        if (not re.fullmatch(r'[A-Za-z0-9_-]{8,80}-(before|after|pre-restore)\.json\.gz|'
                             r'[A-Za-z0-9_-]{8,80}-manifest\.json', name or '')
                or mime not in ('application/gzip', 'application/json')
                or not isinstance(content, bytes)):
            raise DataError('BACKUP_CREATE: 保存ファイルの形式が不正です')
        boundary = 'cutover-' + uuid4().hex
        meta = json_text({'name': name, 'parents': [self.folder_id], 'mimeType': mime}).encode()
        marker = ('--' + boundary + '\r\n').encode()
        body = (marker + b'Content-Type: application/json; charset=UTF-8\r\n\r\n' + meta + b'\r\n'
                + marker + ('Content-Type: ' + mime + '\r\n\r\n').encode() + content + b'\r\n'
                + ('--' + boundary + '--\r\n').encode())
        url = ('https://www.googleapis.com/upload/drive/v3/files?'
               + urlencode({'uploadType': 'multipart', 'supportsAllDrives': 'true',
                            'fields': 'id,name,parents,mimeType'}))
        raw_result = self._request(url, 'POST', body, 'multipart/related; boundary=' + boundary)
        try:
            result = json.loads(raw_result)
        except (UnicodeError, ValueError):
            raise DataError('BACKUP_RESPONSE: 保存結果を確認できません') from None
        if (result.get('name') != name or result.get('parents') != [self.folder_id]
                or result.get('mimeType') != mime
                or not re.fullmatch(r'[A-Za-z0-9_-]{10,}', result.get('id') or '')):
            raise DataError('BACKUP_RESPONSE: 保存先またはファイルIDを確認できません')
        return result['id']

    def read(self, file_id):
        if not re.fullmatch(r'[A-Za-z0-9_-]{10,}', file_id or ''):
            raise DataError('BACKUP_ID: ファイルIDが不正です')
        base = 'https://www.googleapis.com/drive/v3/files/' + quote(file_id, safe='')
        raw_info = self._request(base + '?' + urlencode({
            'supportsAllDrives': 'true', 'fields': 'id,parents,mimeType'}))
        try:
            info = json.loads(raw_info)
        except (UnicodeError, ValueError):
            raise DataError('BACKUP_METADATA: 保存先を確認できません') from None
        if info.get('id') != file_id or info.get('parents') != [self.folder_id]:
            raise DataError('BACKUP_FOLDER: 指定の保存先にないファイルです')
        return self._request(base + '?' + urlencode({'alt': 'media',
                                                       'supportsAllDrives': 'true'}))

    def verify_folder(self, expected_drive_id):
        url = ('https://www.googleapis.com/drive/v3/files/' + self.folder_id + '?'
               + urlencode({'supportsAllDrives': 'true',
                            'fields': 'id,mimeType,driveId,capabilities(canAddChildren)'}))
        raw = self._request(url)
        try:
            info = json.loads(raw)
        except (UnicodeError, ValueError):
            raise DataError('BACKUP_FOLDER: 保存先を確認できません') from None
        if (not isinstance(info, dict) or info.get('id') != self.folder_id
                or info.get('mimeType') != 'application/vnd.google-apps.folder'
                or info.get('driveId') != expected_drive_id
                or info.get('capabilities', {}).get('canAddChildren') is not True):
            raise DataError('BACKUP_FOLDER: 会社共有ドライブの保存先と作成権限を確認してください')


class TestSheetClient:
    """Separate, test-Sheet-only writer; the old collection CLI remains read-only."""

    def __init__(self, sheet_id, token):
        if sheet_id == PRODUCTION_SHEET_ID:
            raise DataError('TARGET_LOCK: この確認版は本番シートを更新できません')
        self.reader = ReadOnlyGoogle(sheet_id, token)
        self.sheet_id, self.token = sheet_id, token

    def snapshot(self):
        return self.reader.snapshot()

    def metadata(self):
        return self.reader.metadata()

    def batch_update(self, payload):
        tab_ids = set(_sheet_identity(self.metadata(), self.sheet_id).values())
        updated = set()
        for item in payload.get('requests', []):
            if set(item) == {'appendDimension'}:
                part = item['appendDimension']
                if (part.get('sheetId') not in tab_ids or part.get('dimension') != 'ROWS'
                        or type(part.get('length')) is not int or part['length'] < 1):
                    raise DataError('WRITE_SCOPE: 更新範囲を確認してください')
            elif set(item) == {'updateCells'}:
                part = item['updateCells']
                sheet = part.get('range', {}).get('sheetId')
                if (sheet not in tab_ids or sheet in updated
                        or part.get('fields') != 'userEnteredValue'
                        or part.get('range', {}).get('startRowIndex') != 0
                        or part.get('range', {}).get('startColumnIndex') != 0):
                    raise DataError('WRITE_SCOPE: 更新範囲を確認してください')
                updated.add(sheet)
            else:
                raise DataError('WRITE_SCOPE: 対象外のシート操作です')
        if updated != tab_ids or payload.get('includeSpreadsheetInResponse') is not False:
            raise DataError('WRITE_SCOPE: 3タブ一括更新を確認してください')
        body = json_text(payload).encode()
        req = Request('https://sheets.googleapis.com/v4/spreadsheets/' + self.sheet_id
                      + ':batchUpdate', data=body,
                      headers={'Authorization': 'Bearer ' + self.token,
                               'Content-Type': 'application/json'}, method='POST')
        try:
            with urlopen(req, timeout=60) as response:
                result = json.load(response)
        except HTTPError as exc:
            status = exc.code
            exc.close()
            raise DataError(f'WRITE_HTTP_{status}: 更新結果を読み戻してください') from None
        except (URLError, TimeoutError, UnicodeError, ValueError):
            raise DataError('WRITE_NETWORK_UNKNOWN: 更新結果を読み戻してください') from None
        if not isinstance(result, dict) or 'error' in result:
            raise DataError('WRITE_RESPONSE_UNKNOWN: 更新結果を読み戻してください')


def _digest(blob):
    return hashlib.sha256(blob).hexdigest()


def _sheet_identity(metadata, sheet_id):
    if metadata.get('spreadsheetId') != sheet_id or sheet_id == PRODUCTION_SHEET_ID:
        raise DataError('TARGET_LOCK: この確認版は本番シートを更新できません')
    if metadata.get('properties', {}).get('timeZone') != 'Asia/Tokyo':
        raise DataError('SHEET_TIMEZONE: 対象シートの設定を確認してください')
    props = {s.get('properties', {}).get('title'): s.get('properties', {})
             for s in metadata.get('sheets', [])}
    tab_ids = {}
    for name in HEADERS:
        prop = props.get(name, {})
        if prop.get('sheetType') != 'GRID' or type(prop.get('sheetId')) is not int:
            raise DataError('SHEET_METADATA: 対象3タブの構造を確認してください')
        tab_ids[name] = prop['sheetId']
    if len(set(tab_ids.values())) != 3:
        raise DataError('SHEET_METADATA: タブIDが重複しています')
    return tab_ids


def _checked_data(snapshot):
    if not isinstance(snapshot, dict):
        raise DataError('BACKUP_SCHEMA: 3タブのデータが不正です')
    data = tables(snapshot)
    latest = date.fromisoformat(data['日次'][-1][0])
    validate_existing(snapshot_of(data), latest)
    return data


def encode_backup(snapshot, metadata, sheet_id, run_id, stage, code_version, captured_at=None):
    """Build a full-history, versioned gzip backup with no secret or token fields."""
    if stage not in ('before', 'after', 'pre-restore'):
        raise DataError('BACKUP_STAGE: 保存段階が不正です')
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,80}', run_id or ''):
        raise DataError('RUN_ID: 実行IDが不正です')
    if not re.fullmatch(r'[0-9a-f]{7,40}', code_version or ''):
        raise DataError('CODE_VERSION: コード版が不正です')
    tab_ids = _sheet_identity(metadata, sheet_id)
    data = _checked_data(snapshot)
    normalized = snapshot_of(data)
    summary = aggregate(normalized, date.fromisoformat(data['日次'][-1][0]))
    document = {
        'schema': SCHEMA, 'sheetId': sheet_id, 'tabIds': tab_ids, 'timeZone': 'Asia/Tokyo',
        'headers': HEADERS, 'runId': run_id, 'stage': stage, 'codeVersion': code_version,
        'capturedAt': captured_at or datetime.now(timezone.utc).isoformat(),
        'rows': {name: len(data[name]) for name in HEADERS},
        'latestDay': data['日次'][-1][0],
        'totals': {key: summary[key] for key in ('weeks', 'impressions', 'clicks')},
        'fingerprint': fingerprint(data), 'snapshot': normalized,
    }
    raw = json_text(document).encode('utf-8')
    return gzip.compress(raw, mtime=0), document


def decode_backup(blob, metadata, sheet_id):
    """Reject damaged, mismatched or incomplete generations before use."""
    tab_ids = _sheet_identity(metadata, sheet_id)
    try:
        document = json.loads(gzip.decompress(blob))
    except (OSError, EOFError, UnicodeError, ValueError, TypeError):
        raise DataError('BACKUP_CORRUPT: バックアップを読み取れません') from None
    if (not isinstance(document, dict) or document.get('schema') != SCHEMA
            or document.get('sheetId') != sheet_id or document.get('tabIds') != tab_ids
            or document.get('timeZone') != 'Asia/Tokyo' or document.get('headers') != HEADERS):
        raise DataError('BACKUP_IDENTITY: 対象シートとバックアップの構造が一致しません')
    data = _checked_data(document.get('snapshot'))
    summary = aggregate(snapshot_of(data), date.fromisoformat(data['日次'][-1][0]))
    if (document.get('fingerprint') != fingerprint(data)
            or document.get('rows') != {name: len(data[name]) for name in HEADERS}
            or document.get('latestDay') != data['日次'][-1][0]
            or document.get('totals') != {key: summary[key] for key in ('weeks', 'impressions', 'clicks')}
            or not re.fullmatch(r'[A-Za-z0-9_-]{8,80}', document.get('runId') or '')
            or document.get('stage') not in ('before', 'after', 'pre-restore')
            or not re.fullmatch(r'[0-9a-f]{7,40}', document.get('codeVersion') or '')):
        raise DataError('BACKUP_INTEGRITY: バックアップの件数・ハッシュが一致しません')
    return document


def save_checked(store, name, blob, metadata, sheet_id):
    """Store must expose create(name, bytes, MIME) and read(file_id), never replace/delete."""
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,80}-(before|after|pre-restore)\.json\.gz', name):
        raise DataError('BACKUP_NAME: 保存名が不正です')
    file_id = store.create(name, blob, 'application/gzip')
    returned = store.read(file_id)
    if returned != blob:
        raise DataError('BACKUP_READBACK: 保存後の読み戻しが一致しません')
    document = decode_backup(returned, metadata, sheet_id)
    return {'id': file_id, 'name': name, 'sha256': _digest(blob),
            'fingerprint': document['fingerprint'], 'rows': document['rows']}


def save_manifest(store, run_id, sheet_id, code_version, before, after):
    """A non-sensitive receipt, stored create-only and read back like the data."""
    manifest = {'schema': SCHEMA, 'runId': run_id, 'sheetId': sheet_id,
                'codeVersion': code_version, 'before': before, 'after': after,
                'state': 'saved_and_verified'}
    blob = json_text(manifest).encode('utf-8')
    file_id = store.create(run_id + '-manifest.json', blob, 'application/json')
    if store.read(file_id) != blob:
        raise DataError('MANIFEST_READBACK: 実行記録を読み戻せません')
    return {'id': file_id, 'sha256': _digest(blob)}


def classify_sheet(snapshot, before_fingerprint, candidate_fingerprint):
    actual = fingerprint(tables(snapshot))
    if actual == candidate_fingerprint:
        return 'candidate'
    if actual == before_fingerprint:
        return 'before'
    return 'other'


def _guard_source(source, sheet_id, expected):
    metadata = source.metadata()
    _sheet_identity(metadata, sheet_id)
    snapshot = source.snapshot()
    _checked_data(snapshot)
    if fingerprint(tables(snapshot)) != expected:
        raise DataError('SOURCE_CHANGED: 元シートが変更されたため停止しました')
    return snapshot, metadata


def _verify_candidate_report(before, candidate, report):
    old, new = tables(before), tables(candidate)
    start, end = report.get('windowStart'), report.get('windowEnd')
    try:
        if (not isinstance(start, str) or not isinstance(end, str)
                or date.fromisoformat(start) > date.fromisoformat(end)):
            raise ValueError
    except ValueError:
        raise DataError('REPORT_WINDOW: 取得対象期間が不正です') from None
    if (report.get('sourceLatestBefore') != old['日次'][-1][0]
            or report.get('sourceLatestCandidate') != new['日次'][-1][0]
            or report.get('rowsBefore') != {name: len(old[name]) for name in HEADERS}
            or report.get('rowsCandidate') != {name: len(new[name]) for name in HEADERS}
            or old['日次'][0][0] != new['日次'][0][0]
            or new['日次'][-1][0] != end):
        raise DataError('REPORT_HISTORY: 候補の履歴・件数が比較結果と一致しません')
    changes = {}
    overlap_end = min(end, old['日次'][-1][0])
    for name in ('日次', 'キーワード'):
        def key(row):
            return (row[0], row[1]) if name == 'キーワード' else row[0]
        outside_old = sorted((row for row in old[name] if not start <= row[0] <= end),
                             key=lambda row: json_text(row))
        outside_new = sorted((row for row in new[name] if not start <= row[0] <= end),
                             key=lambda row: json_text(row))
        if outside_old != outside_new:
            raise DataError('HISTORY_CHANGED: 取得期間外の過去データが変わっています')
        a = {key(row): row for row in old[name] if start <= row[0] <= overlap_end}
        b = {key(row): row for row in new[name] if start <= row[0] <= overlap_end}
        changes[name] = {'added': len(b.keys() - a.keys()),
                         'removed': len(a.keys() - b.keys()),
                         'changed': sum(a[k] != b[k] for k in a.keys() & b.keys())}
    if report.get('existingPeriodDifferences') != changes or any(
            value for counts in changes.values() for value in counts.values()):
        raise DataError('REVIEW_REQUIRED: 既存期間の差を確認してください')
    summary = aggregate(candidate, date.fromisoformat(new['日次'][-1][0]))
    if any(report.get(key) != summary[key] for key in ('weeks', 'impressions', 'clicks')):
        raise DataError('REPORT_TOTALS: 集計が比較結果と一致しません')


def _apply(source, sheet_id, candidate, metadata, before_fingerprint, candidate_fingerprint):
    _guard_source(source, sheet_id, before_fingerprint)
    payload = batch_request(candidate, metadata)
    try:
        source.batch_update(payload)
    except Exception:
        # A timeout might occur after Sheets committed. Never repeat the write.
        try:
            state = classify_sheet(source.snapshot(), before_fingerprint, candidate_fingerprint)
        except Exception:
            raise DataError('WRITE_UNKNOWN: 書込結果を確認できません。再送しないでください') from None
        if state != 'candidate':
            raise DataError('WRITE_UNCONFIRMED_' + state.upper() + ': 再送せず状態を確認してください') from None
    try:
        state = classify_sheet(source.snapshot(), before_fingerprint, candidate_fingerprint)
    except Exception:
        raise DataError('WRITE_READBACK: 保存後の全件照合ができません') from None
    if state != 'candidate':
        raise DataError('WRITE_MISMATCH: 保存後の全件が候補と一致しません')


def update_test_sheet(source, store, sheet_id, candidate, report, run_id, code_version):
    """One test-Sheet write after validated before backup; returns non-sensitive receipt."""
    if report.get('reviewRequired') is not False or any(
            value for counts in report.get('existingPeriodDifferences', {}).values()
            for value in counts.values()):
        raise DataError('REVIEW_REQUIRED: 既存期間に差があるため停止しました')
    before_fingerprint = report.get('sourceFingerprint')
    if not re.fullmatch(r'[0-9a-f]{64}', before_fingerprint or ''):
        raise DataError('REPORT_FINGERPRINT: 比較結果を確認してください')
    candidate_data = _checked_data(candidate)
    candidate_fingerprint = fingerprint(candidate_data)
    if candidate_fingerprint == before_fingerprint:
        raise DataError('NO_CHANGE: 更新対象がありません')
    before, metadata = _guard_source(source, sheet_id, before_fingerprint)
    _verify_candidate_report(before, candidate, report)
    before_blob, _ = encode_backup(before, metadata, sheet_id, run_id, 'before', code_version)
    before_record = save_checked(store, run_id + '-before.json.gz', before_blob, metadata, sheet_id)
    _apply(source, sheet_id, candidate, metadata, before_fingerprint, candidate_fingerprint)
    after, after_metadata = _guard_source(source, sheet_id, candidate_fingerprint)
    after_blob, _ = encode_backup(after, after_metadata, sheet_id, run_id, 'after', code_version)
    after_record = save_checked(store, run_id + '-after.json.gz', after_blob, after_metadata, sheet_id)
    manifest_record = save_manifest(store, run_id, sheet_id, code_version,
                                    before_record, after_record)
    return {'runId': run_id, 'sheetId': sheet_id, 'before': before_record,
            'after': after_record, 'manifest': manifest_record, 'state': 'saved_and_verified'}


def restore_test_sheet(source, store, sheet_id, generation_id, run_id, code_version):
    """Restore a selected generation to a test Sheet after preserving its current state."""
    current = source.snapshot()
    metadata = source.metadata()
    _sheet_identity(metadata, sheet_id)
    current_data = _checked_data(current)
    current_fingerprint = fingerprint(current_data)
    selected_blob = store.read(generation_id)
    selected = decode_backup(selected_blob, metadata, sheet_id)
    target = selected['snapshot']
    target_fingerprint = selected['fingerprint']
    if current_fingerprint == target_fingerprint:
        raise DataError('NO_CHANGE: 復元先は選択した世代と同じです')
    before_blob, _ = encode_backup(current, metadata, sheet_id, run_id, 'pre-restore', code_version)
    safety_record = save_checked(store, run_id + '-pre-restore.json.gz', before_blob, metadata, sheet_id)
    _apply(source, sheet_id, target, metadata, current_fingerprint, target_fingerprint)
    _guard_source(source, sheet_id, target_fingerprint)
    return {'runId': run_id, 'sheetId': sheet_id, 'selectedGeneration': generation_id,
            'preRestore': safety_record, 'state': 'restored_and_verified'}
