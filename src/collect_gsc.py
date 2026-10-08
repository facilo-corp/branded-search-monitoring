"""Collect and compare only. No Google write endpoint is reachable from this CLI.

Outputs contain private source data: keep them outside the repository/site/artifacts.
The atomic Sheets request is a review artifact, not an executable approval.
"""
import argparse
from collections import defaultdict
from datetime import date, datetime, timedelta
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from dashboard import DataError, JST, aggregate, date_key, guard_history, json_text, number

SITE = 'sc-domain:facilo.jp'
REGEX = 'facilo|ファシロ'
HEADERS = {
    '週次': ['week_start', 'impressions', 'clicks', 'days'],
    '日次': ['date', 'impressions', 'clicks', 'dow'],
    'キーワード': ['date', 'query', 'impressions', 'clicks', 'ctr_%', 'type', 'modifier'],
}
DOW = ['月', '火', '水', '木', '金', '土', '日']
GSC_URL = 'https://www.googleapis.com/webmasters/v3/sites/' + quote(SITE, safe='') + '/searchAnalytics/query'


def dates(start, end):
    day = date.fromisoformat(start)
    stop = date.fromisoformat(end)
    while day <= stop:
        yield day.isoformat()
        day += timedelta(days=1)


class ReadOnlyGoogle:
    def __init__(self, sheet_id, token):
        if not re.fullmatch(r'[A-Za-z0-9_-]{20,}', sheet_id or '') or not token:
            raise DataError('CONFIG: 認証と対象シートを確認してください')
        self.sheet_id, self.token = sheet_id, token
        self.sheet_url = 'https://sheets.googleapis.com/v4/spreadsheets/' + sheet_id

    def request(self, url, payload=None):
        # Explicit allowlist: POST only performs a Search Console query (read).
        if payload is not None and url != GSC_URL:
            raise DataError('WRITE_FORBIDDEN: この確認版は外部へ書き込みません')
        if payload is None and not (url == self.sheet_url or url.startswith(self.sheet_url + '?')
                                    or url.startswith(self.sheet_url + '/values:batchGet?')):
            raise DataError('ENDPOINT_FORBIDDEN: 対象外の接続先です')
        req = Request(url, data=json_text(payload).encode() if payload is not None else None,
                      headers={'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'},
                      method='POST' if payload is not None else 'GET')
        for attempt in range(3):
            try:
                with urlopen(req, timeout=45) as response:
                    value = json.load(response)
                if not isinstance(value, dict) or 'error' in value:
                    raise DataError('API_RESPONSE: 応答形式を確認してください')
                return value
            except HTTPError as exc:
                status = exc.code
                exc.close()
                if status not in (429, 500, 502, 503, 504) or attempt == 2:
                    raise DataError(f'API_HTTP_{status}: 取得を停止しました。権限とAPIの状態を確認してください') from None
            except (URLError, TimeoutError):
                if attempt == 2:
                    raise DataError('API_NETWORK: 通信エラーのため取得を停止しました') from None
            except (json.JSONDecodeError, UnicodeError):
                raise DataError('API_JSON: 不正な応答のため取得を停止しました') from None
            time.sleep(2 ** attempt)

    def snapshot(self):
        query = urlencode({'ranges': ["'週次'!A:D", "'日次'!A:D", "'キーワード'!A:G"],
                           'valueRenderOption': 'UNFORMATTED_VALUE',
                           'dateTimeRenderOption': 'SERIAL_NUMBER'}, doseq=True)
        return self.request(self.sheet_url + '/values:batchGet?' + query)

    def metadata(self):
        fields = 'spreadsheetId,properties(timeZone),sheets(properties(sheetId,title,sheetType,gridProperties))'
        return self.request(self.sheet_url + '?' + urlencode({'fields': fields}))

    def query(self, payload):
        return self.request(GSC_URL, payload)


def query_rows(api, start, end, dimensions, filtered=True, limit=25000):
    if not 1 <= limit <= 25000:
        raise DataError('PAGE_SIZE: 取得行数の指定が不正です')
    rows, seen = [], set()
    for page in range(10):
        payload = dict(startDate=start, endDate=end, dimensions=dimensions,
                       type='web', aggregationType='auto', dataState='final',
                       rowLimit=limit, startRow=page * limit)
        if filtered:
            payload['dimensionFilterGroups'] = [{'groupType': 'and', 'filters': [
                {'dimension': 'query', 'operator': 'includingRegex', 'expression': REGEX}]}]
        result = api.query(payload)
        batch = result.get('rows', [])
        if not isinstance(batch, list) or len(batch) > limit:
            raise DataError('API_ROWS: 取得行の形式が不正です')
        if result.get('metadata', {}).get('first_incomplete_date'):
            raise DataError('INCOMPLETE_DATA: 未確定データを検出しました')
        for row in batch:
            if not isinstance(row, dict) or not isinstance(row.get('keys'), list) or len(row['keys']) != len(dimensions):
                raise DataError('API_KEYS: 取得キーの形式が不正です')
            keys = row['keys']
            if any(not isinstance(k, str) for k in keys):
                raise DataError('API_KEYS: 取得キーの型が不正です')
            key = tuple(keys)
            if key in seen:
                raise DataError('API_DUPLICATE: 取得結果が重複しています')
            day = date_key(keys[0])
            if not start <= day <= end:
                raise DataError('API_DATE: 取得期間外の行を検出しました')
            imp, clicks = number(row.get('impressions')), number(row.get('clicks'))
            if clicks > imp:
                raise DataError('API_COUNTS: クリック数と表示回数が矛盾しています')
            if len(keys) == 2 and not re.search(REGEX, keys[1], re.I):
                raise DataError('API_QUERY: 指名検索以外の行を検出しました')
            seen.add(key)
            rows.append(dict(keys=keys, impressions=imp, clicks=clicks))
        if len(batch) < limit:
            return rows
    raise DataError('PAGE_LIMIT: 行の取得上限に達したため、不完全な結果を保存しません')


def classify(query):
    alpha, kana = 'facilo' in query.lower(), 'ファシロ' in query
    modifier = re.sub(r'\s+', ' ', re.sub(REGEX, '', query, flags=re.I)).strip()
    kind = ('both' if alpha and kana else ('alpha' if alpha else 'kana') +
            ('_compound' if modifier else '_exact'))
    return kind, modifier or '-'


def keyword_row(day, query, imp, clicks):
    # Match GAS Math.round(ctr * 10000) / 100, avoiding Python banker's rounding.
    ctr = math.floor(clicks / imp * 10000 + 0.5) / 100 if imp else 0
    return [day, query, imp, clicks, ctr, *classify(query)]


def weekly_rows(daily):
    weeks = defaultdict(list)
    for row in daily:
        dt = date.fromisoformat(row[0])
        weeks[(dt - timedelta(days=dt.weekday())).isoformat()].append(row)
    incomplete = sorted(w for w, rs in weeks.items() if len(rs) < 7)
    keep = incomplete[-1] if incomplete else None
    return [[w, sum(r[1] for r in rs), sum(r[2] for r in rs), len(rs)]
            for w, rs in sorted(weeks.items()) if len(rs) == 7 or w == keep]


def tables(snapshot):
    out = {}
    for block in snapshot.get('valueRanges', []):
        name = block.get('range', '').split('!')[0].strip("'")
        values = block.get('values', [])
        if name not in HEADERS or name in out or not values or values[0] != HEADERS[name]:
            raise DataError('SCHEMA: 3タブの見出しと取得範囲を確認してください')
        result, seen = [], set()
        for row in values[1:]:
            if not any(v not in ('', None) for v in row):
                continue
            if len(row) != len(HEADERS[name]):
                raise DataError('ROW_WIDTH: 空欄または想定外の列を検出しました')
            r = list(row)
            r[0] = date_key(r[0])
            key = (r[0], r[1]) if name == 'キーワード' else r[0]
            if key in seen:
                raise DataError('DUPLICATE: 日付または検索語の重複を検出しました')
            seen.add(key)
            result.append(r)
        out[name] = result
    if set(out) != set(HEADERS) or not out['日次']:
        raise DataError('MISSING_SHEET: 必要な3タブが揃っていません')
    out['日次'].sort(key=lambda r: r[0])
    out['週次'].sort(key=lambda r: r[0])
    out['キーワード'].sort(key=lambda r: (r[0], -number(r[2])))
    return out


def snapshot_of(data):
    return {'valueRanges': [{'range': "'" + name + "'!A1:" + ('G' if name == 'キーワード' else 'D'),
                             'values': [HEADERS[name]] + data[name]} for name in HEADERS]}


def validate_existing(snapshot, today):
    data = tables(snapshot)
    # Check shape/counts/history even if the old source is stale. The new source
    # must pass the current-date freshness check after collection.
    latest = date.fromisoformat(data['日次'][-1][0])
    if latest > today:
        raise DataError('FUTURE_DATE: 元シートに未来の日付があります')
    aggregate(snapshot_of(data), latest)
    if data['週次'] != weekly_rows(data['日次']):
        raise DataError('WEEKLY_MISMATCH: 元シートの日次と週次が一致しません')
    for row in data['日次']:
        if row[3] != DOW[date.fromisoformat(row[0]).weekday()]:
            raise DataError('DOW_MISMATCH: 日次の曜日が一致しません')
    for row in data['キーワード']:
        expected = keyword_row(row[0], row[1], row[2], row[3])
        if (row[5:] != expected[5:] or isinstance(row[4], bool)
                or not isinstance(row[4], (int, float)) or not math.isfinite(row[4])
                or abs(row[4] - expected[4]) > 0.011):
            raise DataError('CLASSIFICATION_MISMATCH: 元シートの分類またはCTRを確認してください')
    return data


def collection_window(data, today, end=None):
    # Match the existing GAS cutoff (JST calendar day minus 3); GSC day labels
    # themselves remain Pacific Time, without any conversion of stored dates.
    cutoff = today - timedelta(days=3)
    finish = date.fromisoformat(date_key(end)) if end else cutoff
    latest = date.fromisoformat(data['日次'][-1][0])
    first = date.fromisoformat(data['日次'][0][0])
    if finish > cutoff or finish < latest:
        raise DataError('CUTOFF: 取得終端は蓄積最終日以降かつ3日前までが必要です')
    start = max(first, min(finish - timedelta(days=27), latest + timedelta(days=1)))
    if (finish - start).days > 89:
        raise DataError('LONG_GAP: 90日を超える取得は復旧計画を確認してください')
    return start.isoformat(), finish.isoformat()


def collect(api, start, end):
    expected_days = set(dates(start, end))
    daily = {r['keys'][0]: [r['impressions'], r['clicks']]
             for r in query_rows(api, start, end, ['date'])}
    missing = expected_days - set(daily)
    if missing:
        available = {r['keys'][0] for r in query_rows(api, start, end, ['date'], filtered=False)}
        if missing - available:
            raise DataError('MISSING_FINAL_DAY: 確定データのない日をゼロとして保存しません')
        for day in missing:
            daily[day] = [0, 0]
    queries = []
    # Day-sized queries reduce GSC's internal top-row truncation risk. Equality
    # with the independently fetched daily totals remains mandatory.
    for day in sorted(expected_days):
        rows = query_rows(api, day, day, ['date', 'query'])
        total = [sum(r[k] for r in rows) for k in ('impressions', 'clicks')]
        if total != daily[day]:
            raise DataError('GSC_TOTAL_MISMATCH: 日次とキーワード内訳が一致しないため停止しました')
        queries.extend(keyword_row(day, r['keys'][1], r['impressions'], r['clicks']) for r in rows)
    return [[d, *daily[d], DOW[date.fromisoformat(d).weekday()]] for d in sorted(daily)], queries


def prepare(api, snapshot, today, end=None, previous_html=None):
    before = validate_existing(snapshot, today)
    start, end = collection_window(before, today, end)
    daily, queries = collect(api, start, end)
    candidate = {
        '日次': [r for r in before['日次'] if not start <= r[0] <= end] + daily,
        'キーワード': [r for r in before['キーワード'] if not start <= r[0] <= end] + queries,
    }
    candidate['日次'].sort(key=lambda r: r[0])
    candidate['キーワード'].sort(key=lambda r: (r[0], -r[2]))
    candidate['週次'] = weekly_rows(candidate['日次'])
    result = snapshot_of(candidate)
    agg = aggregate(result, today)
    guard_history(agg, previous_html)
    if candidate['日次'][0][0] != before['日次'][0][0]:
        raise DataError('HISTORY_LOSS: 過去の蓄積が失われています')
    report = {'mode': 'compare', 'externalWrites': 0, 'windowStart': start, 'windowEnd': end,
              'sourceLatestBefore': before['日次'][-1][0], 'sourceLatestCandidate': end,
              'preservedDaysBeforeWindow': sum(r[0] < start for r in before['日次']),
              'rowsBefore': {n: len(v) for n, v in before.items()},
              'rowsCandidate': {n: len(v) for n, v in candidate.items()},
              'weeks': agg['weeks'], 'impressions': agg['impressions'], 'clicks': agg['clicks']}
    overlap_end = min(end, before['日次'][-1][0])
    changes = {}
    for name in ('日次', 'キーワード'):
        def index(rows):
            return {(r[0], r[1]) if name == 'キーワード' else r[0]:
                    r[2:4] if name == 'キーワード' else r[1:3]
                    for r in rows if start <= r[0] <= overlap_end}
        old, new = index(before[name]), index(candidate[name])
        changes[name] = {'added': len(new.keys() - old.keys()), 'removed': len(old.keys() - new.keys()),
                         'changed': sum(old[k] != new[k] for k in old.keys() & new.keys())}
    report['existingPeriodDifferences'] = changes
    report['reviewRequired'] = any(v for counts in changes.values() for v in counts.values())
    report['sourceFingerprint'] = fingerprint(before)
    return result, report


def fingerprint(data):
    # Ignore range labels, row ordering and date display type, but detect every
    # value edit in the three managed tables.
    def numeric_normalization(row):
        return [int(v) if isinstance(v, float) and math.isfinite(v) and v.is_integer() else v for v in row]
    canonical = {name: sorted([numeric_normalization(r) for r in rows],
                             key=lambda r: json_text(r[:2] if name == 'キーワード' else r[:1]))
                 for name, rows in data.items()}
    return hashlib.sha256(json_text(canonical).encode()).hexdigest()


def batch_request(candidate, metadata):
    """Create one atomic review payload; never send it. Preserve formatting."""
    data = tables(candidate)
    if metadata.get('properties', {}).get('timeZone') != 'Asia/Tokyo':
        raise DataError('SHEET_TIMEZONE: シートのタイムゾーンを確認してください')
    props = {s['properties']['title']: s['properties'] for s in metadata.get('sheets', [])}
    requests = []
    for name, header in HEADERS.items():
        prop = props.get(name, {})
        if prop.get('sheetType') != 'GRID' or not isinstance(prop.get('sheetId'), int):
            raise DataError('SHEET_METADATA: 対象タブの構造を確認してください')
        grid = prop['gridProperties']
        rows = [header] + data[name]
        if grid['columnCount'] < len(header):
            raise DataError('SHEET_COLUMNS: 対象タブの列数を確認してください')
        if len(rows) > grid['rowCount']:
            requests.append({'appendDimension': {'sheetId': prop['sheetId'], 'dimension': 'ROWS',
                                                  'length': len(rows) - grid['rowCount']}})
        cells = [{'values': [{'userEnteredValue': {'stringValue': v} if isinstance(v, str)
                              else {'numberValue': v}} for v in r]} for r in rows]
        requests.append({'updateCells': {'range': {'sheetId': prop['sheetId'], 'startRowIndex': 0,
                          'endRowIndex': max(grid['rowCount'], len(rows)), 'startColumnIndex': 0,
                          'endColumnIndex': len(header)}, 'rows': cells, 'fields': 'userEnteredValue'}})
    return {'requests': requests, 'includeSpreadsheetInResponse': False}


def private_directory(path):
    root = Path(__file__).resolve().parents[1]
    target = path.resolve()
    if target == root or root in target.parents:
        raise DataError('PRIVATE_OUTPUT: 元データはリポジトリ外へ保存してください')
    target.mkdir(parents=True, exist_ok=True)
    return target


def main():
    parser = argparse.ArgumentParser(description='書き込みなしの取得・照合。権限はGSCとSheetsの閲覧のみ。')
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--end-date', help='同期間比較用の終端。既定はJSTの3日前')
    args = parser.parse_args()
    try:
        folder = private_directory(args.output_dir)
        # Refuse nonempty directories so a failed run cannot masquerade as an
        # older successful candidate.
        if any(folder.iterdir()):
            raise DataError('OUTPUT_EXISTS: 実行ごとに空の保存先を指定してください')
        api = ReadOnlyGoogle(os.environ.get('SHEET_ID'), os.environ.get('GOOGLE_ACCESS_TOKEN'))
        snapshot = api.snapshot()
        metadata = api.metadata()
        today = datetime.now(JST).date()
        previous = (Path(__file__).resolve().parents[1] / 'site/index.html').read_text(encoding='utf-8')
        candidate, report = prepare(api, snapshot, today, args.end_date, previous)
        if fingerprint(tables(api.snapshot())) != report['sourceFingerprint']:
            raise DataError('SOURCE_CHANGED: 取得中に元シートが変わったため、再確認が必要です')
        payload = batch_request(candidate, metadata)
        # These outputs are local candidates, NOT persistent company backups.
        for name, value in [('source-before.json', snapshot), ('snapshot-candidate.json', candidate),
                            ('sheet-request-review-only.json', payload), ('comparison.json', report)]:
            (folder / name).write_text(json_text(value), encoding='utf-8')
        print(json_text(report))
        return 2 if report['reviewRequired'] else 0
    except DataError as exc:
        print(str(exc), file=sys.stderr)
    except (KeyError, TypeError, ValueError, OSError, OverflowError, AttributeError):
        print('COLLECTION_FAILED: 応答と入力形式を確認してください。外部書き込みはありません', file=sys.stderr)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
