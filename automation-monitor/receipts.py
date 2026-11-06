"""Run-bound Slack acknowledgements. Recovery never calls Slack or retries a send."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
from urllib.parse import urlparse
import zipfile

import delivery as d
import monitor as m

FILE = "delivery-receipts.json"
WORKFLOW = ".github/workflows/automation-monitor.yml"
MAX_ARCHIVE = 512_000
MAX_DOCUMENT = 128_000
OWNER = r"run:([1-9]\d*):([1-9]\d*):[0-9a-f]{32}"


def artifact_name(run_id, attempt):
    return f"automation-monitor-receipts-{run_id}-{attempt}"


def receipt_valid(receipt, channel):
    return (isinstance(receipt, dict) and set(receipt) == {"channel", "ts"}
            and receipt["channel"] == channel and isinstance(receipt["ts"], str)
            and re.fullmatch(r"[1-9]\d*\.\d{6}", receipt["ts"]) is not None)


class ReceiptLog:
    def __init__(self, output, repository, target, owner):
        match = re.fullmatch(OWNER, owner)
        d.need(match is not None and target == "branded" and repository == m.CONFIG[target]["repo"],
               "receipt_context_invalid")
        run_id, attempt = match.groups()
        d.need(run_id == os.getenv("GITHUB_RUN_ID") and attempt == os.getenv("GITHUB_RUN_ATTEMPT")
               and repository == os.getenv("GITHUB_REPOSITORY"), "receipt_context_invalid")
        head = os.getenv("GITHUB_SHA", "")
        d.need(re.fullmatch(r"[0-9a-f]{40}", head), "receipt_context_invalid")
        self.path = Path(output) / FILE
        d.need(not self.path.exists(), "receipt_file_already_exists")
        self.doc = {"schema": 1, "repository": repository, "target": target, "owner": owner,
                    "run_id": int(run_id), "run_attempt": int(attempt), "head_sha": head,
                    "code_sha256": d.code_fingerprint(), "receipts": []}
        self._save()  # Verify the independent local destination before sending anything.

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        raw = json.dumps(self.doc, ensure_ascii=False, sort_keys=True).encode("utf-8")
        d.need(len(raw) <= MAX_DOCUMENT, "receipt_file_too_large")
        with temporary.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)

    def record(self, journal, eid, receipt, sent_at):
        row = journal.state["outbox"][eid]
        d.need(journal.state["owner"] == self.doc["owner"] and row["status"] == "sending"
               and receipt_valid(receipt, row["channel"]), "receipt_context_invalid")
        d.need(not any(r["id"] == eid for r in self.doc["receipts"]), "receipt_already_recorded")
        self.doc["receipts"].append({"id": eid, "channel": row["channel"],
            "text_sha256": row["text_sha256"], "row_sha256": d.digest(row),
            "intent_sha": journal.sha, "state_sha256": d.digest(journal.state),
            "receipt": deepcopy(receipt), "sent_at": m.iso(sent_at),
            "recorded_at": m.iso(datetime.now(timezone.utc))})
        self._save()  # Must happen before the fallible GitHub journal write.


def download(store, artifact):
    """Only the GitHub API gets the token; signed blob requests are unauthenticated."""
    import requests
    artifact_id = artifact.get("id")
    d.need(type(artifact_id) is int and artifact_id > 0, "receipt_artifact_invalid")
    try:
        response = requests.get(
            f"https://api.github.com/repos/{store.repository}/actions/artifacts/{artifact_id}/zip",
            headers={"Authorization": "Bearer " + store.token, "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"}, timeout=25, allow_redirects=False)
        d.need(response.status_code == 302, "receipt_artifact_unavailable")
        location = response.headers.get("Location", "")
        url = urlparse(location)
        d.need(url.scheme == "https" and url.port in {None, 443} and not url.username and not url.password
               and url.hostname and url.hostname.endswith((".blob.core.windows.net", ".actions.githubusercontent.com")),
               "receipt_download_host_invalid")
        chunks, size = [], 0
        with requests.get(location, timeout=25, allow_redirects=False, stream=True) as blob:
            d.need(blob.status_code == 200, "receipt_artifact_unavailable")
            for chunk in blob.iter_content(32_768):
                size += len(chunk)
                d.need(size <= MAX_ARCHIVE, "receipt_artifact_too_large")
                chunks.append(chunk)
        raw = b"".join(chunks)
        d.need(artifact.get("digest") == "sha256:" + hashlib.sha256(raw).hexdigest(),
               "receipt_artifact_digest_mismatch")
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            members = archive.infolist()
            d.need(len(members) == 1 and members[0].filename == FILE
                   and members[0].file_size <= MAX_DOCUMENT and not members[0].flag_bits & 1,
                   "receipt_artifact_invalid")
            return json.loads(archive.read(members[0]))
    except Exception:
        # No response bodies, signed URLs, tokens or arbitrary exception strings.
        raise ValueError("receipt_artifact_unavailable") from None


class Recovery:
    def __init__(self, store, approved_source):
        self.store, self.approved_source = store, approved_source
        self.recovered = []

    def __call__(self, sha, state):
        """Return a complete candidate only. Journal.save performs the final CAS."""
        try:
            return self._candidate(sha, state)
        except Exception as exc:
            known = {"receipt_context_invalid", "receipt_run_unverified", "receipt_code_unapproved",
                     "receipt_run_expired", "receipt_artifact_incomplete", "receipt_artifact_missing",
                     "receipt_artifact_unverified", "receipt_artifact_unavailable", "receipt_proof_invalid",
                     "receipt_proof_missing", "receipt_proof_mismatch", "receipt_time_invalid", "receipt_incident_changed"}
            self.store.note("receipt_recovery", code=str(exc) if str(exc) in known else "unverified")
            raise ValueError("monitor_receipt_requires_review") from None

    def _candidate(self, sha, state):
        store = self.store
        d.need(store.target == "branded", "receipt_context_invalid")
        d.validate_target(state, store.target)
        match = re.fullmatch(OWNER, state["owner"] or "")
        d.need(match and match.group(1) != os.getenv("GITHUB_RUN_ID"), "receipt_context_invalid")
        run_id, attempt = map(int, match.groups())
        latest = store.request("GET", f"actions/runs/{run_id}")
        exact = store.request("GET", f"actions/runs/{run_id}/attempts/{attempt}")
        for run in (latest, exact):
            d.need(run.get("id") == run_id and run.get("run_attempt") == attempt
                   and run.get("path") == WORKFLOW and run.get("head_branch") == "main"
                   and run.get("event") in {"schedule", "workflow_dispatch"}
                   and run.get("status") == "completed"
                   and run.get("repository", {}).get("full_name") == store.repository
                   and run.get("head_repository", {}).get("full_name") == store.repository,
                   "receipt_run_unverified")
        head = exact.get("head_sha")
        d.need(head == latest.get("head_sha") and self.approved_source(head), "receipt_code_unapproved")
        started, ended = m.parse_time(exact["run_started_at"]), m.parse_time(exact["updated_at"])
        d.need(started <= ended <= datetime.now(timezone.utc) + timedelta(seconds=60)
               and datetime.now(timezone.utc) - ended <= timedelta(days=14), "receipt_run_expired")
        listing = store.request("GET", f"actions/runs/{run_id}/artifacts?per_page=100")
        artifacts = listing.get("artifacts", [])
        d.need(listing.get("total_count") == len(artifacts) <= 100, "receipt_artifact_incomplete")
        found = [a for a in artifacts if a.get("name") == artifact_name(run_id, attempt)]
        d.need(len(found) == 1, "receipt_artifact_missing")
        artifact = found[0]
        origin = artifact.get("workflow_run", {})
        d.need(artifact.get("expired") is False and type(artifact.get("size_in_bytes")) is int
               and 0 < artifact["size_in_bytes"] <= MAX_ARCHIVE
               and origin.get("id") == run_id and origin.get("head_sha") == head
               and origin.get("head_branch") == "main"
               and type(exact.get("repository", {}).get("id")) is int
               and origin.get("repository_id") == exact["repository"]["id"]
               and started - timedelta(seconds=60) <= m.parse_time(artifact["created_at"]) <= ended + timedelta(seconds=60),
               "receipt_artifact_unverified")
        proof = download(store, artifact)
        expected = {"schema": 1, "repository": store.repository, "target": store.target,
                    "owner": state["owner"], "run_id": run_id, "run_attempt": attempt,
                    "head_sha": head, "code_sha256": d.code_fingerprint()}
        d.need(isinstance(proof, dict) and set(proof) == set(expected) | {"receipts"}
               and all(proof[k] == v for k, v in expected.items()), "receipt_proof_invalid")
        records = proof["receipts"]
        d.need(isinstance(records, list) and 0 < len(records) <= 20
               and len({r["id"] for r in records}) == len(records), "receipt_proof_invalid")
        held = [r for r in state["outbox"].values() if r["status"] in {"sending", "needs_review"}]
        # Dispatch is serial: only one uncommitted successful send can exist.
        d.need(len(held) == 1 and held[0]["status"] == "sending", "receipt_proof_invalid")
        row = held[0]
        matching = [r for r in records if r["id"] == row["id"]]
        d.need(len(matching) == 1, "receipt_proof_missing")
        item = matching[0]
        d.need(set(item) == {"id", "channel", "text_sha256", "row_sha256", "intent_sha",
                            "state_sha256", "receipt", "sent_at", "recorded_at"}
               and item["channel"] == row["channel"] and item["text_sha256"] == row["text_sha256"]
               and item["row_sha256"] == d.digest(row) and item["intent_sha"] == sha
               and item["state_sha256"] == d.digest(state)
               and receipt_valid(item["receipt"], row["channel"]), "receipt_proof_mismatch")
        sent, recorded = m.parse_time(item["sent_at"]), m.parse_time(item["recorded_at"])
        slack_time = datetime.fromtimestamp(float(item["receipt"]["ts"]), timezone.utc)
        d.need(item["sent_at"] == row.get("attempted_at")
               and started - timedelta(seconds=60) <= sent <= recorded <= ended + timedelta(seconds=60)
               and abs((slack_time - recorded).total_seconds()) <= 60,
               "receipt_time_invalid")
        incident = state["workflows"].get(row["key"], {})
        d.need(incident.get("signature") == row["signature"], "receipt_incident_changed")
        candidate = deepcopy(state)
        candidate["outbox"][row["id"]].update(status="sent", receipt=deepcopy(item["receipt"]), sent_at=item["sent_at"])
        candidate["workflows"][row["key"]]["last_sent_at"] = item["sent_at"]
        self.recovered = [{"id": row["id"], "run_id": run_id, "run_attempt": attempt}]
        return candidate
