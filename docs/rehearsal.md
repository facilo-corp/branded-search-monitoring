# 会社Drive・検証Sheetでの保存／復元リハーサル

状態：2026-10-09 ローカル準備済み、外部反映は未実施。以下の設定・コード反映・手動1回の試験は本人確認後に実行する。本番取得切替の承認とは別。

## 1. 作成するもの

会社共有ドライブ「社外共有 - その他（External Partners）」
（`0AGV3TNU5qZvGUk9PVA`）直下に次の5フォルダを作成する。実行前に同名の既存フォルダと共有設定を再照合し、重複を作らない。

```
[社内運用] 指名検索モニタリング
  01_蓄積データ
  02_バックアップ
  03_運用手順
  90_移行検証
```

`90_移行検証` 内に `指名検索ログ_移行検証_20261009` を新設。元Sheet
`1XrYckoVd3vurkLfhdrF9y1AQbg7muJioSSza30kn6J0` の週次・日次・キーワードだけを `sheets.copyTo` でコピーする。各タブ名を元と同名にし、タイムゾーンをAsia/Tokyoへ設定。新規作成時の空タブがあれば検証Sheet内の空タブだけを取り除く。元SheetやそのGASはコピー先の処理から変更しない。

コピーは3タブの値・書式を保持する。スプレッドシート全体の複製ではなく新規Sheetへのタブコピーなので、元GASプロジェクトを複製せず、検証用トリガーも作成しない。生成したSheetのIDと全値指紋を記録し、元3タブと一致を確認する。元Sheetの共有ドライブ移動はこの工程に含めない。

## 2. 追加する権限

| 対象 | アカウント | 今回の追加 | 元データ側 |
|---|---|---|---|
| 検証Sheetだけ | `branded-search-reader@facilo-seo.iam.gserviceaccount.com` | 閲覧者 | 元Sheet閲覧者のまま |
| 検証Sheetだけ | `branded-search-collector@facilo-seo.iam.gserviceaccount.com` | 編集者 | 元Sheet閲覧者・GSC制限付きのまま |
| `02_バックアップ`だけ | 新規 `branded-search-backup@facilo-seo.iam.gserviceaccount.com` | 投稿者（API role=`writer`） | 元Sheet・GSCの権限なし |

バックアップ専用SAは既存の会社GCP `facilo-seo` へ作成し、鍵、project Owner/Editor、ドメイン全体の委任は付けない。Drive APIが無効なら有効化する。SA上へ付けるIAM bindingは次の1つ。

- role：`roles/iam.workloadIdentityUser`
- member：`principalSet://iam.googleapis.com/projects/619038418215/locations/global/workloadIdentityPools/branded-search-pages/attribute.repository_id/1409654390`

既存provider
`projects/619038418215/locations/global/workloadIdentityPools/branded-search-pages/providers/github`
の会社ID272079311、repo ID1409654390、main、update.yml、schedule/workflow_dispatchという制限は変更しない。既存reader/collectorのIAM bindingも変更しない。

SAを共有ドライブ全体のメンバーへ追加しない。共有通知は送らず、既存の人の共有権限を維持する。会社側の継承は前回確認時に全従業員グループと社内2名。実行前に現在の継承設定を再確認する。

`writer`は既存ファイルも編集できる権限であり、改変不能な保管権限ではない。コードの利用経路を新規作成・読取に限定する。閲覧者のダウンロード制限がSheets API読取を妨げた場合は**停止**し、readerの権限拡大やDrive制限の解除で回避しない。

## 3. 会社GitHubへ反映するもの

リポジトリは `facilo-corp/branded-search-monitoring`、比較元mainは
`4f1139657019c2b97cc1b04b4b426ed940742f21`。実行直前にmainを再確認し、進んでいたら差分を照合する。8ファイルだけ反映し、force pushしない。

- `src/cutover.py`、`src/rehearse_cutover.py`
- `tests/test_cutover.py`、`tests/test_rehearse_cutover.py`
- `.github/workflows/update.yml`
- `docs/cutover-review.md`、`docs/collection.md`、`docs/rehearsal.md`

元データ、バックアップ、認証情報、公開HTMLをコミットしない。このpushでPagesを公開する経路はない。既存月曜09:17 JSTのscheduleと全体のconcurrencyは維持する。

追加するRepository variablesは4つ。値が未発行のIDは、上記の作成対象を照合して取得した値を入れ、別の既存ファイルで代用しない。

| 変数 | 値・操作 |
|---|---|
| `GOOGLE_BACKUP_SERVICE_ACCOUNT` | `branded-search-backup@facilo-seo.iam.gserviceaccount.com` |
| `REHEARSAL_SHEET_ID` | 新規検証SheetのID |
| `REHEARSAL_BACKUP_FOLDER_ID` | 新規 `02_バックアップ` のID |
| `REHEARSAL_ENABLED` | 初期false → 検証直前true → 成功・失敗どちらでも確認後false |

既存6変数 `AUTOMATION_ENABLED`、`SCHEDULE_ENABLED`、`SHEET_ID`、`GOOGLE_WIF_PROVIDER`、`GOOGLE_SERVICE_ACCOUNT`、`GOOGLE_COLLECTOR_SERVICE_ACCOUNT` は維持する。新しい定期実行、課金契約、Slack通知は追加しない。

## 4. 手動試験1回の手順と成功条件

1. 73テスト、actionlint、変更ファイル一覧とデータ非混入を再確認。元Sheetと検証Sheetの全値指紋・共有設定を確認する。設定を作成した操作と本人の承認範囲を記録する。
2. `REHEARSAL_ENABLED=true` とし、既存 `update.yml` をmainで手動実行。入力は `gsc_rehearsal=true`、`gsc_compare=false`、`publish=false`、`comparison_end_date`空欄。run IDとhead SHAを記録する。自動再試行しない。
3. 既存WIFを通して3アカウントの30分トークンを発行。collectorは `webmasters.readonly` + `spreadsheets`、readerは `spreadsheets.readonly`、backupは `drive`。鍵ファイルは作成しない。
4. backup先が上記会社Driveのフォルダで作成可能と確認。元Sheet・検証Sheet・reader取得結果の全値が一致した後に、確定GSCから候補を作る。既存期間の差、過去履歴の変化、欠落日、最新データ不足で止める。新規日がなく同一結果なら「完全試験成功」とせず停止する。
5. 更新前backupを新規作成・読戻し、検証Sheetを一括更新・全件照合、更新後backupとmanifestを保存・読戻し。readerで更新値を取得し、公開予定ページをメモリ内で生成・検査する。ファイルやPagesへは出さない。
6. 今回作った更新前世代を選び、検証Sheetの現在値を退避してから元の値に復元する。この**検証内の復元1回**は試験範囲に含む。readerの読取、復元値の完全一致、元Sheetの全値不変を確認して完了記録をbackupフォルダへ保存・読戻しする。
7. 通常時の作成ファイルは5つ（before、after、更新manifest、pre-restore、完了manifest）、検証Sheetの一括更新は2回。履歴を公開ログ・Actions artifactへ出さない。ログは合否と集計件数だけ。試験ファイルは証跡として保持し、自動削除しない。
8. `REHEARSAL_ENABLED=false` に戻し、再取得で確認。成功時はrun成功、元Sheet不変、reader読取成功、backup読戻し成功、検証Sheet復元成功、Pages公開なしを報告する。

途中失敗の場合、検証Sheetは更新済みの可能性がある。再実行や自動復元をせず、Drive上の世代と検証Sheetの全値を読み取って状態を示す。実行権限を広げない。許可された成功シナリオ以外の復旧・削除・再試験は、結果に応じて確認する。

## 5. その後の区切り

リハーサルが成功しても、元Sheet移動、collectorの元Sheet編集権限、初回の書式込み全体backup、GASトリガー停止、取得定期実行切替、Pages更新は実行しない。本番切り替えの最終差分と結果を提示して判断を待つ。副担当は未定のまま。担当決定後の運用引継ぎと技術的な切り替え完了を区別する。

一次仕様：[タブのコピー](https://developers.google.com/workspace/sheets/api/samples/sheet)、[共有DriveとSA](https://developers.google.com/workspace/drive/api/guides/about-shareddrives)、[Driveの権限](https://developers.google.com/workspace/drive/api/guides/ref-roles)、[GitHubからのGoogle認証](https://github.com/google-github-actions/auth)。
