# 個別監視・通知（branded）

このリポジトリの業務処理だけを、標準GITHUB_TOKENで監視します。別リポジトリへの個人認証は不要です。
毎時7・22・37・52分予定。業務処理と別に起動し、未起動・失敗・結果不明・要確認・復旧を判定します。GitHubの起動遅延や全体停止の検知は保証しません。

通知先は #marketing-automation-alerts（C0C7QRMJP0R）、既存のFacilo Marketing Bot。担当は渡辺さん。通常成功は通知せず、同じ問題の再通知は24時間間隔です。業務登録・招待・データ修正は行いません。

2026-10-11、この会話で3件への本番反映、承認済みテスト1通、重複防止検証、自動通知の開始が承認されました。宛先・定型文・条件に限定した継続通知の承認を使用します。テストはBiziblからだけです。

## 設定と検証

- MONITOR_ENABLED、ALERT_DELIVERY_ENABLED、ALERT_RECURRING_ENABLEDは個別スイッチです。
- APPROVED_MONITOR_SHAは承認版の基準点。監視コード・承認ファイル・監視workflow・.gitattributesの差分は停止します。通常の集計結果更新だけなら継続します。
- `python automation-monitor/runtime.py --target branded --output monitor-result` は読み取りとローカル試算のみ。`--persist` / `--send`は本番台帳 / Slack送信の操作です。
- `python -m unittest discover -s automation-monitor -p 'test_*.py' -q` で判定・中断・重複防止を検証します。

## 送信記録

自リポジトリのautomation-monitor-state branchのalerts.jsonを使用します。送信前の記録と読み戻し、Slackへの1回の送信、受領結果の記録と読み戻しを行います。送信結果不明・owner残留・記録の不一致では自動再送しません。既存台帳の再初期化・削除は禁止です。

止める場合はMONITOR_ENABLED=false、通知だけ止める場合はALERT_DELIVERY_ENABLED=false。ウェビナー個別通知台帳Cと、GitHub外の監視は今回の対象外です。
# Receipt recovery (branded only, pre-production review 2026-11-01)

Slack acknowledgements are written to `delivery-receipts.json` before committing
the sent state. A separate, attempt-specific Actions artifact keeps these records
for 14 days. It contains IDs, hashes, timestamps and receipts, never message text,
API bodies, credentials or personal data. The artifact upload runs even when the
monitor step fails. Neither a receipt-file failure nor an upload failure is hidden.

On the next execution, recovery accepts exactly one interrupted `sending` row
only when both the original run and its exact attempt have completed. It checks
the repository, workflow, main branch, event, approved Git history, code hash,
artifact run metadata and archive digest, the complete pre-send journal hash,
row/text hashes, destination and Slack receipt timestamp. A final compare-and-swap
guards against changes after validation. No Slack history access or resend is used.
Normal sent records and the existing notification policy are preserved.

Missing/expired artifacts, mismatched provenance, an active owner, unknown Slack
responses or a changed journal remain held for review. The first pre-upgrade
failure cannot be recovered using a receipt artifact that did not exist then.
If both journal and artifact persistence fail, manual receipt verification is
still necessary. Run deletion or retention expiry also removes recovery evidence.

`report.json` and `evidence.json` are saved before journal/delivery work.
`monitor_complete: false` means the business observation is available but the
monitor did not finish. `state-diagnostics.json` contains bounded, sanitized
HTTP statuses and reconciliation stages. Failed writes are never retried; only
reads are retried. A successful write followed by stale Contents responses can
be checked against the branch's immutable commit. A different current state
continues to stop processing.

Before production deployment, synchronize the recurring authorization's code
hash and the approved monitor commit as one reviewed release. Use:

```
python automation-monitor/runtime.py --target branded --check-release --authorization automation-monitor/authorization.json --output release-check
```

The check is read-only, requires the candidate checkout and approval variables to
match, and rejects dirty/unreviewed monitor files or a stale policy even when no
alert would be queued. This review change does not itself modify the live
authorization file, GitHub variables, schedules, destinations or message templates.
Do not merge/deploy until this exact candidate has been approved. After approved
deployment, verify ordinary scheduled executions, the receipt artifact, and
the journal. Do not manufacture an alert or replay old messages for a live test.
