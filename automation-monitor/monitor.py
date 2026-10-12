"""Read-only monitoring prototype. No Slack client, sender, scheduler or retry action.

Fetch: GitHub GET API and read-only run logs via an existing gh login.
Replay: sanitized local evidence only. All outputs are LOCAL previews.
"""
from __future__ import annotations

import argparse
import concurrent.futures
from contextlib import contextmanager
import hashlib
import html
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / "output/261008_自動処理通知_判定プレビュー.html"
JST = timezone(timedelta(hours=9))
CHANNEL_ID = "C0C7QRMJP0R"
CHANNEL_NAME = "marketing-automation-alerts"
CHANNEL_URL = f"https://app.slack.com/client/E0BQA894FGA/{CHANNEL_ID}"
OWNER = "渡辺さん"
POLL_MINUTES = 15
SCHEDULE_GRACE_MINUTES = 90
LABELS = {"action_required": "要対応", "watching": "再確認中", "recovered": "復旧済み", "healthy": "対応不要", "unknown": "判定不能"}
REASONS = {"name_email_conflict", "account_name_mismatch", "conflicting_application_names", "duplicate_email_accounts", "invalid_application", "browser_operation_unconfirmed_review", "run_time_budget_reached", "unexpected_runner_error_review_required", "non_student_account", "inactive_account", "invalid_student_id", "enrollment_name_mismatch", "duplicate_enrollment", "possible_test_application"}
BIZIBL_STEPS = ["Run scraper (applicant data sync)", "Distribute to shared sheets", "Run schedule sync (notification sheet)"]
BRAND_STEPS = ["Read and build (no Sheet writes)", "Deploy", "Verify exact delivery and noindex"]
BRAND_PRODUCTION_BUILD_STEPS = ["Execute checked production operation", "Validate generated production page"]
CONFIG = {
    "bizibl": {"name": "Bizibl同期", "repo": "facilo-corp/facilo-marketing-webinar", "workflow": 301946418,
               "path": ".github/workflows/sync.yml", "cron": ["0 21 * * *", "0 9 * * *"], "slots": [(None, 6, 0), (None, 18, 0)], "max_minutes": 25, "seed": 37688209543},
    "takken": {"name": "宅建講座の自動登録", "repo": "facilo-corp/takken-registration", "workflow": 369036762,
               "path": ".github/workflows/registration.yml", "cron": ["0 0,9 * * *"], "slots": [(None, 9, 0), (None, 18, 0)], "max_minutes": 360, "seed": 37705985768},
    "branded": {"name": "指名検索更新", "repo": "facilo-corp/branded-search-monitoring", "workflow": 378041333,
                "path": ".github/workflows/update.yml", "cron": ["17 0 * * 1"], "slots": [(0, 9, 17)], "max_minutes": 35, "seed": 37722145029},
}
ACTIVE_REPOSITORY = None


@contextmanager
def repository_scope(key):
    """Confine every reader to this monitor's repository, including dry runs."""
    global ACTIVE_REPOSITORY
    if key not in CONFIG:
        raise ValueError("monitor_target_invalid")
    repository = CONFIG[key]["repo"]
    if os.getenv("GITHUB_ACTIONS") == "true" and os.getenv("GITHUB_REPOSITORY") != repository:
        raise ValueError("monitor_repository_mismatch")
    previous = ACTIVE_REPOSITORY
    if previous is not None and previous != repository:
        raise ValueError("cross_repository_read_forbidden")
    ACTIVE_REPOSITORY = repository
    try:
        yield repository
    finally:
        ACTIVE_REPOSITORY = previous


def parse_time(value):
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("An explicit timezone is required")
    return dt.astimezone(timezone.utc)


def iso(value):
    return parse_time(value).isoformat()


def jp(value):
    return parse_time(value).astimezone(JST).strftime("%m/%d %H:%M") if value else "未確認"


def slots_around(cfg, now):
    local = parse_time(now).astimezone(JST)
    slots = []
    for delta in range(-8, 9):
        day = local + timedelta(days=delta)
        for weekday, hour, minute in cfg["slots"]:
            if weekday is None or weekday == day.weekday():
                slots.append(day.replace(hour=hour, minute=minute, second=0, microsecond=0).astimezone(timezone.utc))
    return max(x for x in slots if x <= local), min(x for x in slots if x > local)


class ReadError(Exception):
    pass


class GitHubReader:
    def __init__(self, repo):
        if repo not in {c["repo"] for c in CONFIG.values()}:
            raise ValueError("Repository outside prototype scope")
        if ACTIVE_REPOSITORY is not None and repo != ACTIVE_REPOSITORY:
            raise ValueError("cross_repository_read_forbidden")
        self.repo = repo

    def _run(self, args):
        try:
            env = os.environ.copy()
            if env.get("GITHUB_ACTIONS") == "true":
                if not env.get("GITHUB_TOKEN"):
                    raise ReadError("repository_token_missing")
                env["GH_TOKEN"] = env["GITHUB_TOKEN"]
            result = subprocess.run(["gh", *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, env=env)
        except (OSError, subprocess.TimeoutExpired):
            raise ReadError("github_read_unavailable") from None
        if result.returncode:
            # stderr may contain private data; retain only a fixed reason.
            raise ReadError("github_read_unavailable")
        return result.stdout

    def get(self, suffix, raw=False):
        if not re.fullmatch(r"(?:actions/[A-Za-z0-9_./?=&%-]+|contents/\.github/workflows/[a-z-]+\.yml|contents/distributions\.yml|actions/variables/(?:AUTOMATION_ENABLED|SCHEDULE_ENABLED))", suffix) or ".." in suffix:
            raise ValueError("Endpoint outside read-only scope")
        args = ["api", "--method", "GET", f"repos/{self.repo}/{suffix}"]
        if raw:
            args += ["-H", "Accept: application/vnd.github.raw+json"]
        payload = self._run(args)
        try:
            return payload if raw else json.loads(payload)
        except ValueError:
            raise ReadError("github_invalid_response") from None

    def logs(self, run_id, attempt):
        if not isinstance(run_id, int) or not isinstance(attempt, int):
            raise ValueError("Invalid run identity")
        return self._run(["run", "view", str(run_id), "--repo", self.repo, "--attempt", str(attempt), "--log"])


def safe_count(value):
    return value if type(value) is int and 0 <= value <= 10_000_000 else None


def extract_summary(log):
    """Select the final runner JSON, then whitelist non-personal business counts."""
    cleaned = re.sub(r"(?m)^[^\n]*?\t[^\n]*?\t\d{4}-\d\d-\d\dT[^\s]+\s?", "", log)
    decoder = json.JSONDecoder()
    summaries = []
    for match in re.finditer(r"\{", cleaned):
        try:
            item, _ = decoder.raw_decode(cleaned[match.start():])
        except ValueError:
            continue
        if isinstance(item, dict) and item.get("status") in {"passed", "needs_review", "ready_for_approval", "stopped"}:
            summaries.append(item)
    if not summaries:
        return None
    src = summaries[-1]
    result = {"status": src["status"], "mode": src.get("mode") if src.get("mode") in {"execute", "preflight"} else None}
    for key in ("completed", "invitation_pending", "deferred", "enrolled_without_resend"):
        result[key] = safe_count(src.get(key))
        if result[key] is None and result["mode"] != "preflight":
            result["invalid_counts"] = True
    for field in ("held_reasons", "business_writes"):
        result[field] = {}
        values = src.get(field)
        if not isinstance(values, dict):
            result["invalid_counts"] = True
            continue
        for key, value in values.items():
            count = safe_count(value)
            if count is None:
                result["invalid_counts"] = True
                continue
            allowed = REASONS if field == "held_reasons" else {"account", "enrollment", "invite"}
            safe_key = key if key in allowed else "unrecognized_reason"
            result[field][safe_key] = result[field].get(safe_key, 0) + count
    registry = src.get("review_registry")
    if registry is not None:
        try:
            cases = registry["cases"]
            if not isinstance(cases, list) or len(cases) > 1000:
                raise ValueError()
            safe = []
            for case in cases:
                if (not isinstance(case, dict) or
                    not all(isinstance(case.get(k), str) and re.fullmatch(r"[0-9a-f]{64}", case[k]) for k in ("case_id", "input_version")) or
                    case.get("status") not in {"open", "not_observed", "acknowledged", "resolved", "superseded"} or
                    case.get("reason") not in REASONS):
                    raise ValueError()
                safe.append({k: case[k] for k in ("case_id", "input_version", "status", "reason")})
            opened = [c for c in safe if c["status"] in {"open", "not_observed", "acknowledged"}]
            if len({c["case_id"] for c in opened}) != registry["open"]:
                raise ValueError()
            result["review_cases"] = safe
        except (KeyError, TypeError, ValueError):
            result["invalid_counts"] = True
    return result


def sanitize_run(src, jobs, summary=None, signals=None, prior_failure=False):
    return {"id": int(src["id"]), "attempt": int(src.get("run_attempt", 1)), "event": src["event"],
            "branch": src["head_branch"], "workflow_id": src["workflow_id"], "status": src["status"],
            "conclusion": src.get("conclusion"), "created_at": src["created_at"], "updated_at": src["updated_at"],
            "started_at": src.get("run_started_at") or src["created_at"], "url": src["html_url"],
            "prior_failure": prior_failure, "summary": summary, "signals": signals or {},
            "jobs": [{"name": j["name"], "status": j["status"], "conclusion": j.get("conclusion"),
                      "steps": [{"name": s["name"], "conclusion": s.get("conclusion"), "status": s["status"]} for s in j.get("steps", [])]} for j in jobs]}


def collect_one(key):
    cfg = CONFIG[key]
    reader = GitHubReader(cfg["repo"])
    out = {"key": key, "errors": [], "runs": [], "flags": {}}
    try:
        meta = reader.get(f"actions/workflows/{cfg['workflow']}")
        out["workflow"] = {k: meta[k] for k in ("id", "path", "state", "created_at")}
        source = reader.get("contents/" + cfg["path"], raw=True)
        out["cron"] = re.findall(r"cron:\s*['\"]([^'\"]+)['\"]", source)
        # Config flags are not credentials. Only these boolean variables are read.
        if key in {"takken", "branded"}:
            for flag in (["AUTOMATION_ENABLED", "SCHEDULE_ENABLED"] if key == "branded" else ["AUTOMATION_ENABLED"]):
                # The workflow passes its own vars context. GITHUB_TOKEN does
                # not need Variables API permission or a personal token.
                value = (os.getenv("MONITOR_" + flag) if os.getenv("GITHUB_ACTIONS") == "true"
                         else reader.get(f"actions/variables/{flag}").get("value"))
                out["flags"][flag] = value if value in {"true", "false"} else "unknown"
        raw_runs = reader.get(f"actions/workflows/{cfg['workflow']}/runs?branch=main&per_page=100")["workflow_runs"]
        raw_runs = [r for r in raw_runs if r.get("head_branch") == "main" and r.get("event") in {"schedule", "workflow_dispatch"}]
        schedule_runs = reader.get(f"actions/workflows/{cfg['workflow']}/runs?branch=main&event=schedule&per_page=5")["workflow_runs"]
        out["run_headers"] = [{k: r[k] for k in ("id", "event", "created_at", "status", "conclusion")} for r in schedule_runs if r.get("head_branch") == "main" and r.get("workflow_id") == cfg["workflow"]]
        # Always retain scheduled runs even after many manual readiness checks.
        # Old seed logs may expire; they are not required for a current verdict.
        candidates = list(raw_runs)
        for scheduled_run in schedule_runs[:3]:
            if scheduled_run["id"] not in {r["id"] for r in candidates}:
                candidates.append(scheduled_run)
        candidates.sort(key=lambda r: r["created_at"], reverse=True)
        for index, src in enumerate(candidates):
            if index >= 40:
                raise ReadError("history_window_requires_review")
            if src["workflow_id"] != cfg["workflow"]:
                raise ReadError("workflow_identity_mismatch")
            attempt = int(src.get("run_attempt", 1))
            jobs = reader.get(f"actions/runs/{src['id']}/attempts/{attempt}/jobs?per_page=100")["jobs"]
            summary, signals, prior_failure = None, {}, False
            if key in {"bizibl", "takken"} and src["status"] == "completed":
                log = reader.logs(int(src["id"]), attempt)
                if key == "takken":
                    summary = extract_summary(log)
                else:
                    rows = re.findall(r"Googleスプレッドシート出力完了:\s*(\d+)行", log)
                    signals = {"rows_written": int(rows[-1]) if rows else None,
                               "click_timeout": 'Locator.click: Timeout' in log}
                del log
            if attempt > 1:
                first = reader.get(f"actions/runs/{src['id']}/attempts/1")
                prior_failure = first.get("conclusion") in {"failure", "timed_out", "cancelled"}
            out["runs"].append(sanitize_run(src, jobs, summary, signals, prior_failure))
            if sum(production_run(key, r) for r in out["runs"]) >= 3:
                break
    except (ReadError, KeyError, TypeError, ValueError):
        out["errors"].append("evidence_read_incomplete")
    return out


def collect(target=None):
    # Independent repositories; one read failure does not hide the other results.
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        evidence = list(pool.map(collect_one, [target] if target else CONFIG))
    return {"schema": 1, "fetched_at": iso(datetime.now(timezone.utc)), "workflows": evidence}


def step(run, name):
    values = [s["conclusion"] for j in run.get("jobs", []) for s in j.get("steps", []) if s["name"] == name]
    return values[0] if len(values) == 1 else None


def branded_publication_verified(run):
    """Accept either reviewed build route, followed by publish and delivery checks."""
    stages = {name: [s["conclusion"] for j in run.get("jobs", []) for s in j.get("steps", [])
                     if s["name"] == name]
              for name in BRAND_STEPS + BRAND_PRODUCTION_BUILD_STEPS}
    if run.get("conclusion") != "success" or any(len(values) > 1 for values in stages.values()):
        return False
    values = {name: rows[0] if rows else None for name, rows in stages.items()}
    if not all(values[name] == "success" for name in BRAND_STEPS[1:]):
        return False
    legacy = values[BRAND_STEPS[0]]
    production = [values[name] for name in BRAND_PRODUCTION_BUILD_STEPS]
    return ((legacy == "success" and all(value in {None, "skipped"} for value in production))
            or (legacy in {None, "skipped"} and all(value == "success" for value in production)))


def production_run(key, run):
    if run.get("branch") != "main" or run.get("workflow_id") != CONFIG[key]["workflow"] or run.get("event") not in {"schedule", "workflow_dispatch"}:
        return False
    if key == "takken" and (run.get("summary") or {}).get("mode") == "preflight":
        return False
    if key == "branded" and run["event"] == "workflow_dispatch":
        if step(run, "Collect and compare without writing") not in {None, "skipped"}:
            return False
        # Successful build-only checks are not publication/recovery evidence.
        if run["conclusion"] == "success" and step(run, "Deploy") != "success":
            return False
    return True


def base_result(key, now):
    cfg = CONFIG[key]
    _, upcoming = slots_around(cfg, now)
    return {"key": key, "name": cfg["name"], "state": "unknown", "condition": "no_evidence",
            "title": "結果を確認できません", "impact": "対応不要とは判断できません", "done": "未確認",
            "next_action": "実行履歴への接続と処理結果を確認する", "owner": OWNER, "checked_at": iso(now),
            "next_check": iso(parse_time(now) + timedelta(minutes=POLL_MINUTES)), "deadline": iso(upcoming),
            "evidence": [], "business_data_checked": False, "details": [], "signature_data": {}}


def assess(key, evidence, now):
    now = parse_time(now)
    cfg = CONFIG[key]
    result = base_result(key, now)
    if evidence.get("errors"):
        result.update(condition="read_unavailable", title="実行履歴を取得できず、状況を判定できません")
        return result
    meta = evidence.get("workflow", {})
    if meta.get("id") != cfg["workflow"] or meta.get("path") != cfg["path"] or set(evidence.get("cron", [])) != set(cfg["cron"]):
        result.update(condition="contract_changed", title="監視対象または実行時刻が変わっています", next_action="新しい設定に監視の判定基準を合わせる")
        return result
    if meta.get("state") != "active" or any(v != "true" for v in evidence.get("flags", {}).values()):
        result.update(condition="schedule_disabled", title="定期処理の停止・有効化設定を確認してください", next_action="意図した停止かを確認する。監視から勝手に再開しない")
        return result
    slot, upcoming = slots_around(cfg, now)
    result["next_scheduled"] = iso(upcoming)
    eligible = [r for r in evidence.get("runs", []) if production_run(key, r)]
    eligible.sort(key=lambda r: (parse_time(r["created_at"]), r["id"]), reverse=True)
    if not eligible:
        return result
    latest = eligible[0]
    result["evidence"] = [latest["url"]]
    result["latest_run"] = latest["id"]
    result["result_at"] = latest["updated_at"]
    # Missing schedule is independent of manual recovery; compare exact due window.
    scheduled = [r for r in evidence.get("run_headers", []) if r["event"] == "schedule" and slot <= parse_time(r["created_at"]) <= now]
    if slot >= parse_time(meta["created_at"]) and now > slot + timedelta(minutes=SCHEDULE_GRACE_MINUTES) and not scheduled:
        result.update(state="action_required", condition="schedule_missing", title="予定の定期処理が始まっていません",
                      impact=f"{jp(slot)}予定の実行記録がなく、通常の更新を確認できません",
                      next_action="定期実行の設定とGitHubの稼働状況を確認する", signature_data={"due": iso(slot)})
        return result
    if latest["status"] != "completed":
        slow = now > parse_time(latest["started_at"]) + timedelta(minutes=cfg["max_minutes"])
        result.update(state="action_required" if slow else "watching", condition="run_stalled" if slow else "running",
                      title="処理が通常の上限時間を超えています" if slow else "処理中です",
                      impact="まだ完了していません", done="実行開始を確認", next_action="停止箇所を確認する" if slow else "15分後に完了結果を再確認する")
        return result
    historical_failure = any(r.get("conclusion") in {"failure", "timed_out", "cancelled"} or r.get("prior_failure") for r in eligible)
    if key == "takken":
        s = latest.get("summary")
        if not s or s.get("invalid_counts") or s.get("mode") != "execute":
            result.update(condition="business_summary_missing", title="登録結果の内訳を確認できません")
            return result
        held = s.get("held_reasons", {})
        conflicts = held.get("name_email_conflict", 0)
        count = sum(held.values())
        writes = s.get("business_writes", {})
        result["done"] = f"今回の会員作成 {writes.get('account', 0)}件・履修追加 {writes.get('enrollment', 0)}件・招待受付 {writes.get('invite', 0)}件"
        result["details"].append(f"招待結果の確認待ち {s.get('invitation_pending') or 0}件（送信失敗とは区別）")
        application_reasons = REASONS - {"browser_operation_unconfirmed_review", "run_time_budget_reached", "unexpected_runner_error_review_required"}
        if count and not set(held) <= application_reasons:
            result.update(state="action_required", condition="registration_incomplete", title="登録処理の停止状況を確認してください",
                impact="処理結果が確定していない申込みがあります", next_action="保存済みの登録・招待履歴を照合し、停止原因を確認する。一括再実行や招待再送はしない",
                signature_data={"held_reasons": held})
            return result
        if count:
            result.update(state="action_required", condition="business_hold", title=f"申込み{count}件の照合が必要です",
                          impact=f"同じ氏名・別メールのため{conflicts}件を保留しています" if conflicts == count else f"申込み{count}件が保留されています",
                          next_action="保留対象の申込内容と既存会員を照合し、同一人物か確認する。確認前に再登録・招待再送はしない",
                          signature_data={"holds": held, "source_run": latest["id"]})
            result["details"].append("保留対象を特定するIDがログにないため、別の実行で同じ件数でも同じ人の案件とは断定しません")
            case_source = latest
            # A newer preflight can confirm current held cases. It never proves
            # execution recovery and never clears an older execution failure.
            newer = sorted([r for r in evidence.get("runs", []) if r.get("conclusion") == "success"
                and parse_time(r["updated_at"]) >= parse_time(latest["updated_at"])
                and (r.get("summary") or {}).get("review_cases") is not None
                and not (r.get("summary") or {}).get("invalid_counts")],
                key=lambda r: r["updated_at"], reverse=True)
            if newer:
                case_source = newer[0]
            cases = (case_source.get("summary") or {}).get("review_cases")
            if cases is not None:
                active = [c for c in cases if c["status"] in {"open", "not_observed", "acknowledged"}]
                counts = {}
                for c in active:
                    counts[c["reason"]] = counts.get(c["reason"], 0) + 1
                if counts == held:
                    result["signature_data"] = {"cases": sorted((c["case_id"], c["input_version"], c["status"]) for c in active)}
                    result["details"][-1] = "保留案件番号と入力の版で重複を抑制します。事前確認の成功を通常実行の復旧とは扱いません"
                    if case_source["id"] != latest["id"]:
                        result["evidence"].append(case_source["url"])
        elif s["status"] == "passed" and latest["conclusion"] == "success" and not s.get("deferred"):
            pending = s.get("invitation_pending") or 0
            result.update(state="watching" if pending else ("recovered" if historical_failure else "healthy"),
                          condition="invitation_wait" if pending else "registration_passed",
                          title="登録済み・招待の送信結果を確認待ちです" if pending else "登録処理は正常です",
                          impact=f"招待結果{pending}件は未確定です" if pending else "実行結果に保留はありません",
                          next_action="次回実行で同じ招待の受付結果を確認する。再送しない" if pending else "今回の処理への追加対応は不要です",
                          signature_data={"pending": pending})
        else:
            result.update(state="action_required", condition="registration_incomplete", title="登録処理が完了していません", impact="一部処理が残っているか、停止しています", next_action="処理結果と保存済み台帳を照合する。全件を一括で再実行しない")
        return result
    if key == "bizibl" and step(latest, BIZIBL_STEPS[0]) == "success":
        stages = {name: step(latest, name) for name in BIZIBL_STEPS}
        rows = latest.get("signals", {}).get("rows_written")
        result["done"] = f"申込データの取得・元シートへの出力 {rows:,}行をログで確認" if rows is not None else "申込データ取得の工程は成功"
        result["signature_data"] = {"stages": stages}
        if all(v == "success" for v in stages.values()) and latest["conclusion"] == "success" and rows is not None:
            result.update(state="recovered" if historical_failure else "healthy", condition="sync_passed", title="同期の全工程が完了しています",
                          impact="取得・共有シート配布・開催予定同期の成功を確認", next_action="今回の失敗への追加対応は不要です")
        elif latest["conclusion"] == "success" and any(v == "skipped" for v in stages.values()) and all(v in {"success", "skipped"} for v in stages.values()):
            _, after_result = slots_around(cfg, parse_time(latest["updated_at"]))
            deadline = after_result + timedelta(minutes=SCHEDULE_GRACE_MINUTES)
            result.update(state="watching" if now < deadline else "action_required", condition="partial_recovery", title="データ取得は復旧、後続の処理は未確認です",
                          impact="共有シート配布・開催予定同期に未実行の工程があり、全体の復旧は未確認です",
                          next_action=f"{jp(after_result)}以降の通常実行で後続工程を確認する。{jp(deadline)}までに完了しなければ原因を調べる",
                          deadline=iso(deadline))
        else:
            result.update(state="action_required", condition="sync_incomplete", title="同期の後続工程が完了していません", impact="共有先または開催予定が古い可能性があります", next_action="未完了工程と出力先を確認する")
        result["details"].append("出力先シートの実値はこの試作では直接照合していません")
        return result
    if key == "branded" and latest["conclusion"] == "success":
        if branded_publication_verified(latest):
            result.update(state="recovered" if historical_failure else "healthy", condition="publication_verified", title="公開と配信内容の検証まで完了しています",
                          impact="今回の失敗への追加対応は不要です", done="データ読取・公開・配信内容の一致検証が成功", next_action="次回の定期更新で結果を確認する")
            result["details"].append("後から成功した照合専用の試験実行は、公開成功の根拠に使っていません")
            return result
        result.update(condition="publication_not_verified", title="公開先の検証が完了したか確認できません")
        return result
    if latest["conclusion"] in {"failure", "timed_out", "cancelled", "action_required"}:
        recent = now - parse_time(latest["updated_at"]) < timedelta(minutes=POLL_MINUTES)
        timeout = latest.get("signals", {}).get("click_timeout", False)
        result.update(state="watching" if recent else "action_required", condition="click_timeout" if timeout else "execution_failed",
                      title="画面操作の待ち時間を超えて同期が止まりました" if timeout else "処理が正常に完了していません",
                      impact="今回の更新を確認できません", done="失敗後の復旧は未確認",
                      next_action="15分後に再実行・復旧の有無を確認する" if recent else "失敗した工程を確認し、復旧方法を判断する")
    return result


def preview_event(result, previous, now):
    """Local simulation only; deliberately has no sent/published acknowledgement."""
    now = parse_time(now)
    condition_hash = hashlib.sha256(json.dumps([result["condition"], result["signature_data"]], sort_keys=True).encode()).hexdigest()
    same_issue = previous and previous.get("condition_hash") == condition_hash
    if same_issue:
        result["deadline"] = previous["deadline"]
        if result["state"] == "watching" and now >= parse_time(previous["deadline"]) and result["condition"] in {"partial_recovery", "invitation_wait"}:
            result.update(state="action_required", title="確認期限を過ぎても完了を確認できていません",
                          next_action="未完了の工程・元の招待受付結果を確認する。結果不明の処理を再送しない" if result["condition"] == "invitation_wait" else "未完了の共有先配布・開催予定同期を確認する")
    signature = hashlib.sha256(json.dumps([result["state"], condition_hash]).encode()).hexdigest()
    changed = not previous or previous.get("signature") != signature
    if result["state"] in {"healthy", "recovered"}:
        kind = "recovery" if previous and previous.get("state") in {"action_required", "watching", "unknown"} else "digest"
    elif changed:
        kind = "new" if not previous else "changed"
    elif now >= parse_time(previous["deadline"]) and now >= parse_time(previous["last_preview_at"]) + timedelta(hours=24):
        kind = "reminder"
    else:
        kind = "suppressed"
    # Keep due time fixed while the same issue continues, rather than postponing it every poll.
    deadline = previous["deadline"] if same_issue else result["deadline"]
    result["deadline"] = deadline
    stamp = iso(now) if kind != "suppressed" else previous["last_preview_at"]
    state = {"signature": signature, "condition_hash": condition_hash, "state": result["state"], "deadline": deadline,
             "last_preview_at": stamp, "observed_at": iso(now), "simulation_only": True}
    return kind, state


def message(result):
    lines = [f"【{LABELS[result['state']]}】{result['name']}｜{result['title']}",
             f"影響：{result['impact']}", f"確認できたこと：{result['done']}",
             f"次の対応：{result['next_action']}"]
    if result["state"] in {"action_required", "unknown"}:
        lines.append(f"対応窓口：{OWNER}")
    else:
        lines.append("追加対応：今すぐの手作業は不要" if result["state"] != "watching" else "次の確認：監視が15分後に再確認する想定")
    lines.append(f"判定時点：{jp(result['checked_at'])} JST")
    if result["evidence"]:
        lines.append("根拠：" + result["evidence"][0])
    return "\n".join(lines)


def build_report(snapshot, previous=None, now=None, target=None):
    now = parse_time(now or snapshot["fetched_at"])
    old = (previous or {}).get("workflows", {})
    by_key = {e["key"]: e for e in snapshot["workflows"]}
    results, next_state = [], {}
    stale = now - parse_time(snapshot["fetched_at"]) > timedelta(minutes=30)
    future = parse_time(snapshot["fetched_at"]) > now + timedelta(minutes=1)
    for key in ([target] if target else CONFIG):
        evidence = by_key.get(key, {"errors": ["missing"]})
        if stale or future:
            evidence = {"errors": ["stale_snapshot"]}
        result = assess(key, evidence, now)
        if stale:
            result.update(title="取得した情報が古いため再取得が必要です", condition="stale_snapshot")
        if future:
            result.update(title="確認時刻より後の情報が含まれるため判定できません", condition="future_snapshot")
        event, next_state[key] = preview_event(result, old.get(key), now)
        result["preview_event"] = event
        result["message_text"] = message(result)
        results.append(result)
    results.sort(key=lambda r: {"action_required": 0, "unknown": 1, "watching": 2, "recovered": 3, "healthy": 4}[r["state"]])
    report = {"schema": 1, "mode": "local_preview_only", "generated_at": iso(now), "evidence_at": snapshot["fetched_at"],
              "destination": {"id": CHANNEL_ID, "name": CHANNEL_NAME, "url": CHANNEL_URL},
              "slack_messages_sent": 0, "scheduled": False, "results": results,
              "limitations": ["本番の定期実行・Slack接続は未設定", "シートの実値・未反映の個人別件数は未照合", "監視自身の停止を外から検知する機構は未実装", "氏名・メール・生ログは通知に含めない"]}
    return report, {"schema": 1, "simulation_only": True, "observed_at": iso(now), "workflows": next_state}


def render_html(report):
    e = lambda v: html.escape(str(v), quote=True)
    badges = "".join(f'<span class="count {state}">{e(label)} <b>{sum(r["state"] == state for r in report["results"])}</b></span>' for state, label in LABELS.items() if any(r["state"] == state for r in report["results"]))
    cards = []
    timings = {"new": "初回の通知候補", "changed": "状態変化の通知候補", "suppressed": "同じ状態のため追加通知なし", "digest": "通常の確認一覧に記録", "recovery": "復旧の通知候補", "reminder": "期限超過の再通知候補"}
    for r in report["results"]:
        links = " ".join(f'<a href="{e(url)}" target="_blank" rel="noreferrer">実行記録を確認 ↗</a>' for url in r["evidence"])
        details = "".join(f'<li>{e(d)}</li>' for d in r["details"])
        cards.append(f'''<article class="card {e(r['state'])}"><div class="cardhead"><span class="badge">{e(LABELS[r['state']])}</span><span>{e(r['name'])}</span></div>
<h2>{e(r['title'])}</h2><p class="impact">{e(r['impact'])}</p><dl><dt>確認できたこと</dt><dd>{e(r['done'])}</dd><dt>次の対応</dt><dd>{e(r['next_action'])}</dd></dl>
<div class="foot">{e(timings[r['preview_event']])} · 結果 {e(jp(r.get('result_at')))} JST</div>
<details open><summary>Slackに届く文面の見本 <small>未送信</small></summary><pre>{e(r['message_text'])}</pre></details>
<details><summary>確認範囲・根拠</summary><ul>{details or '<li>GitHubの実行結果に基づく判定です</li>'}</ul>{links}</details></article>''')
    return f'''<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow"><title>自動処理の通知・判定プレビュー</title><style>
:root{{--ink:#18283d;--sub:#546579;--line:#dbe3ed}}*{{box-sizing:border-box}}body{{margin:0;background:#f4f6fa;color:var(--ink);font:15px/1.75 "Yu Gothic UI","Meiryo",sans-serif}}main{{max-width:1100px;margin:auto;padding:38px 24px 70px}}header{{border-bottom:1px solid var(--line);padding-bottom:25px}}.eyebrow{{font-size:13px;color:var(--sub);letter-spacing:.08em}}h1{{font-size:29px;margin:5px 0 7px;line-height:1.4}}h2{{font-size:20px;line-height:1.55;margin:14px 0 8px}}p{{margin:7px 0}}a{{color:#205aab}}.notice{{background:#e7edf8;border-radius:8px;padding:13px 17px;margin:22px 0}}.counts{{display:flex;gap:10px;flex-wrap:wrap;margin:18px 0}}.count{{padding:7px 13px;border:1px solid var(--line);border-radius:6px;background:white}}.count b{{margin-left:8px}}.card{{border:1px solid var(--line);border-left:5px solid #758399;border-radius:10px;background:white;padding:23px;margin:17px 0}}.action_required{{border-left-color:#b73938}}.watching{{border-left-color:#b67b11}}.recovered,.healthy{{border-left-color:#25815d}}.unknown{{border-left-color:#7655a0}}.cardhead{{display:flex;align-items:center;gap:13px;font-weight:650}}.badge{{background:#edf1f6;font-size:12px;border-radius:4px;padding:3px 9px}}.action_required .badge{{background:#fbe7e7;color:#912626}}.watching .badge{{background:#fff0ce;color:#80530a}}.recovered .badge,.healthy .badge{{background:#def3e9;color:#216144}}.impact{{color:#46566b}}dl{{display:grid;grid-template-columns:132px 1fr;gap:7px 12px;margin:18px 0}}dt{{font-size:13px;color:var(--sub)}}dd{{margin:0}}.foot{{color:var(--sub);font-size:12px;border-top:1px solid #edf0f5;padding-top:13px}}details{{margin-top:15px}}summary{{cursor:pointer;font-size:14px;font-weight:650}}small{{font-weight:400;color:#6b7686;margin-left:8px}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#f6f7fa;border:1px solid #e4e8ef;border-radius:7px;padding:17px;font:14px/1.85 "Yu Gothic UI","Meiryo",sans-serif}}.rules{{background:white;border:1px solid var(--line);border-radius:9px;padding:22px;margin-top:26px}}.rules h2{{margin-top:0}}table{{width:100%;border-collapse:collapse;font-size:14px}}td,th{{text-align:left;border-bottom:1px solid #e6eaf0;padding:10px 8px;vertical-align:top}}th{{width:27%;color:#4c5e74}}footer{{font-size:12px;color:var(--sub);margin-top:22px}}@media(max-width:620px){{main{{padding:22px 13px 40px}}h1{{font-size:24px}}.card{{padding:17px}}dl{{grid-template-columns:1fr;gap:3px}}dd{{margin-bottom:10px}}th{{width:35%}}}}
</style></head><body><main><header><div class="eyebrow">マーケティングの自動処理 / 確認用の試作</div><h1>必要な対応が分かる通知へ</h1><p>通知先：<a href="{e(CHANNEL_URL)}">#{CHANNEL_NAME}</a>　対応窓口：渡辺さん</p><p class="eyebrow">取得・判定時点 {e(jp(report['evidence_at']))} JST</p></header>
<div class="notice"><strong>実際の履歴から作った見本です。Slackへの送信は0件。</strong><br>下の文章が通知候補です。15分ごとの定期監視はまだ開始していません。</div><div class="counts">{badges}</div>{''.join(cards)}
<section class="rules"><h2>通知するタイミング（試作の設定）</h2><table><tr><th>要対応・判定不能</th><td>初回と状態変化で通知候補を作る。状況が分からないものを「正常」にしない</td></tr><tr><th>一時的な失敗</th><td>失敗から15分以内は再確認中。正常化を確認できなければ要対応へ</td></tr><tr><th>予定の実行がない</th><td>予定時刻から90分を過ぎても実行が始まらなければ要対応。GitHubの遅延を考慮した仮の猶予</td></tr><tr><th>同じ問題が続く</th><td>追加通知を抑制。確認期限を過ぎ、前回の候補作成から24時間経過した場合に再通知候補</td></tr><tr><th>復旧した</th><td>前回の問題から復旧へ変化した場合に1回。初回確認時に既に復旧していたものは通常一覧へ</td></tr></table></section>
<section class="rules"><h2>この試作で確認できる範囲</h2><p>実行の成功・失敗、工程の省略、宅建の保留内訳、再試行による復旧、予定実行の欠落を判定します。氏名・メール・生ログは通知に含めません。</p><p>シートの実値との照合と、監視自体の停止を別経路で知らせる機構は、本番化前に追加する項目です。今回の試作から本番設定や業務処理を変更する機能はありません。</p></section><footer>確認版 / 自動送信・本番稼働は未承認。再確認時刻や期限は、定期監視を開始した場合の運用案です。</footer></main></body></html>'''


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--fetch", action="store_true")
    modes.add_argument("--replay", type=Path)
    parser.add_argument("--now", help="Only for replay; timezone-aware ISO timestamp")
    parser.add_argument("--work-dir", type=Path, default=ROOT / "work/261008_automation_monitor")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if args.fetch and args.now:
        parser.error("--now is only available for local replay")
    snapshot = collect() if args.fetch else json.loads(args.replay.read_text(encoding="utf-8"))
    state_path = args.work_dir / "preview-state.json"
    previous = None
    if state_path.exists():
        try:
            previous = json.loads(state_path.read_text(encoding="utf-8"))
            if previous.get("schema") != 1 or previous.get("simulation_only") is not True:
                raise ValueError("Unrecognized state")
        except (ValueError, KeyError):
            # Do not erase a corrupt state or lose unresolved issues silently.
            raise SystemExit("Local preview state is unreadable; preserve it and inspect before continuing")
    report, state = build_report(snapshot, previous, args.now)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render_html(report), encoding="utf-8")
    write_json(args.work_dir / "evidence.json", snapshot)
    write_json(args.work_dir / "report.json", report)
    write_json(state_path, state)
    print(json.dumps({"mode": report["mode"], "slack_messages_sent": 0,
                      "results": [{"name": r["name"], "state": r["state"], "condition": r["condition"], "event": r["preview_event"]} for r in report["results"]]}, ensure_ascii=False))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
