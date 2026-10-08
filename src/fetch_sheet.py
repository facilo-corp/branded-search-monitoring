"""Read just two ranges; never write to Sheets or print Google response bodies."""
import argparse
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def fetch(sheet_id, token):
    if not re.fullmatch(r'[A-Za-z0-9_-]{20,}', sheet_id or '') or not token:
        raise ValueError('SHEET_CONFIG: Google認証またはシートIDが未設定です')
    query = urlencode({'ranges': ["'日次'!A:D", "'キーワード'!A:G"],
                       'valueRenderOption': 'UNFORMATTED_VALUE', 'dateTimeRenderOption': 'SERIAL_NUMBER'}, doseq=True)
    request = Request(f'https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values:batchGet?{query}',
                      headers={'Authorization': 'Bearer ' + token}, method='GET')
    for attempt in range(3):
        try:
            with urlopen(request, timeout=45) as response:
                return json.load(response)
        except HTTPError as exc:
            status = exc.code
            exc.close()
            if status not in (429, 500, 502, 503, 504) or attempt == 2:
                raise ValueError(f'SHEET_READ_FAILED: Google応答 {status}。権限と実行状況を確認してください') from None
        except (URLError, TimeoutError):
            if attempt == 2:
                raise ValueError('SHEET_NETWORK: 元シートを取得できませんでした') from None
        time.sleep(2 ** attempt)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        data = fetch(os.environ.get('SHEET_ID'), os.environ.get('GOOGLE_ACCESS_TOKEN'))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open('w', encoding='utf-8') as handle:
            json.dump(data, handle, ensure_ascii=False)
    except (ValueError, OSError):
        print('SHEET_READ_FAILED: 設定・権限・Google側の応答を確認してください。元データはログに出していません', file=sys.stderr)
        return 1
    print('Sheet read completed (read-only).')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
