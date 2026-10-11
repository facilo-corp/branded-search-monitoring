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
