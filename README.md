# Facilo 指名検索モニタリング

Google Search Console由来の指名検索データを集計し、静的なダッシュボードを作成します。

公開予定: `https://facilo-corp.github.io/branded-search-monitoring/`

この一式は公開前の候補です。作成だけでは会社のリポジトリやGoogleの権限は設定されません。

## 更新の流れ

既存GAS → 非公開のGoogle Sheet → GitHub Actionsで読取・検証 → Pages。

- 月曜09:17 JST（00:17 UTC）の実行設定
- 7日間そろった月曜〜日曜のみ集計。カテゴリ分析も同じ期間
- 重複、欠損、件数不整合、履歴の縮小、元データが14日超古い場合は公開を停止
- ページのnoindex・内容ハッシュを公開後に確認
- 認証はGoogleの専用サービスアカウント＋Workload Identity Federation。秘密鍵の保存は不要
- 公開するのは生成コード、テンプレート、集計済みページ。検索語の全件明細や元シートのバックアップは保存しない

## ファイル

| ファイル | 役割 |
|---|---|
| `template.html` | 現行ページから引き継いだ表示と施策イベント |
| `src/fetch_sheet.py` | 元シートの2タブを読む。書き込みは行わない |
| `src/dashboard.py` | 集計・検証・HTML生成 |
| `src/verify_site.py` | ローカルHTMLと実際の公開内容の照合 |
| `site/index.html` | Pagesに出すHTML |
| `.github/workflows/update.yml` | 週次更新と手動確認 |
| `tests/test_dashboard.py` | 合成データによる障害・集計テスト |

Python 3.12の標準ライブラリのみで動きます。生成処理でpipインストールは不要です。

```shell
python -m unittest discover -s tests -v
python src/dashboard.py --input /outside-repository/source.json
python src/verify_site.py --file site/index.html
```

ローカルでの再現用データはリポジトリの外に置きます。実行時刻は自動取得し、表示上は日本時間に変換します。`--built-at`はローカルの再現検証用です。本番ワークフローで上書きしません。

## 初期設定と引き継ぎ

[初期設定](docs/setup.md) と [運用・復旧](docs/operations.md) を参照してください。

初期状態では `AUTOMATION_ENABLED`・`SCHEDULE_ENABLED` が未設定のため動きません。自動化の設定と公開承認を得てから有効にします。手動実行は既定で公開しない確認モードです。

公開リポジトリの標準GitHub実行環境は実行時間無料です。保存容量は別枠のため、Pages用の小さな成果物のみ1日保持します。[GitHub公式仕様](https://docs.github.com/en/billing/concepts/product-billing/github-actions)

noindexは検索除外の指定です。このページとリポジトリは公開されます。
