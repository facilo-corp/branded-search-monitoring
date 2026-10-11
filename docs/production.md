# 本番取得・保存・復旧の運用（切替前の確認版）

この版はローカル実装・検証まで。本番反映、権限変更、元Sheet移動、旧GAS停止は未実施。確認済み版への切替承認後に下記の順序で行う。副担当は未定。

## 対象

- repo: facilo-corp/branded-search-monitoring / main / .github/workflows/update.yml
- 元Sheet: 1XrYckoVd3vurkLfhdrF9y1AQbg7muJioSSza30kn6J0
- 移動先: 会社共有Drive 0AGV3TNU5qZvGUk9PVA 内、01_蓄積データ 1X0fCtYzd1RDIHbFOOcydCGYscOt6Mt4z
- バックアップ先: 同Driveの02_バックアップ 1Aigwimya_m0rG-ZmNT93MYvt2FAtQWJH
- 週次: 毎週月曜09:17 JST開始予定。同一workflowのconcurrencyを維持し、手動・予定実行・復元を直列化する。
- 原データは日次・日次×検索語・週次の全履歴を保持。表示は既存の週次／月次・カテゴリ上位10語＋その他・PRを維持。
- GSCは既存と同じ確定値、JST暦日の3日前まで。日付ラベルはPT基準を保持。直近28日を再取得、遅れがあれば最大90日まで追いつく。既存期間の差は自動採用しない。

## 新しい設定（実環境への設定は承認後）

| 設定 | コード配備時 | 切替後 |
|---|---|---|
| PRODUCTION_ENABLED | false | true |
| LEGACY_GAS_STOPPED | false | 旧GASの停止を実画面で確認した後だけtrue |
| PRODUCTION_BACKUP_FOLDER_ID | 上記02のID | 同じ |
| RESTORE_ENABLED | false | 通常はfalse。復元の個別承認時だけtrue |
| SCHEDULE_ENABLED（既存） | 切替作業中false | 手動1回の公開・配信検証成功後true |

AUTOMATION_ENABLEDは既存の全体停止設定。認証3アカウント、WIF条件、元SHEET_ID、検証設定は既存値を維持。新設定が未設定／falseなら既存の読取・公開運用を継続し、本番取得は始まらない。LEGACY_GAS_STOPPEDは現地確認結果の記録であり、GASを自動検出・停止する機能ではない。

取得用collectorを元Sheet1件だけ閲覧者から編集者へ。GSCは閲覧権限のまま。readerは閲覧専用、backupは02だけ投稿者のまま。サービスアカウント鍵・全社委任・GCP管理ロール・Drive全体メンバーを追加しない。

## 切替時の順序

1. 承認済みファイルのハッシュ、remote main、他の変更を照合。未確認の差があれば統合内容を確認してから反映する。SCHEDULE_ENABLED=falseにし、既に起動したActionsとGASの実行中0を確認。旧GASの起動時間帯を避ける。
2. 書式を含む元Sheet全体コピー、3タブ全値・構造、GASコード、トリガー1件の関数・曜日・時刻・タイムゾーン、GitHubの秘密値を含まない設定を02へ退避。読み戻して元シートと照合。現行共有一覧と移動先の継承を確認する。
3. 確認版の9ファイルを反映し、新設定は停止状態で作成。旧GASのupdateBrandedSearchトリガー1件を削除し、0件・実行中0を確認。コードは残す。LEGACY_GAS_STOPPED=trueはその後に設定する。
4. 元Sheetを同じIDのまま01へ移動。全3タブ・全値・タイムゾーン・個別共有・継承を再確認。想定外の共有変更なら停止。collectorだけ編集者へ変更し、readerからも読み取り確認する。共有通知は送らない。
5. PRODUCTION_ENABLED=true、RESTORE_ENABLED=false、予定実行停止の状態で、gsc_collect=true / publish=trueを手動1回実行。比較・検証・復元・公開再実行の入力は指定しない。
6. 実行結果の取得・前後バックアップ・全件照合・reader検証、build/deploy/verify、公開ファイルのhashとnoindexを確認。Sheet最終日とページ更新日時を区別して記録。途中で失敗したら定期実行を再開しない。
7. 成功後にSCHEDULE_ENABLED=true。最初の自然な月曜実行を確認して取得移行完了とする。副担当決定後にrepo/Googleアクセスと復旧手順を引き継ぐ。

承認なしに操作を先取りしない。移動直前の元データは、ローカル試験に使った9月29日版から更新されている可能性がある。直前の実データと状態を再確認する。

## 通常の更新と手動実行

自動実行は取得→候補検証→更新前backupと読戻し→元Sheet再照合→3タブ一括保存→全件読戻し→更新後backupとmanifest読戻し→reader照合→HTML生成→公開→配信検証。

同日再実行などで差分が0ならSheetへ書かず、現在の全履歴を前後backupとして保存・照合する。公開前の途中障害が復旧した場合も、既存データを二重に追加しない。

- 比較のみ: gsc_compare=true、publish=false。元Sheetへは書かない。
- 画面検証のみ: 全入力既定。元Sheet読取と生成まで。書込・公開なし。
- 取得して保存: gsc_collect=true。publish=falseでもSheet保存は行うため、この操作自体の承認が必要。
- 取得して公開: gsc_collect=true、publish=true。
- 公開だけ再実行: gsc_collect=false、publish=true、backup_manifestに成功した保存処理のmanifest ID。GSC取得・Sheet書込・Drive新規保存を行わない。
- 復元: 下記の個別手順。公開と同時指定できない。

公開再実行ではmanifestと更新後backupのID・hash・対象・件数・全値指紋を照合し、現在のSheetがその保存成功版と一致するときだけ公開する。取得を有効にした後は、manifestなしの公開を拒否する。

## 失敗時

| 状態 | 対応 |
|---|---|
| 認証・権限・対象違い・旧GAS停止未確認 | 書込前に停止。鍵追加や権限拡大で迂回しない |
| 欠落日・集計不一致・既存期間の差 | 候補を保存しない。原因と差を確認する |
| 更新前backupの作成／読戻し失敗 | Sheetへ書かない |
| Sheet書込の応答が不明 | 同じ書込を再送しない。元・候補・その他を全値で判定。候補一致なら続行、それ以外は停止 |
| 更新後backup／manifest／reader照合失敗 | Sheetは更新済みの可能性あり。自動で戻さず、状態を確認。再取得で候補一致なら書込0でbackupを完成させられる |
| Sheet保存は成功、公開だけ失敗 | 同じ保存成功manifestで公開だけ再実行。GSC取得・Sheet保存を繰り返さない |
| 公開後verify失敗 | 公開済みの可能性あり。配信状況を確認し、無断で再公開・旧版復元をしない |

通常の既存期間差を勝手に採用する機能はない。Google側の修正などで差が出た場合は、差分を提示して別途判断する。生成3タブを手編集しない。APIだけで人の編集を完全にロックすることはできない。

## 復元（個別承認が必要）

1. 予定実行を止め、実行中0と旧GAS停止を確認。バックアップの世代と現在のデータの差を提示し、復元先・世代・現在の全件指紋・公開する範囲への承認を得る。
2. SCHEDULE_ENABLED=false、PRODUCTION_ENABLED=true、LEGACY_GAS_STOPPED=true、RESTORE_ENABLED=true。restore_generationに選んだgzip backup ID、restore_fingerprintに確認済みの現在の全件SHA256を指定し、他の操作入力はfalse／空欄。
3. 現在のSheetが承認時と一致することを確認し、復元前backupを作成・読戻し。選んだ同一Sheet・同一タブの世代を一括復元し、全件照合、reader照合、復元後backupとmanifestの読戻しまで行う。この実行では公開しない。
4. 復元結果を確認し、RESTORE_ENABLED=falseへ戻す。公開が承認されていれば復元完了manifestを使い、公開だけを別途実行する。明示的な復元完了記録の場合のみ、以前公開した数字より小さくなることを許可する。元データの鮮度・整合性検証は省略しない。古い世代では鮮度により公開が止まるため、取得再開方法も確認する。
5. 復旧成功後、承認された運用へ戻す。未知の状態で何度も更新・復元を再実行しない。

旧GASへ戻す場合は会社側の取得が停止・実行中0であることを確認。保存済み仕様どおり月曜6〜7時JSTのトリガーを1件だけ再作成する。GAS停止フラグも実状態に合わせる。Sheet編集権限や移動先が元と異なるため、本人アカウントからの接続を検証してから再開する。旧GASへの切戻しも別途判断であり、自動では行わない。

## バックアップと記録

before.json.gz、after.json.gz、manifest.jsonをrun ID＋試行番号付きの新規ファイルとして02へ保存する。同じrunの再試行も試行番号で区別。通信断後のDrive新規作成は自動再試行しない。上書き・自動削除なし。

バックアップは3タブの全履歴・見出し・Sheet/タブID・タイムゾーン・日時・件数・集計・指紋・コード版を含む。定期バックアップは値の復旧用。書式・GAS・トリガーの退避は初回の別工程。同じDriveなので独立した改変不能な保管ではない。

公開ログにはID・件数・集計・hashのみ。検索語全件・生データ・トークンを出さず、backupをGitHub artifactや公開repoへ入れない。公開用HTMLに含まれる既存の上位キーワード表示は従来どおり。
