"""Read-only aggregation and static rendering. Python standard library only."""
import argparse
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
import hashlib
from html import escape
from html.parser import HTMLParser
import json
import math
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
JST = timezone(timedelta(hours=9))
MAX_DATA_AGE_DAYS = 14
RULES = [
    ('exact', 'ブランド指名', 'facilo / ファシロ のみ', r'^$'),
    ('navigation', 'ナビゲーション', 'ログイン・ログオン', r'ログイン|ログオン|log[ -]?in|sign[ -]?in'),
    ('recruitment', '採用', '採用・求人・給与・職場情報', r'採用|求人|年収|転職|募集|給与|給料|就職|新卒|中途|インターン|リクルート|recruit|career|働き|残業|福利|退職|離職|面接|選考|エンジニア|社員'),
    ('reputation', '評判', '評判・口コミ', r'評判|口コミ|クチコミ|レビュー|review'),
    ('news', 'ニュース', '資金調達・ニュース・note', r'資金調達|調達|ニュース|news|プレス|prtimes|note|シリーズ|出資|資本|提携|プレスリリース'),
    ('product', 'プロダクト', 'サービス・料金・機能', r'不動産|料金|価格|費用|レインズ|reins|crm|クラウド|システム|サービス|機能|物件|購入|売却|賃貸|事業用|マーケットプレイス|marketplace|マップ|map|デモ|導入|使い方|アプリ|アカウント|契約|解約|連携|ai'),
    ('corporate', '企業調査', '会社情報・代表名', r'株式会社|会社|とは|代表|市川|資本金|売上|上場|法人|所在地|住所|電話|企業|創業|設立|社長|ceo|従業員数|ファシーロ'),
    ('other', 'その他', '上記に当てはまらない検索', r'.*'),
]


class DataError(ValueError):
    """Messages must never include a search query, credential or raw row."""


def json_text(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def script_json(value):
    return (json_text(value).replace('&', '\\u0026').replace('<', '\\u003c')
            .replace('>', '\\u003e').replace('\u2028', '\\u2028').replace('\u2029', '\\u2029'))


def date_key(value):
    try:
        if isinstance(value, bool):
            raise ValueError
        if isinstance(value, (int, float)):
            if not math.isfinite(value) or value != int(value):
                raise ValueError
            result = date(1899, 12, 30) + timedelta(days=int(value))
        elif isinstance(value, str) and re.fullmatch(r'\d{4}[-/]\d{2}[-/]\d{2}', value):
            result = date.fromisoformat(value.replace('/', '-'))
        else:
            raise ValueError
        if result.year < 2000:
            raise ValueError
        return result.isoformat()
    except (ValueError, TypeError, OverflowError):
        raise DataError('INVALID_DATE: 日付の形式を確認してください') from None


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DataError('INVALID_NUMBER: 件数に数値以外が含まれています')
    if not math.isfinite(value) or value < 0 or int(value) != value:
        raise DataError('INVALID_NUMBER: 件数は0以上の整数が必要です')
    return int(value)


def read_rows(snapshot):
    rows = {}
    expected = {'日次': ['date', 'impressions', 'clicks', 'dow'],
                'キーワード': ['date', 'query', 'impressions', 'clicks', 'ctr_%', 'type', 'modifier']}
    for block in snapshot.get('valueRanges', []):
        sheet = block.get('range', '').split('!')[0].strip("'")
        if sheet not in expected:
            continue
        values = block.get('values', [])
        if sheet in rows or not values or values[0] != expected[sheet]:
            raise DataError('SCHEMA_CHANGED: シートの見出し・取得範囲を確認してください')
        rows[sheet] = [row for row in values[1:] if any(v not in ('', None) for v in row)]
    if set(rows) != set(expected):
        raise DataError('MISSING_SHEET: 必要な2タブを取得できませんでした')
    return rows


def aggregate(snapshot, today):
    source = read_rows(snapshot)
    daily, queries = {}, {}
    for sheet, target in [('日次', daily), ('キーワード', queries)]:
        for row in source[sheet]:
            if len(row) < (3 if sheet == '日次' else 4):
                raise DataError('SHORT_ROW: 必須セルが空欄です')
            day = date_key(row[0])
            if sheet == '日次':
                key, counts = day, [number(row[1]), number(row[2])]
            else:
                if not isinstance(row[1], str) or not re.search(r'facilo|ファシロ', row[1], re.I):
                    raise DataError('INVALID_QUERY: 指名検索以外の行があります')
                key, counts = (day, row[1]), [number(row[2]), number(row[3])]
            if counts[1] > counts[0]:
                raise DataError('INVALID_COUNTS: クリック数が表示回数を超えています')
            if key in target:
                raise DataError('DUPLICATE: 日付または日付×検索語の重複を検出しました')
            target[key] = counts
    if not daily or not queries:
        raise DataError('EMPTY_DATA: データが空です')
    days = sorted(daily)
    first, latest = map(date.fromisoformat, (days[0], days[-1]))
    if latest > today:
        raise DataError('FUTURE_DATE: 未来の日付が含まれています')
    if (today - latest).days > MAX_DATA_AGE_DAYS:
        raise DataError('STALE_DATA: 元データの最終日から14日を超えています。GASの実行結果を確認してください')
    if (latest - first).days + 1 != len(days):
        raise DataError('MISSING_DAY: 日次データに欠落日があります')
    keyword_totals = defaultdict(lambda: [0, 0])
    for (day, _), counts in queries.items():
        for i in range(2):
            keyword_totals[day][i] += counts[i]
    if set(keyword_totals) - set(daily) or any(keyword_totals.get(d, [0, 0]) != c for d, c in daily.items()):
        raise DataError('TOTAL_MISMATCH: 日次と検索語の集計が一致しません。公開せず取得元を確認してください')
    weeks = defaultdict(list)
    for day in days:
        dt = date.fromisoformat(day)
        weeks[(dt - timedelta(days=dt.weekday())).isoformat()].append(day)
    weekly = [[week, sum(daily[d][0] for d in ds), sum(daily[d][1] for d in ds)]
              for week, ds in sorted(weeks.items()) if len(ds) == 7]
    if len(weekly) < 15:
        raise DataError('SHORT_HISTORY: 指標比較には15完了週以上が必要です')
    start = weekly[0][0]
    end = (date.fromisoformat(weekly[-1][0]) + timedelta(days=6)).isoformat()
    cats = {k: dict(key=k, label=label, desc=desc, imp=0, clicks=0, pct=0, top=[], color='--cat-' + k)
            for k, label, desc, _ in RULES}
    modifiers = defaultdict(Counter)
    for (day, query), counts in queries.items():
        if not start <= day <= end:
            continue
        mod = re.sub(r'\s+', ' ', re.sub(r'facilo|ファシロ', '', query, flags=re.I)).strip().lower()
        key = next(k for k, _, _, pattern in RULES if re.search(pattern, mod, re.I))
        cats[key]['imp'] += counts[0]
        cats[key]['clicks'] += counts[1]
        if mod:
            modifiers[key][mod] += counts[0]
    impressions, clicks = (sum(w[i] for w in weekly) for i in (1, 2))
    if not impressions or any(sum(c[field] for c in cats.values()) != n
                              for field, n in [('imp', impressions), ('clicks', clicks)]):
        raise DataError('CATEGORY_MISMATCH: カテゴリ合計が一致しません')
    for key, cat in cats.items():
        cat['pct'] = round(cat['imp'] / impressions * 100, 1)
        cat['top'] = [m for m, _ in modifiers[key].most_common(3)]
        if key in ('navigation', 'reputation'):
            cat['ctr'] = f"CTR {cat['clicks'] / cat['imp'] * 100:.1f}%" if cat['imp'] else 'CTR —'
    return dict(weekly=weekly, categories=sorted(cats.values(), key=lambda c: -c['imp']),
                periodStart=start, periodEnd=end, sourceLatestDay=days[-1], weeks=len(weekly),
                impressions=impressions, clicks=clicks,
                excludedStartDays=sum(d < start for d in days), excludedEndDays=sum(d > end for d in days))


def guard_history(data, previous_html):
    if not previous_html:
        return
    old = extract_metadata(previous_html)
    if data['periodStart'] != old['periodStart'] or data['periodEnd'] < old['periodEnd'] or data['weeks'] < old['weeks']:
        raise DataError('HISTORY_LOSS: 過去データまたは最新の週が失われています')


def extract_metadata(html):
    found = re.search(r'<script id="dashboard-meta" type="application/json">(.*?)</script>', html, re.S)
    if not found:
        raise DataError('INVALID_PREVIOUS_SITE: 前の公開ファイルのメタデータを確認してください')
    return json.loads(found.group(1))


def render(data, template, built_at):
    period = f"{data['periodStart'].replace('-', '/')} 〜 {data['periodEnd'].replace('-', '/')}"
    cats = {c['key']: c for c in data['categories']}
    nav, rep = cats['navigation'], cats['reputation']
    note = (f'<strong>ナビゲーション（ログイン）</strong>：{nav["imp"]:,} imp・{nav["ctr"]}。ログイン目的の検索傾向を見る補助指標です。<br>'
            f'<strong>評判</strong>：{rep["imp"]:,} imp・{rep["ctr"]}。検索結果での比較・情報収集を含むため、クリック率だけで導入意欲は判断できません。')
    meta = {key: value for key, value in data.items() if key not in ('weekly', 'categories')}
    meta.update(builtAt=built_at.isoformat(timespec='seconds'), staleAfterDays=MAX_DATA_AGE_DAYS)
    mapping = {
        'WEEKLY_JSON': script_json(data['weekly']), 'CATEGORIES_JSON': script_json(data['categories']),
        'METADATA_JSON': script_json(meta), 'PERIOD': escape(period), 'WEEKS': str(data['weeks']),
        'IMPRESSIONS': f"{data['impressions']:,}", 'LATEST': data['sourceLatestDay'].replace('-', '/'),
        'UPDATED': built_at.astimezone(JST).strftime('%Y/%m/%d %H:%M JST'),
        'EXCLUDED_START': str(data['excludedStartDays']), 'EXCLUDED_END': str(data['excludedEndDays']),
        'CATEGORY_NOTE': note,
    }
    for key, value in mapping.items():
        template = template.replace('@@' + key + '@@', value)
    if re.search(r'@@[A-Z_]+@@', template):
        raise DataError('TEMPLATE_TOKEN: テンプレートに未置換の項目があります')
    validate_html(template)
    return template


class AuditHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.robots = []
        self.bad = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'meta' and attrs.get('name', '').lower() in ('robots', 'googlebot', 'bingbot'):
            self.robots.append({s.strip().lower() for s in attrs.get('content', '').split(',')})
        if tag == 'meta' and attrs.get('http-equiv', '').lower() == 'refresh':
            self.bad.append('redirect')
        if tag in ('iframe', 'form', 'object', 'embed', 'base'):
            self.bad.append(tag)
        for attr in ('src', 'href'):
            value = attrs.get(attr, '')
            if value and not value.startswith(('#', 'data:')):
                self.bad.append('dependency')


def validate_html(html):
    parser = AuditHTML()
    parser.feed(html)
    required = {'noindex', 'nofollow', 'noarchive'}
    if not parser.robots or any(not required <= r or 'index' in r or 'follow' in r for r in parser.robots):
        raise DataError('NOINDEX_MISSING: noindex設定が不足しています')
    if parser.bad or re.search(r'\bfetch\s*\(|XMLHttpRequest|WebSocket|google\.script\.run', html):
        raise DataError('EXTERNAL_DEPENDENCY: 公開HTMLに外部通信・依存があります')
    if any(token in html for token in ('-----BEGIN PRIVATE KEY-----', '"private_key"', '"refresh_token"')):
        raise DataError('SECRET_IN_HTML: 認証情報らしい文字列が含まれています')
    meta = extract_metadata(html)
    weekly = json.loads(re.search(r'const RAW = (.*?);\n', html).group(1))
    cats = json.loads(re.search(r'const CATS = (.*?);\n', html).group(1))
    if len(weekly) != meta['weeks'] or sum(r[1] for r in weekly) != meta['impressions'] or sum(c['imp'] for c in cats) != meta['impressions']:
        raise DataError('RENDER_MISMATCH: 表示データが集計結果と一致しません')
    if sum(r[2] for r in weekly) != meta['clicks'] or sum(c['clicks'] for c in cats) != meta['clicks']:
        raise DataError('RENDER_MISMATCH: クリック数が一致しません')
    return meta


def build(snapshot, template_path, output, built_at):
    data = aggregate(snapshot, built_at.astimezone(JST).date())
    previous = output.read_text(encoding='utf-8') if output.exists() else None
    guard_history(data, previous)
    result = render(data, template_path.read_text(encoding='utf-8'), built_at)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix('.tmp')
    temporary.write_text(result, encoding='utf-8', newline='\n')
    os.replace(temporary, output)
    return {**extract_metadata(result), 'sha256': hashlib.sha256(result.encode('utf-8')).hexdigest()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=ROOT / 'site/index.html')
    parser.add_argument('--built-at', help='ISO timestamp with timezone; for reproducible local review only')
    args = parser.parse_args()
    built_at = datetime.fromisoformat(args.built_at) if args.built_at else datetime.now(timezone.utc)
    if built_at.tzinfo is None:
        parser.error('--built-at requires timezone')
    try:
        result = build(json.loads(args.input.read_text(encoding='utf-8')), ROOT / 'template.html', args.output, built_at)
    except (DataError, KeyError, TypeError, json.JSONDecodeError, OSError) as exc:
        print(str(exc) if isinstance(exc, DataError) else 'BUILD_FAILED: 入力・テンプレートを確認してください', file=sys.stderr)
        return 1
    print(json_text(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
