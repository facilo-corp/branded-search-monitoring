# GSC取得の引き継ぎ・確認版

この版は取得・比較とローカルの更新候補作成まで。Googleへの書き込み経路はありません。
導入しても定期実行は従来の「Sheet読取→Pages更新」のままです。

## 取得条件

- プロパティ `sc-domain:facilo.jp`、Web検索、`facilo|ファシロ`、確定値のみ
- 終端は現行GASと同じJST暦日の3日前。保存する日付ラベルはGSCのPT基準のまま
- 通常は直近28日を再取得。未取得期間が長ければ最終蓄積日の翌日から追いつく。90日超は停止し、復旧方法を確認
- 日次合計は期間全体、日付×検索語は1日ずつ取得してページ送り。日次と内訳の一致を必須とする。APIは全行を保証しないので差を無理に埋めない
- ブランド日次が空の場合、フィルタなしの確定データで当日の存在を確認でき、検索語結果も空のときだけ0件として扱う。未確定・欠落日は補完しない
- 再取得期間は完全に置き換える候補を作る。APIで消えた検索語を残さず、期間外の蓄積は保持する
- 元シートに重複・週次不一致・分類不一致があれば停止。取得中に元シートが変更された場合も停止
- 3タブの一括更新リクエストも作成するが送信しない。書式を保ち、対象列の値だけ変更する設計。検索語は文字列セルとして扱う

## 次の承認対象：比較環境だけを設定

1. 会社の `facilo-seo` に `branded-search-collector@facilo-seo.iam.gserviceaccount.com` を新設。鍵、プロジェクトOwner/Editor、全社の権限委任は不要
2. 現在のWIF providerを利用。既存の会社ID `272079311`／repo ID `1409654390`／main／update.yml／scheduleまたはworkflow_dispatchという条件は変更しない
3. collector上で次の主体だけに `roles/iam.workloadIdentityUser` を付与

   `principalSet://iam.googleapis.com/projects/619038418215/locations/global/workloadIdentityPools/branded-search-pages/attribute.repository_id/1409654390`

4. GSC `sc-domain:facilo.jp` にcollectorを「制限付き」で追加。Sheetsは元ログ1件の「閲覧者」で追加（この段階では編集権限不要）。共有通知はオフ。既存readerは変更しない
5. Search Console APIが有効か確認し、必要な場合のみ承認範囲内で有効化。API使用制限で失敗しても無断で権限を増やさない
6. 確認済みの5ファイルだけを会社repo mainに反映し、Repository variable `GOOGLE_COLLECTOR_SERVICE_ACCOUNT` を上記collectorに設定。既存の稼働・スケジュール変数は維持
7. `gsc_compare=true`, `publish=false` で手動実行。最初は実行直前に確認したSheet最終日を `comparison_end_date` に設定。同じ期間で新GSC結果と蓄積内容を照合
8. 一致後に終端を空欄にして、未取得日を含む候補を作成・検証。元シート、GAS、公開ページは変更しない

手動入力は環境変数から引数として渡し、シェルへ埋め込まない。比較と公開の同時指定はエラーにする。

## 実行結果の読み方

- 終了0：取得・集計検証に成功し、既存期間の件数差がない。新しい期間の追加は差とは区別する
- 終了2：日次・検索語の既存期間に差あり。Google側の修正や取得仕様もあり得るため、自動で正誤を断定せず確認。シートもPagesも更新しない
- 終了1：認証、通信、欠落、集計不一致など。途中結果を保存しない
- 公開ログには集計値・件数・期間・エラー種別のみ。元データやGoogle応答本文、トークンは出さない
- ローカルCLIの保存先はリポジトリ外の空ディレクトリのみ。ActionsではRUNNER_TEMPの専用フォルダを使い、終了時に削除。比較候補や元データをartifactにアップロードしない
- この一時コピーは会社の永続バックアップではない

## 本番切り替え前に残る作業（この版では未実施）

1. 会社共有ドライブの移動先・共有差分・副担当を決める。SheetのIDを維持する移動を確認
2. 非公開の永続バックアップ先と復元方法を確定する
3. 新アカウントでの実データ比較後、検証済み候補を保存する処理を最終化する。バックアップの保存・読み戻し、元データ再照合、単一batchUpdate、更新後照合が必須
4. collectorのSheet編集権限、必要なバックアップ権限、書込処理・週次取得開始、旧GASトリガー1件の停止を、対象と差分を提示して別途承認する
5. 旧GAS停止後、新処理を1回実行。取得→Sheet→既存readerによるページ生成・公開を検証。自然の定期実行も確認する

Sheet更新とPages公開は別の処理。Sheet更新後に公開だけ失敗した場合、前の公開版が残る。原状回復やGAS再開は担当者の判断なしに行わない。Google設定・共有移動・旧GAS停止・本番書込はこの確認版の承認へ含めない。

## 一次仕様

- [GSC取得API](https://developers.google.com/webmaster-tools/v1/searchanalytics/query)
- [GSCの権限](https://support.google.com/webmasters/answer/7687615?hl=en)
- [Sheets一括更新](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets/batchUpdate)
- [セル更新の範囲と末尾消去](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets/request#UpdateCellsRequest)
- [GitHubからのGoogle認証](https://github.com/google-github-actions/auth)
