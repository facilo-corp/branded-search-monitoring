"""Read-only verification adapters. Business rows stay in memory only."""
from collections import Counter
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import tempfile

import monitor as m

SOURCE = "1M2qtlvbFRIUiyYq1S7FYejSBs2CIO_glAzT9nGOj6JE"
TARGET = "1V_EtYgN50x5BDO-UWU1WoVFQtNix5lgElJtF1g0D9O8"
SOURCE_TAB, TARGET_TAB = "申込者データ", "申込者リスト"
DASHBOARDS = {"email-dashboard.yml": (371741379, "メール実績", "email-dashboard"),
              "paid-mail-dashboard.yml": (374330278, "広告実績", "paid-mail-dashboard")}


def need(ok, reason):
    if not ok:
        raise m.ReadError(reason)


def values_key(rows):
    normalized = [list(r) for r in rows]
    for r in normalized:
        while r and r[-1] == "":
            r.pop()
    while normalized and not normalized[-1]:
        normalized.pop()
    return hashlib.sha256(json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def compare_distribution(source, target, config):
    need(source and len(source) > 1, "source_empty")
    headers, rows = source[0], source[1:]
    need(len(headers) == len(set(headers)), "duplicate_source_headers")
    need(config["spreadsheet_id"] == TARGET and config["sheet_name"] == TARGET_TAB, "distribution_target_changed")
    selected = rows
    for field, condition in config.get("filter", {}).items():
        need(field in headers, "filter_column_missing")
        index = headers.index(field)
        allowed = condition if isinstance(condition, list) else [condition]
        selected = [r for r in selected if len(r) > index and r[index] in allowed]
    names = config.get("columns", "auto")
    if names == "auto":
        names = [h for i, h in enumerate(headers) if h not in config.get("exclude", [])
                 and any(len(r) > i and str(r[i]).strip() for r in selected)]
    need(isinstance(names, list) and names and all(h in headers for h in names), "selected_column_missing")
    indices = [headers.index(h) for h in names]
    expected = [names] + [[r[i] if len(r) > i else "" for i in indices] for r in selected]
    need(values_key(expected) == values_key(target), "distribution_mismatch")
    need("イベントID" in headers, "event_column_missing")
    i = headers.index("イベントID")
    events = Counter(r[i] for r in rows if len(r) > i and r[i])
    return {"source_rows": len(rows), "event_count": len(events), "shared_rows": len(selected),
            "shared_columns": len(names), "distribution_equal": True}


def sheets_check(expected_rows):
    import yaml
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build
    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    raw = os.getenv("GCP_SA_KEY_JSON")
    if raw:
        credentials = Credentials.from_service_account_info(json.loads(raw.lstrip("\ufeff")), scopes=scopes)
    else:
        credentials = Credentials.from_service_account_file(str(m.ROOT / "tools/bizibl-scraper/gcp-sa-key.json"), scopes=scopes)
    api = build("sheets", "v4", credentials=credentials, cache_discovery=False).spreadsheets()
    reader = m.GitHubReader(m.CONFIG["bizibl"]["repo"])
    config = yaml.safe_load(reader.get("contents/distributions.yml", raw=True))["distributions"]
    need(len(config) == 1, "distribution_scope_changed")
    data = []
    for sid, title in ((SOURCE, SOURCE_TAB), (TARGET, TARGET_TAB)):
        meta = api.get(spreadsheetId=sid, fields="sheets.properties").execute()
        names = [s["properties"]["title"] for s in meta["sheets"]]
        need(not any(n.startswith("__sync_stage_") for n in names), "sheet_writer_in_progress_or_interrupted")
        data.append(api.values().get(spreadsheetId=sid, range=f"'{title}'!A:ZZ", valueRenderOption="FORMATTED_VALUE").execute().get("values", []))
    need(len(data[0])-1 == expected_rows, "source_changed_since_run")
    return compare_distribution(*data, config[0])


def downstream_check(reader, upstream, now):
    statuses = []
    for workflow, (wid, name, prefix) in DASHBOARDS.items():
        runs = reader.get(f"actions/workflows/{wid}/runs?branch=main&event=workflow_run&per_page=20")["workflow_runs"]
        found = None
        for run in runs:
            if run["created_at"] < upstream["updated_at"]:
                continue
            need(run["head_branch"] == "main" and run["workflow_id"] == wid, "downstream_identity_mismatch")
            # Completed verification artifacts bind this run to its exact source.
            if run["status"] != "completed":
                continue
            if run["conclusion"] != "success":
                import re
                log = reader.logs(int(run["id"]), int(run.get("run_attempt", 1)))
                bindings = re.findall(r"UPSTREAM_RUN_ID:\s*(\d+)", log)
                del log
                if str(upstream["id"]) not in bindings:
                    # A delayed, unrelated failed workflow_run is not proof
                    # that this source run's refresh failed.
                    continue
                found = {"name": name, "status": "failed", "url": run["html_url"]}
                break
            with tempfile.TemporaryDirectory(prefix="monitor-aggregate-") as directory:
                reader._run(["run", "download", str(run["id"]), "--repo", reader.repo,
                             "--name", f"{prefix}-{run['id']}", "--dir", directory])
                path = Path(directory) / "summary.json"
                need(path.exists() and path.stat().st_size < 1_000_000, "summary_unavailable")
                summary = json.loads(path.read_text(encoding="utf-8"))
            if str(summary["upstream"]["run_id"]) != str(upstream["id"]):
                continue
            need(summary["mode"] == "apply" and summary["write_verified"] is True, "aggregate_not_verified")
            found = {"name": name, "status": "verified", "url": run["html_url"]}
            break
        if found is None:
            overdue = m.parse_time(now) > m.parse_time(upstream["updated_at"]) + timedelta(minutes=40)
            found = {"name": name, "status": "missing" if overdue else "waiting"}
        statuses.append(found)
    return statuses


def enrich(report, snapshot, *, check_sheets=sheets_check, check_downstream=downstream_check):
    result = next((r for r in report["results"] if r["key"] == "bizibl"), None)
    if result is None:
        return report
    if result["condition"] != "sync_passed":
        return report
    evidence = next(e for e in snapshot["workflows"] if e["key"] == "bizibl")
    latest = next(r for r in evidence["runs"] if r["id"] == result["latest_run"])
    reader = m.GitHubReader(m.CONFIG["bizibl"]["repo"])
    try:
        data = check_sheets(latest["signals"]["rows_written"])
        downstream = check_downstream(reader, latest, report["generated_at"])
        # The source may be updated while we compare; don't certify mixed runs.
        current = reader.get(f"actions/workflows/{m.CONFIG['bizibl']['workflow']}/runs?branch=main&per_page=1")["workflow_runs"]
        need(current and current[0]["id"] == latest["id"] and current[0]["status"] == "completed", "source_run_changed_during_read")
        result["business_data_checked"] = True
        result["verification"] = data | {"downstream": downstream}
        result["details"] = ["元シートと共有先を読み取り専用で全件照合。個人データは保存していません"]
        result["done"] = f"申込データ{data['source_rows']:,}行、共有先{data['shared_rows']}行・{data['shared_columns']}列の一致を確認"
        statuses = [d["status"] for d in downstream]
        result["signature_data"]["downstream"] = statuses
        if any(s in {"failed", "missing"} for s in statuses):
            result.update(state="action_required", condition="downstream_incomplete", title="後続の集計が完了していません",
                impact="元の申込データは更新済みですが、メール・広告の集計が古い可能性があります",
                next_action="未完了の集計の実行記録を確認する。申込データの同期全体は再実行しない")
        elif "waiting" in statuses:
            result.update(state="watching", condition="downstream_waiting", title="データ同期済み、後続集計の完了待ちです",
                impact="メール・広告の集計は更新待ちです", next_action="次の監視で集計結果を再確認する")
        else:
            result["impact"] = "データ保存・共有転記・後続の2集計まで確認できています"
        result["evidence"].extend(d["url"] for d in downstream if "url" in d)
    except Exception as exc:
        code = str(exc) if isinstance(exc, m.ReadError) else "business_evidence_unavailable"
        result.update(state="unknown", condition="business_evidence_unavailable", title="データの保存・転記結果を確認できません",
            impact="実行記録の成功だけでは対応不要と判断できません", next_action="保存先への接続とデータ照合結果を確認する",
            signature_data={"reason": code}, business_data_checked=False)
        result["details"] = ["業務データの内容や接続エラーの原文は出力していません"]
    for r in report["results"]:
        r["message_text"] = m.message(r)
    return report
