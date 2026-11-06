"""Cloud-ready entrypoint. Default is read-only collection + local preview."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import monitor as m
import evidence as verification
import delivery as d
import receipts
from state_store import GitStore, MemoryStore, Journal, StateStoreError

MONITOR_PATHS = ("automation-monitor/", ".github/workflows/automation-monitor.yml", ".gitattributes")


@contextmanager
def record_store_diagnostics(store, destination):
    """Persist fixed, sanitized transport facts even if the journal cannot be saved."""
    try:
        yield
    finally:
        if isinstance(store, GitStore):
            output = Path(destination)
            output.mkdir(parents=True, exist_ok=True)
            m.write_json(output / "state-diagnostics.json", {"events": store.diagnostics})


def approved_source(head):
    """Previous run must contain the same reviewed monitor, not just claim a hash."""
    base = os.getenv("APPROVED_MONITOR_SHA", "")
    if not all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) for value in (base, head)):
        return False
    try:
        for command in (("merge-base", "--is-ancestor", base, head),
                        ("merge-base", "--is-ancestor", head, "HEAD"),
                        ("diff", "--quiet", base, head, "--", *MONITOR_PATHS)):
            if subprocess.run(["git", *command], capture_output=True, timeout=30).returncode:
                return False
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


def validate_release(authorization, target, now):
    """Check version binding even when a healthy observation queues no message."""
    d.need(isinstance(authorization, dict) and authorization.get("repository") == m.CONFIG[target]["repo"]
           and authorization.get("target") == target, "authorization_target_mismatch")
    d.need(authorization.get("mode") == "recurring_template_exception"
           and d.approved({"event": "new"}, authorization, now), "monitor_policy_not_approved")


def approved_checkout():
    """Keep approval tied to monitor files, allowing normal dashboard commits."""
    base, head = os.getenv("APPROVED_MONITOR_SHA", ""), os.getenv("GITHUB_SHA", "")
    if not all(re.fullmatch(r"[0-9a-f]{40}", value) for value in (base, head)):
        return False
    def git(*args):
        return subprocess.run(["git", *args], capture_output=True, text=True, timeout=30)
    try:
        actual = git("rev-parse", "HEAD")
        if actual.returncode or actual.stdout.strip() != head:
            return False
        if not approved_source(head):
            return False
        dirty = git("status", "--porcelain", "--untracked-files=all", "--", *MONITOR_PATHS)
        return dirty.returncode == 0 and not dirty.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return False


def heartbeat_status(state, now):
    """Reusable by an independent host. It must not rely on the monitored job."""
    from datetime import timedelta
    last = state.get("heartbeat")
    if last is None:
        return "missing"
    gap = m.parse_time(now)-m.parse_time(last)
    if gap.total_seconds() < -60:
        return "invalid"
    return "stale" if gap > timedelta(minutes=60) else "current"


def run(args):
    d.need(args.target in m.CONFIG, "monitor_target_required")
    d.need(not args.test_delivery or args.target == "bizibl", "one_test_only_from_bizibl")
    d.need(not args.ack_key or args.ack_key == args.target, "acknowledgement_target_mismatch")
    with m.repository_scope(args.target):
        return run_scoped(args)


def run_scoped(args):
    now = datetime.now(timezone.utc)
    repository = m.CONFIG[args.target]["repo"]
    authorization = json.loads(Path(args.authorization).read_text(encoding="utf-8")) if args.authorization else None
    if args.send:
        d.need(authorization and authorization.get("repository") == repository and
               authorization.get("target") == args.target, "authorization_target_mismatch")
    if args.replay:
        snapshot = json.loads(Path(args.replay).read_text(encoding="utf-8"))
        now = m.parse_time(args.now or snapshot["fetched_at"])
        d.need(not args.persist and not args.send, "replay_cannot_mutate_production")
    else:
        d.need(not args.now, "cannot_override_live_clock")
        snapshot = m.collect(args.target)
        now = m.parse_time(snapshot["fetched_at"])
    report, _ = m.build_report(snapshot, now=now, target=args.target)
    if not args.replay:
        verification.enrich(report, snapshot)
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    report.update(target=args.target, repository=repository, monitor_complete=False,
                  delivery_phase="not_completed")
    # Preserve the business observation even if locking, journal writes or
    # delivery subsequently fail. It is explicitly not an end-to-end success.
    m.write_json(output / "evidence.json", snapshot)
    m.write_json(output / "report.json", report)
    if args.persist or args.send:
        d.need(args.persist and os.getenv("MONITOR_ENABLED") == "true", "monitor_persistence_disabled")
        d.need(os.getenv("GITHUB_REPOSITORY") == repository and
               os.getenv("GITHUB_REF") == "refs/heads/main" and
               approved_checkout(),
               "monitor_code_not_approved")
        if args.send and args.target == "branded":
            validate_release(authorization, args.target, now)
        store = GitStore(os.getenv("GITHUB_TOKEN", ""), repository=repository, target=args.target)
    else:
        store = MemoryStore()
    if args.test_delivery:
        d.need(args.persist and args.send and authorization and authorization.get("mode") == "exact_messages", "exact_test_approval_required")
    with record_store_diagnostics(store, output):
        sent, pending, state, recovered = run_journal(args, store, report, authorization, now, output)
    report.update(target=args.target, repository=repository,
        monitor_complete=True, delivery_phase="finished", recovered_receipts=recovered,
        mode="production_monitor" if args.persist else "local_validation_only",
        scheduled=bool(args.persist and os.getenv("GITHUB_EVENT_NAME") == "schedule"), slack_messages_sent=sent["sent"],
        pending_messages=pending, delivery_needs_review=sent["needs_review"], approval_blocked=sent["approval_blocked"],
        template_version=d.TEMPLATE_VERSION, code_sha256=d.code_fingerprint(),
        limitations=["本番の定期監視とSlack送信はまだ有効化していません" if not args.persist else "監視の実行時刻は遅延する場合があります",
                     "GitHub全体の停止を検知する別ホストの監視は未接続です", "ウェビナー個別通知の新台帳Cは未反映・今回の対象外です"])
    for result in report["results"]:
        candidate = next((r for r in reversed(pending) if r["key"] == result["key"]), None)
        result["preview_event"] = candidate["event"] if candidate else "suppressed"
        result["message_text"] = candidate["text"] if candidate else d.message(result, "new")
    # Only sanitized evidence, no rows, cookies, credentials or raw logs.
    m.write_json(output / "evidence.json", snapshot)
    m.write_json(output / "report.json", report)
    if not args.persist:
        m.write_json(output / "preview-state.json", {"simulation_only": True, "state": state})
    approval = {"approved": False, "mode": "exact_messages", "channel": m.CHANNEL_ID,
                "approval_reference": "", "messages": [{"id": r["id"], "text_sha256": r["text_sha256"]} for r in pending]}
    m.write_json(output / "exact-approval-unapproved.json", approval)
    print(json.dumps({"mode": report["mode"], "states": {r["key"]: r["state"] for r in report["results"]},
                      "pending_messages": len(pending), "slack_messages_sent": sent["sent"]}, ensure_ascii=False))
    return 1 if sent["needs_review"] or sent["approval_blocked"] else 0


def run_journal(args, store, report, authorization, now, output):
    sent = {"sent": 0, "needs_review": 0, "approval_blocked": 0}
    recovery = receipts.Recovery(store, approved_source) if args.persist and args.target == "branded" else None
    journal = Journal(store)
    with journal.exclusive(recover=recovery):
        d.validate_target(journal.state, args.target)
        receipt_log = receipts.ReceiptLog(output, store.repository, args.target, journal.owner) \
            if args.send and args.target == "branded" else None
        journal.save(d.plan(journal.state, report, now))
        if args.test_delivery:
            journal.save(d.enqueue_test(journal.state, now))
        if args.ack_key or args.ack_signature:
            d.need(args.persist and args.ack_key and args.ack_signature, "exact_acknowledgement_required")
            journal.save(d.acknowledge(journal.state, args.ack_key, args.ack_signature))
        if args.send:
            d.need(authorization and authorization.get("approved") is True, "specific_slack_approval_required")
            sender = d.SlackSender(os.getenv("SLACK_ALERT_BOT_TOKEN", ""))
            sent = d.dispatch(journal, sender, authorization, now, receipt_log=receipt_log)
        pending = [r for r in journal.state["outbox"].values() if r["status"] in {"pending", "needs_review"}]
    return sent, pending, journal.state, recovery.recovered if recovery else []


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", choices=list(m.CONFIG), required=True)
    parser.add_argument("--output", default="monitor-result")
    parser.add_argument("--replay")
    parser.add_argument("--now")
    parser.add_argument("--persist", action="store_true")
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--authorization")
    parser.add_argument("--test-delivery", action="store_true")
    parser.add_argument("--ack-key", choices=list(m.CONFIG))
    parser.add_argument("--ack-signature")
    parser.add_argument("--check-release", action="store_true",
                        help="Read-only gate: verify checkout and authorization before deployment")
    args = parser.parse_args()
    try:
        if args.check_release:
            d.need(args.authorization and not args.persist and not args.send, "release_check_read_only")
            authorization = json.loads(Path(args.authorization).read_text(encoding="utf-8"))
            validate_release(authorization, args.target, datetime.now(timezone.utc))
            d.need(approved_checkout(), "monitor_code_not_approved")
            print(json.dumps({"release_check": "passed", "target": args.target, "code_sha256": d.code_fingerprint()}))
            return 0
        return run(args)
    except Exception as exc:
        # Fixed descriptions only: never print URLs, API bodies or exception
        # text unless it is a reviewed, non-sensitive code.
        explanations = {
            "monitor_lock_requires_review": ("前回の監視処理の終了を確認できず、監視を停止しています", "前回実行の終了と通知の送信履歴を照合してから復旧する"),
            "monitor_delivery_requires_review": ("前回のSlack送信結果が未確定のため、監視を停止しています", "送信先の受信記録を照合する。結果不明の通知は再送しない"),
            "monitor_previous_run_unverified": ("前回実行の識別情報が一致しないため、監視を停止しています", "実行履歴と処理中の記録を照合する"),
            "state_write_unconfirmed": ("監視記録の保存結果を確認できません", "GitHubの保存履歴を照合する。通知の送信は繰り返さない"),
            "state_readback_unconfirmed": ("監視記録の読み戻しが一致しません", "保存された版と実行履歴を照合する"),
            "state_changed_concurrently": ("監視記録が他の処理で変更されたため停止しています", "同時実行と変更履歴を確認する"),
            "state_api_unavailable": ("GitHubの監視記録に接続できません", "GitHubの稼働状況とアクセス権を確認する"),
            "state_timeout": ("GitHubの監視記録への接続が時間切れになりました", "保存履歴と診断記録を照合する。通知は再送しない"),
            "state_connection_error": ("GitHubの監視記録への通信が切断されました", "保存履歴と診断記録を照合する。通知は再送しない"),
            "state_service_unavailable": ("GitHubから一時的な応答エラーが返りました", "保存履歴と診断記録を照合する。通知は再送しない"),
            "state_rate_limited": ("GitHubの利用制限により監視記録へのアクセスを停止しました", "GitHubの制限解除を確認する。連続して再実行しない"),
            "state_access_denied": ("GitHubの監視記録へのアクセスが拒否されました", "実行用認証の権限を確認する"),
            "state_not_found": ("GitHubの監視記録または保存先を参照できません", "保存先と認証の参照権限を確認する。記録を初期化しない"),
            "state_conflict": ("監視記録の保存が競合しました", "保存履歴を照合する。通知は再送しない"),
            "state_request_rejected": ("GitHubが監視記録への要求を受け付けませんでした", "診断記録と保存先設定を確認する"),
            "state_response_invalid": ("GitHubの監視記録の応答を正しく読み取れませんでした", "保存履歴と応答の診断記録を確認する"),
            "state_retry_budget_exceeded": ("監視記録の再確認が制限時間に達しました", "保存履歴と診断記録を照合する。通知は再送しない"),
            "state_history_invalid": ("監視記録の保存履歴を検証できませんでした", "保存先の履歴を確認する。通知は再送しない"),
            "state_content_hash_mismatch": ("監視記録の内容と版情報が一致しません", "保存先の履歴を確認する。通知は再送しない"),
            "monitor_code_not_approved": ("監視コードが承認済みの版と一致しません", "変更差分を確認して承認対象の版を指定する"),
            "monitor_policy_not_approved": ("通知の承認済み版情報が実装と一致しません", "反映前にコードと通知の版情報を同期して確認する"),
            "monitor_receipt_requires_review": ("前回の送信記録を自動照合できません", "受信番号と実行記録を確認する。結果不明の通知は再送しない"),
            "receipt_record_unconfirmed": ("Slackの受信番号を別記録へ保存できません", "受信記録と台帳を照合する。通知は再送しない"),
        }
        code = str(exc) if str(exc) in explanations else "monitor_requires_review_no_automatic_retry"
        reason, action = explanations.get(code, ("監視処理が停止しました。原因の追加確認が必要です", "実行履歴を調査する。自動再送はしない"))
        failure = {"status": "monitor_stopped", "reason": code, "target": args.target,
                   "cause": reason, "next_action": action, "business_recovery_verified": False}
        output = Path(args.output)
        output.mkdir(parents=True, exist_ok=True)
        if isinstance(exc, StateStoreError):
            failure["diagnostic"] = exc.diagnostic
        m.write_json(output / "failure.json", failure)
        summary_path = os.getenv("GITHUB_STEP_SUMMARY")
        if summary_path:
            with open(summary_path, "a", encoding="utf-8") as stream:
                stream.write(f"\n### 監視停止・要対応\n\n{reason}。\n\n対応：{action}。\n\n業務の復旧は未確認です。\n")
        print(json.dumps(failure, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
