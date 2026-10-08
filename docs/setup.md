# 初期設定（管理者向け）

以下は承認後の設定手順です。現時点で実行したことを意味しません。

## 1. GitHub

1. `facilo-corp/branded-search-monitoring` をpublic・default branch `main`で作成
2. レビュー済みのファイル一覧だけを登録。元データ・ローカルの認証ファイル・作業フォルダを含めない
3. PagesのBuild and deploymentは「GitHub Actions」、HTTPS有効
4. `github-pages` environmentは`main`からのデプロイのみ許可
5. 会社側のActionsポリシーで使用する公式Actionsと、生成HTMLだけのbotコミットが許可されることを確認。ブランチ保護を無断で緩めない
6. 会社側の管理担当を渡辺さん以外にも置く。担当者は別途指定

## 2. Google（会社管理者と確認）

会社管理のCloudプロジェクトを使います。既存プロジェクトを利用する場合は所有者・IAMを確認します。既存の広い権限を持つ秘密鍵は転用しません。

### 作成する専用ID

サービスアカウント名候補: `branded-search-reader`。プロジェクトのOwnerやEditor権限は付けません。

元シート1件を、このアカウントへ「閲覧者」で共有します。会社側の外部共有ポリシーで許可されるか確認します。元シートの一般公開や、全社の権限委任は不要です。

### 短期認証の連携

Sheets API、IAM Service Account Credentials API、Security Token Service API、IAM APIが利用可能なことを確認します。Workload Identity Pool／OIDC Providerを作成し、issuerを`https://token.actions.githubusercontent.com`にします。

マッピング:

```text
google.subject=assertion.sub
attribute.repository_id=assertion.repository_id
attribute.repository_owner_id=assertion.repository_owner_id
```

条件（実際に作成された会社・リポジトリの数値IDに置換）:

```text
assertion.repository_owner_id=='ORG_NUMERIC_ID' &&
assertion.repository_id=='REPO_NUMERIC_ID' &&
assertion.ref=='refs/heads/main' &&
assertion.workflow_ref=='facilo-corp/branded-search-monitoring/.github/workflows/update.yml@refs/heads/main' &&
(assertion.event_name=='schedule' || assertion.event_name=='workflow_dispatch')
```

サービスアカウント上で、次の限定principalSetに`roles/iam.workloadIdentityUser`を付与します。Google Workspaceのdomain-wide delegationやユーザーなりすましは使いません。

```text
principalSet://iam.googleapis.com/projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/POOL_ID/attribute.repository_id/REPO_NUMERIC_ID
```

連携先はこの専用アカウントだけに限定。長期秘密鍵の作成・既存鍵のGitHub登録は行いません。

[Googleの設定手順](https://docs.cloud.google.com/iam/docs/workload-identity-federation-with-deployment-pipelines)、[認証Actionの設定](https://github.com/google-github-actions/auth)

## 3. GitHubのVariables

値はリポジトリ設定に登録します。これらは秘密鍵ではありません。Sheetsアクセスに必要な権限はGoogle側で制限されます。

| 名前 | 値・初期状態 |
|---|---|
| `SHEET_ID` | 対象の元シートID |
| `GOOGLE_WIF_PROVIDER` | `projects/.../locations/global/workloadIdentityPools/.../providers/...` |
| `GOOGLE_SERVICE_ACCOUNT` | 専用サービスアカウントのメールアドレス |
| `AUTOMATION_ENABLED` | 初期`false`。認証設定後、確認実行を許可した段階で`true` |
| `SCHEDULE_ENABLED` | 初期`false`。手動で公開・配信検証した後、定期更新開始の承認範囲で`true` |

## 4. 動作確認と公開

1. `main`で手動実行し、`publish`は`false`。元シート読取、集計、検証結果を確認。コミット・Pages反映は行われない
2. 本人確認済みの版・公開操作への承認がある場合のみ、`publish=true`で手動実行
3. Pagesの実URL、配信HTMLのSHA256、noindex、root robots.txt、PC・スマホ表示を確認
4. `SCHEDULE_ENABLED=true`にして定期更新開始。次回の予定実行が成功したかも確認する
5. 新URL確認後、承認範囲内で旧指名検索ページを新URLの案内へ更新。他ページや個人リポジトリ全体は移さない

本番データが確認版から変わっている場合は、対象期間と変更内容を再照合します。集計ルール・表示・イベントの変更は別途レビューします。

## 5. Google側の継続性

この版では既存GASを継続使用します。GASトリガーは作成者の権限で動くため、会社リポジトリ化だけで引き継ぎ完了とはしません。

シート所有者・Cloud管理者・GSC権限を確認し、次段階で取得処理も会社管理へ移します。新しい取得処理と現行結果の一致を確認してから、別途承認された範囲でGASを停止します。
