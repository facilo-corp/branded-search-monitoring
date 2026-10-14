"""Versioned GitHub journal. Missing state and interrupted ownership fail closed."""
from contextlib import contextmanager
from copy import deepcopy
import base64
import hashlib
import json
import os
import re
import time
import uuid

from delivery import initial_state, validate, validate_target, need
import monitor as m

REPOSITORY = "facilo-corp/facilo-marketing-webinar"
BRANCH = "automation-monitor-state"
FILE = "alerts.json"
READBACK_DELAYS = (0, 1, 2, 4)


def execution_owner():
    run, attempt = os.getenv("GITHUB_RUN_ID", ""), os.getenv("GITHUB_RUN_ATTEMPT", "")
    if re.fullmatch(r"[1-9]\d*", run) and re.fullmatch(r"[1-9]\d*", attempt):
        return f"run:{run}:{attempt}:{uuid.uuid4().hex}"
    return uuid.uuid4().hex


class GitStore:
    def __init__(self, token, repository=REPOSITORY, target=None):
        need(bool(token), "state_credential_missing")
        need(repository in {c["repo"] for c in m.CONFIG.values()}, "state_repository_invalid")
        if target is not None:
            need(target in m.CONFIG and m.CONFIG[target]["repo"] == repository, "state_repository_mismatch")
        if m.ACTIVE_REPOSITORY is not None:
            need(repository == m.ACTIVE_REPOSITORY, "cross_repository_write_forbidden")
        self.token = token
        self.repository = repository
        self.target = target

    def request(self, method, suffix, body=None):
        import requests
        try:
            response = requests.request(method, "https://api.github.com/repos/" + self.repository + "/" + suffix,
                headers={"Authorization": "Bearer " + self.token, "Accept": "application/vnd.github+json",
                         "X-GitHub-Api-Version": "2022-11-28"}, json=body, timeout=25, allow_redirects=False)
            need(response.status_code in {200, 201}, "state_api_unavailable")
            return response.json()
        except Exception:
            raise ValueError("state_api_unavailable") from None

    def read(self):
        value = self.request("GET", f"contents/{FILE}?ref={BRANCH}")
        need(value.get("encoding") == "base64" and value.get("size", 1_000_000) < 900_000, "state_content_invalid")
        doc = json.loads(base64.b64decode(value["content"]))
        return value["sha"], validate_target(doc, self.target) if self.target else validate(doc)

    def write(self, previous_sha, doc):
        validate(doc)
        if self.target:
            validate_target(doc, self.target)
        raw = json.dumps(doc, ensure_ascii=False, sort_keys=True).encode()
        expected_sha = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
        payload = {"message": "Persist marketing alert monitor state", "branch": BRANCH, "sha": previous_sha,
                   "content": base64.b64encode(raw).decode()}
        # One attempt only. Lost responses are reconciled by reading, never by PUT retry.
        try:
            self.request("PUT", f"contents/{FILE}", payload)
        except Exception:
            pass
        # Retry only GET, and only an old version or unavailable read. Never
        # accept another writer's revision, nor submit the PUT a second time.
        for delay in READBACK_DELAYS:
            if delay:
                time.sleep(delay)
            try:
                sha, verified = self.read()
            except ValueError as exc:
                if str(exc) == "state_api_unavailable":
                    continue
                raise
            if sha == expected_sha and verified == doc:
                return sha
            need(sha == previous_sha, "state_changed_concurrently")
        raise ValueError("state_write_unconfirmed")

    def confirm(self, expected_sha, expected_doc, previous_sha):
        for delay in READBACK_DELAYS:
            if delay:
                time.sleep(delay)
            sha, doc = self.read()
            if sha == expected_sha and doc == expected_doc:
                return sha, doc
            need(sha == previous_sha, "state_changed_concurrently")
        raise ValueError("state_readback_unconfirmed")

    def previous_owner_finished(self, owner):
        match = re.fullmatch(r"run:([1-9]\d*):([1-9]\d*):[0-9a-f]{32}", owner)
        if not match:
            return False  # Legacy UUIDs require an explicit reviewed recovery.
        run_id, attempt = match.group(1), int(match.group(2))
        if run_id == os.getenv("GITHUB_RUN_ID"):
            return False
        run = self.request("GET", f"actions/runs/{run_id}")
        exact = self.request("GET", f"actions/runs/{run_id}/attempts/{attempt}")
        for item in (run, exact):
            need(str(item.get("id")) == run_id and item.get("path") == ".github/workflows/automation-monitor.yml"
                 and item.get("head_branch") == "main" and item.get("event") in {"schedule", "workflow_dispatch"},
                 "monitor_previous_run_unverified")
        need(exact.get("run_attempt") == attempt, "monitor_previous_run_unverified")
        return run.get("status") == exact.get("status") == "completed"


class MemoryStore:
    """Preview/tests only; cannot be mistaken for a production ledger."""
    def __init__(self, doc=None):
        self.doc = deepcopy(doc or initial_state())
        self.revision = 0

    def read(self):
        return str(self.revision), deepcopy(self.doc)

    def write(self, previous_sha, doc):
        need(previous_sha == str(self.revision), "concurrent_state_change")
        self.doc = deepcopy(validate(doc))
        self.revision += 1
        return str(self.revision)


class Journal:
    def __init__(self, store, owner=None):
        self.store = store
        self.owner = owner or execution_owner()
        self.sha, self.state = None, None
        self.uncertain = False

    def save(self, doc):
        need(not self.uncertain, "state_uncertain")
        sha, fresh = self.store.read()
        need(sha == self.sha and fresh == self.state, "state_changed_concurrently")
        need(set(self.state["outbox"]) <= set(doc["outbox"]), "delivery_history_cannot_be_deleted")
        for eid, old in self.state["outbox"].items():
            new = doc["outbox"][eid]
            need(all(old[k] == new[k] for k in ("id", "key", "signature", "event", "channel", "text", "text_sha256", "created_at")), "delivery_identity_changed")
            transitions = {"pending": {"pending", "sending", "superseded"},
                           "sending": {"sending", "sent", "needs_review"},
                           "sent": {"sent"}, "needs_review": {"needs_review"}, "superseded": {"superseded"}}
            need(new["status"] in transitions[old["status"]], "delivery_status_cannot_be_rewound")
            if old["status"] == "sent":
                need(old == new, "sent_receipt_changed")
        doc = deepcopy(doc)
        doc["revision"] = self.state["revision"]+1
        doc["operation_id"] = uuid.uuid4().hex
        try:
            next_sha = self.store.write(self.sha, doc)
            if isinstance(self.store, GitStore):
                check_sha, check = self.store.confirm(next_sha, doc, self.sha)
            else:
                check_sha, check = self.store.read()
            need(check_sha == next_sha and check == doc, "state_readback_unconfirmed")
        except Exception:
            self.uncertain = True
            raise
        self.sha, self.state = next_sha, doc

    @contextmanager
    def exclusive(self):
        self.sha, self.state = self.store.read()
        if self.state["owner"] is not None:
            # There is nothing to resend or infer when every delivery already
            # has a receipt (or was never sent). Unknown sends remain held.
            need(not any(r["status"] in {"sending", "needs_review"} for r in self.state["outbox"].values()),
                 "monitor_delivery_requires_review")
            need(isinstance(self.store, GitStore) and self.store.previous_owner_finished(self.state["owner"]),
                 "monitor_lock_requires_review")
        locked = deepcopy(self.state)
        locked["owner"] = self.owner
        self.save(locked)
        try:
            yield self
        finally:
            if not self.uncertain:
                unlocked = deepcopy(self.state)
                need(unlocked["owner"] == self.owner, "monitor_lock_lost")
                unlocked["owner"] = None
                self.save(unlocked)
