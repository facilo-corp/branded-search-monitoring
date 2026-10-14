"""Cloud-ready entrypoint. Default is read-only collection + local preview."""
import argparse
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
from state_store import GitStore, MemoryStore, Journal


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
        if git("merge-base", "--is-ancestor", base, head).returncode:
            return False
        return git("diff", "--quiet", base, head, "--", "automation-monitor/",
                   ".github/workflows/automation-monitor.yml", ".gitattributes").returncode == 0
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
    if args.persist or args.send:
        d.need(args.persist and os.getenv("MONITOR_ENABLED") == "true", "monitor_persistence_disabled")
        d.need(os.getenv("GITHUB_REPOSITORY") == repository and
               os.getenv("GITHUB_REF") == "refs/heads/main" and
               approved_checkout(),
               "monitor_code_not_approved")
        store = GitStore(os.getenv("GITHUB_TOKEN", ""), repository=repository, target=args.target)
    else:
        store = MemoryStore()
    if args.test_delivery:
        d.need(args.persist and args.send and authorization and authorization.get("mode") == "exact_messages", "exact_test_approval_required")
    sent = {"sent": 0, "needs_review": 0, "approval_blocked": 0}
    with Journal(store).exclusive() as journal:
        d.validate_target(journal.state, args.target)
        journal.save(d.plan(journal.state, report, now))
        if args.test_delivery:
            journal.save(d.enqueue_test(journal.state, now))
        if args.ack_key or args.ack_signature:
            d.need(args.persist and args.ack_key and args.ack_signature, "exact_acknowledgement_required")
            journal.save(d.acknowledge(journal.state, args.ack_key, args.ack_signature))
        if args.send:
            d.need(authorization and authorization.get("approved") is True, "specific_slack_approval_required")
            sender = d.SlackSender(os.getenv("SLACK_ALERT_BOT_TOKEN", ""))
            sent = d.dispatch(journal, sender, authorization, now)
        pending = [r for r in journal.state["outbox"].values() if r["status"] in {"pending", "needs_review"}]
        state = journal.state
    state = store.read()[1]
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    report.update(target=args.target, repository=repository,
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
    args = parser.parse_args()
    try:
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
            "monitor_code_not_approved": ("監視コードが承認済みの版と一致しません", "変更差分を確認して承認対象の版を指定する"),
        }
        code = str(exc) if str(exc) in explanations else "monitor_requires_review_no_automatic_retry"
        reason, action = explanations.get(code, ("監視処理が停止しました。原因の追加確認が必要です", "実行履歴を調査する。自動再送はしない"))
        failure = {"status": "monitor_stopped", "reason": code, "target": args.target,
                   "cause": reason, "next_action": action, "business_recovery_verified": False}
        output = Path(args.output)
        output.mkdir(parents=True, exist_ok=True)
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
