"""Incident/outbox transitions and approval-gated, single-attempt Slack delivery."""
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import uuid

import monitor as m

TEMPLATE_VERSION = "marketing-alerts-v1"
EVENTS = {"new", "changed", "reminder", "recovery", "test"}
TEST_KEY = "__delivery_test__"
TEST_MESSAGE = "【通知テスト】マーケティング自動処理の監視通知です。対応は不要です。"


def need(condition, code):
    if not condition:
        raise ValueError(code)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def initial_state():
    return {"schema": 1, "purpose": "marketing-alert-monitor-v1", "revision": 0,
            "owner": None, "operation_id": "initial", "heartbeat": None,
            "workflows": {}, "outbox": {}}


def validate(state):
    need(isinstance(state, dict) and set(state) == set(initial_state()), "state_fields_invalid")
    need(state["schema"] == 1 and state["purpose"] == "marketing-alert-monitor-v1", "state_identity_invalid")
    need(type(state["revision"]) is int and state["revision"] >= 0, "state_revision_invalid")
    need(state["owner"] is None or isinstance(state["owner"], str), "state_owner_invalid")
    need(isinstance(state["workflows"], dict) and set(state["workflows"]) <= set(m.CONFIG), "state_scope_invalid")
    need(isinstance(state["outbox"], dict) and len(json.dumps(state)) < 900_000, "state_size_invalid")
    for eid, row in state["outbox"].items():
        need(row["id"] == eid and re.fullmatch(r"[0-9a-f]{64}", eid), "outbox_id_invalid")
        need(row["key"] in {*m.CONFIG, TEST_KEY} and row["channel"] == m.CHANNEL_ID and row["event"] in EVENTS, "outbox_scope_invalid")
        if row["event"] == "test" or row["key"] == TEST_KEY:
            need(row["event"] == "test" and row["key"] == TEST_KEY and row["text"] == TEST_MESSAGE,
                 "test_message_scope_invalid")
        need(row["status"] in {"pending", "sending", "sent", "needs_review", "superseded"}, "outbox_status_invalid")
        need(row["text_sha256"] == digest(row["text"]) and isinstance(row["text"], str) and len(row["text"]) < 3500, "outbox_text_invalid")
        if row["status"] == "sent":
            need(row["receipt"]["channel"] == m.CHANNEL_ID and re.fullmatch(r"\d+\.\d+", row["receipt"]["ts"]), "receipt_invalid")
    return state


def message(result, event):
    label = "確認待ち" if result["condition"] == "business_hold" else m.LABELS[result["state"]]
    if event == "recovery":
        label = "復旧済み"
    if event == "reminder":
        label += "・継続"
    lines = [f"【{label}】{result['name']}", f"影響：{result['impact']}",
             f"確認結果：{result['done']}", f"対応：{result['next_action']}"]
    if event != "recovery":
        lines.append("確認担当：渡辺さん")
    lines.append(f"確認時刻：{m.jp(result['checked_at'])} JST")
    if result["evidence"]:
        # Only links to the configured private repositories can reach Slack.
        allowed = [f"https://github.com/{c['repo']}/actions/runs/" for c in m.CONFIG.values()]
        links = result["evidence"][:3]
        need(all(any(re.fullmatch(re.escape(p)+r"\d+", url) for p in allowed) for url in links), "evidence_url_invalid")
        lines.extend(links)
    text = "\n".join(lines)
    need(len(text) < 3500 and not re.search(r"<[@!]|xox[baprs]-|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", text), "message_contains_unapproved_content")
    return text


def validate_target(state, target):
    """Reject a mixed or wrongly deployed ledger before any send or write."""
    validate(state)
    need(target in m.CONFIG, "state_target_invalid")
    need(set(state["workflows"]) <= {target}, "state_contains_other_target")
    allowed = {target, TEST_KEY} if target == "bizibl" else {target}
    need(all(row["key"] in allowed for row in state["outbox"].values()), "outbox_contains_other_target")
    return state


def signature(result):
    state = "healthy" if result["state"] in {"healthy", "recovered"} else result["state"]
    return digest([result["condition"], state, result.get("signature_data", {})])


def plan(state, report, now):
    validate(state)
    out = deepcopy(state)
    now = m.parse_time(now)
    # An interrupted sender is never interpreted as not sent.
    for row in out["outbox"].values():
        if row["status"] == "sending":
            row.update(status="needs_review", reason="interrupted_delivery_result_unknown")
    for result in report["results"]:
        key = result["key"]
        previous = out["workflows"].get(key)
        sig = signature(result)
        good = result["state"] in {"healthy", "recovered"}
        # Normal execution or short transient errors are recorded, not posted.
        # They do not clear a previously observed incident.
        if result["state"] == "watching":
            continue
        same = previous and previous["signature"] == sig
        incident = deepcopy(previous) if same else {
            "signature": sig, "generation": (previous or {}).get("generation", 0)+1,
            "first_seen": m.iso(now), "acknowledged": False,
            "last_sent_at": None, "state": result["state"], "condition": result["condition"]}
        incident["last_seen"] = m.iso(now)
        event = None
        if good:
            if previous and previous["state"] not in {"healthy", "recovered"}:
                prior_sent = any(r["key"] == key and r["signature"] == previous["signature"] and r["status"] == "sent"
                                 for r in out["outbox"].values())
                if prior_sent:
                    event = "recovery"
        elif not same:
            event = "new" if not previous or previous["state"] in {"healthy", "recovered"} else "changed"
        elif not incident["acknowledged"] and incident["last_sent_at"] and now >= m.parse_time(incident["last_sent_at"]) + timedelta(hours=24):
            event = "reminder"
        for row in out["outbox"].values():
            if row["key"] == key and row["status"] == "pending" and row["signature"] != sig:
                row["status"] = "superseded"
        if event:
            # One pending event per unchanged observation. Reminders are based
            # on confirmed sending, never on preview generation or polling time.
            existing = any(r["key"] == key and r["signature"] == sig and r["status"] in {"pending", "sending", "needs_review"}
                           for r in out["outbox"].values())
            if not existing:
                text = message(result, event)
                sequence = sum(r["key"] == key for r in out["outbox"].values()) + 1
                eid = digest([key, sig, incident["generation"], sequence])
                out["outbox"][eid] = {"id": eid, "key": key, "signature": sig, "event": event,
                    "status": "pending", "channel": m.CHANNEL_ID, "text": text, "text_sha256": digest(text),
                    "created_at": m.iso(now), "receipt": None}
        out["workflows"][key] = incident
    out["heartbeat"] = m.iso(now)
    return validate(out)


def acknowledge(state, key, exact_signature):
    out = deepcopy(validate(state))
    need(out["workflows"].get(key, {}).get("signature") == exact_signature, "acknowledgement_version_changed")
    out["workflows"][key]["acknowledged"] = True
    for row in out["outbox"].values():
        if row["key"] == key and row["signature"] == exact_signature and row["status"] == "pending":
            row["status"] = "superseded"
    return out


def enqueue_test(state, now):
    """One immutable test, independent of all business incidents; never requeues."""
    out = deepcopy(validate(state))
    eid = digest([TEMPLATE_VERSION, "single_delivery_test", m.CHANNEL_ID, TEST_MESSAGE])
    out["outbox"].setdefault(eid, {"id": eid, "key": TEST_KEY, "signature": eid,
        "event": "test", "status": "pending", "channel": m.CHANNEL_ID, "text": TEST_MESSAGE,
        "text_sha256": digest(TEST_MESSAGE), "created_at": m.iso(now), "receipt": None})
    return validate(out)


def code_fingerprint():
    here = Path(__file__).parent
    return digest({p: hashlib.sha256((here/p).read_bytes()).hexdigest()
                   for p in ("monitor.py", "evidence.py", "delivery.py", "state_store.py", "runtime.py")})


def approved(row, authorization, now):
    if not isinstance(authorization, dict) or authorization.get("approved") is not True:
        return False
    if authorization.get("channel") != m.CHANNEL_ID or not authorization.get("approval_reference"):
        return False
    if authorization.get("mode") == "exact_messages":
        return any(e.get("id") == row["id"] and e.get("text_sha256") == row["text_sha256"]
                   for e in authorization.get("messages", []))
    if authorization.get("mode") == "recurring_template_exception":
        # This mode is inert until the user explicitly relaxes the per-message
        # rule for THIS channel/template. Agreement to implementation is not it.
        return (row["event"] != "test" and authorization.get("exception_scope") == "only_these_marketing_alerts"
            and authorization.get("code_sha256") == code_fingerprint()
            and authorization.get("template_version") == TEMPLATE_VERSION
            and m.parse_time(authorization["not_before"]) <= m.parse_time(now)
            and (authorization.get("expires_at") is None or m.parse_time(now) < m.parse_time(authorization["expires_at"])))
    return False


def dispatch(journal, sender, authorization, now):
    sent = 0
    blocked = 0
    for eid in list(journal.state["outbox"]):
        row = journal.state["outbox"][eid]
        if row["status"] != "pending":
            continue
        if not approved(row, authorization, now):
            selected_exact = isinstance(authorization, dict) and authorization.get("mode") == "exact_messages"
            requested = selected_exact and any(e.get("id") == eid for e in authorization.get("messages", []))
            if not selected_exact or requested:
                blocked += 1
            continue
        # Freshness applies to current observation, not the age of an unresolved
        # case. Old queued wording must be regenerated by observation first.
        gap = m.parse_time(now)-m.parse_time(journal.state["heartbeat"])
        if gap > timedelta(minutes=30) or gap < timedelta(seconds=-60):
            raise ValueError("observation_stale_no_send")
        current = journal.state["workflows"].get(row["key"])
        if row["event"] != "test" and (not current or current["signature"] != row["signature"]):
            continue
        draft = deepcopy(journal.state)
        draft["outbox"][eid].update(status="sending", attempted_at=m.iso(now))
        journal.save(draft)  # Verified intent precedes the single network call.
        try:
            receipt = sender.post(row["channel"], row["text"], str(uuid.UUID(eid[:32])))
            need(receipt.get("channel") == row["channel"] and re.fullmatch(r"\d+\.\d+", receipt.get("ts", "")), "slack_ack_invalid")
        except Exception:
            draft = deepcopy(journal.state)
            draft["outbox"][eid].update(status="needs_review", reason="delivery_result_unknown_do_not_retry")
            journal.save(draft)
            # Stop this batch rather than sending more after a transport fault.
            return {"sent": sent, "needs_review": 1, "approval_blocked": blocked}
        draft = deepcopy(journal.state)
        draft["outbox"][eid].update(status="sent", receipt=receipt, sent_at=m.iso(now))
        if row["event"] != "test":
            draft["workflows"][row["key"]]["last_sent_at"] = m.iso(now)
        journal.save(draft)
        sent += 1
    return {"sent": sent, "needs_review": sum(r["status"] == "needs_review" for r in journal.state["outbox"].values()), "approval_blocked": blocked}


class SlackSender:
    def __init__(self, token):
        need(os.getenv("ALERT_DELIVERY_ENABLED") == "true" and token, "slack_delivery_disabled")
        self.token = token

    def post(self, channel, text, client_id):
        import requests
        need(channel == m.CHANNEL_ID, "slack_destination_changed")
        # requests has no automatic status/network retry here. Even an explicit
        # error is held for review; a timeout must never trigger a blind resend.
        response = requests.post("https://slack.com/api/chat.postMessage", timeout=20,
            headers={"Authorization": "Bearer " + self.token}, allow_redirects=False,
            json={"channel": channel, "text": text, "client_msg_id": client_id,
                  "unfurl_links": False, "unfurl_media": False, "parse": "none"})
        need(response.status_code == 200, "slack_response_unconfirmed")
        body = response.json()
        need(body.get("ok") is True, "slack_response_unconfirmed")
        return {"channel": body.get("channel"), "ts": body.get("ts")}
