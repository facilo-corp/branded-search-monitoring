"""Verify the exact delivered file and root robots.txt after deployment."""
import argparse
import hashlib
from pathlib import Path
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from urllib.robotparser import RobotFileParser
from dashboard import DataError, validate_html


def verify(url, expected_sha256):
    parts = urlsplit(url)
    if parts.scheme != 'https' or parts.netloc != 'facilo-corp.github.io' or parts.path != '/branded-search-monitoring/':
        raise DataError('URL_MISMATCH: 公開先が計画と異なります')
    with urlopen(Request(url, headers={'Cache-Control': 'no-cache'}), timeout=30) as response:
        if response.status != 200 or response.geturl() != url:
            raise DataError('PUBLIC_RESPONSE: 公開URLの応答が想定と異なります')
        payload = response.read()
        xrobots = response.headers.get('X-Robots-Tag', '')
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise DataError('HASH_MISMATCH: 公開内容の反映を待っています')
    validate_html(payload.decode('utf-8'))
    if any(t.strip().lower() in ('index', 'follow') for t in xrobots.split(',')):
        raise DataError('ROBOTS_HEADER_CONFLICT: 公開ヘッダーを確認してください')
    robots_url = f'{parts.scheme}://{parts.netloc}/robots.txt'
    try:
        with urlopen(robots_url, timeout=30) as response:
            rules = response.read().decode('utf-8')
    except HTTPError as exc:
        if exc.code == 404:
            exc.close()
            return
        raise
    parser = RobotFileParser()
    parser.parse(rules.splitlines())
    if not all(parser.can_fetch(agent, url) for agent in ('*', 'Googlebot', 'Bingbot')):
        raise DataError('ROBOTS_BLOCK: robots.txtがnoindexの読み取りを妨げています')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--file', type=Path)
    parser.add_argument('--url')
    parser.add_argument('--sha256')
    args = parser.parse_args()
    if args.file:
        validate_html(args.file.read_text(encoding='utf-8'))
        print('Local HTML validation passed.')
        return 0
    if not args.url or not args.sha256:
        parser.error('use --file or --url with --sha256')
    for attempt in range(12):
        try:
            verify(args.url, args.sha256)
            print('Published HTML, hash and noindex verified.')
            return 0
        except (DataError, HTTPError, URLError, TimeoutError):
            if attempt < 11:
                time.sleep(10)
    print('PUBLIC_VERIFY_FAILED: 公開済みの可能性があります。再公開・自動ロールバックせず状況を確認してください', file=sys.stderr)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
