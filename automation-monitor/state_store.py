"""Versioned GitHub journal. Missing state and interrupted ownership fail closed."""
from contextlib import contextmanager
from copy import deepcopy
import base64
import json
import uuid

from delivery import initial_state, validate, validate_target, need
import monitor as m

REPOSITORY = "facilo-corp/facilo-marketing-webinar"
BRANCH = "automation-monitor-state"
FILE = "alerts.json"


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
        payload = {"message": "Persist marketing alert monitor state", "branch": BRANCH, "sha": previous_sha,
                   "content": base64.b64encode(json.dumps(doc, ensure_ascii=False, sort_keys=True).encode()).decode()}
        # One attempt only. Lost responses are reconciled by reading, never by PUT retry.
        try:
            self.request("PUT", f"contents/{FILE}", payload)
        except Exception:
            pass
        sha, verified = self.read()
        need(sha != previous_sha and verified == doc, "state_write_unconfirmed")
        return sha


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
        self.owner = owner or uuid.uuid4().hex
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
            check_sha, check = self.store.read()
            need(check_sha == next_sha and check == doc, "state_readback_unconfirmed")
        except Exception:
            self.uncertain = True
            raise
        self.sha, self.state = next_sha, doc

    @contextmanager
    def exclusive(self):
        self.sha, self.state = self.store.read()
        need(self.state["owner"] is None, "monitor_lock_requires_review")
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
