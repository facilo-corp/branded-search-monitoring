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
READBACK_DELAYS = (0, 2, 5, 10)
GET_DELAYS = (0, 1, 3)
TRANSIENT = {"state_timeout", "state_connection_error", "state_service_unavailable"}
API_CODES = TRANSIENT | {"state_rate_limited", "state_access_denied", "state_not_found",
    "state_conflict", "state_request_rejected", "state_response_invalid", "state_retry_budget_exceeded"}


class StateStoreError(ValueError):
    """Only fixed codes and numeric status may leave the state transport."""
    def __init__(self, code, *, method=None, status=None):
        if code not in API_CODES | {"state_write_unconfirmed", "state_readback_unconfirmed"}:
            code = "state_request_rejected"
        super().__init__(code)
        self.diagnostic = {"code": code}
        if method in {"GET", "PUT"}:
            self.diagnostic["method"] = method
        if type(status) is int and 100 <= status <= 599:
            self.diagnostic["http_status"] = status


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
        self.diagnostics = []
        self.phase = "read"
        self.deadline = None

    def note(self, event, **values):
        # Never accept response bodies, URLs, headers, tokens or exception text.
        entry = {"phase": self.phase, "event": event}
        entry.update({k: v for k, v in values.items()
                      if k in {"attempt", "http_status", "method", "code", "matched"}})
        self.diagnostics.append(entry)
        self.diagnostics[:] = self.diagnostics[-100:]

    @contextmanager
    def budget(self):
        prior = self.deadline
        if prior is None:
            self.deadline = time.monotonic() + 120
        try:
            yield
        finally:
            self.deadline = prior

    def remaining(self):
        remaining = 30 if self.deadline is None else self.deadline - time.monotonic()
        if remaining <= 0:
            raise StateStoreError("state_retry_budget_exceeded")
        return remaining

    def pause(self, delay):
        if delay >= self.remaining():
            raise StateStoreError("state_retry_budget_exceeded")
        if delay:
            time.sleep(delay)

    def request(self, method, suffix, body=None):
        import requests
        delays = GET_DELAYS if method == "GET" else (0,)
        with self.budget():
            for attempt, delay in enumerate(delays, 1):
                self.pause(delay)
                try:
                    seconds = min(10, self.remaining() / 2)
                    response = requests.request(method, "https://api.github.com/repos/" + self.repository + "/" + suffix,
                        headers={"Authorization": "Bearer " + self.token, "Accept": "application/vnd.github+json",
                                 "X-GitHub-Api-Version": "2022-11-28", "Cache-Control": "no-cache"},
                        json=body, timeout=(min(5, seconds), seconds), allow_redirects=False)
                    status = response.status_code
                    if status not in {200, 201}:
                        headers = response.headers
                        secondary_limit = False
                        if status == 403:
                            try:
                                error_body = response.json()
                                message = error_body.get("message", "") if isinstance(error_body, dict) else ""
                                secondary_limit = isinstance(message, str) and any(
                                    label in message.lower() for label in ("secondary rate limit", "rate limit exceeded"))
                            except ValueError:
                                pass
                        # Stop this run on rate limits; never retry before GitHub's cooldown.
                        if status == 429 or (status == 403 and
                            (headers.get("x-ratelimit-remaining") == "0" or headers.get("retry-after") or secondary_limit)):
                            code = "state_rate_limited"
                        elif status in {401, 403}: code = "state_access_denied"
                        elif status == 404: code = "state_not_found"
                        elif status == 409: code = "state_conflict"
                        elif status in {408, 500, 502, 503, 504}: code = "state_service_unavailable"
                        else: code = "state_request_rejected"
                        raise StateStoreError(code, method=method, status=status)
                    try:
                        value = response.json()
                    except ValueError:
                        raise StateStoreError("state_response_invalid", method=method) from None
                    self.note("response_ok", method=method, attempt=attempt, http_status=status)
                    return value
                except requests.Timeout:
                    error = StateStoreError("state_timeout", method=method)
                except requests.ConnectionError:
                    error = StateStoreError("state_connection_error", method=method)
                except requests.RequestException:
                    error = StateStoreError("state_request_rejected", method=method)
                except StateStoreError as exc:
                    error = exc
                self.note("request_failed", attempt=attempt, **error.diagnostic)
                if str(error) not in TRANSIENT or attempt == len(delays):
                    raise error from None

    def decode(self, value):
        need(isinstance(value, dict), "state_content_invalid")
        need(value.get("encoding") == "base64" and value.get("size", 1_000_000) < 900_000, "state_content_invalid")
        try:
            raw = base64.b64decode(value["content"])
            doc = json.loads(raw)
        except (KeyError, ValueError, TypeError):
            raise StateStoreError("state_response_invalid") from None
        actual_sha = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
        need(value.get("sha") == actual_sha, "state_content_hash_mismatch")
        return value["sha"], validate_target(doc, self.target) if self.target else validate(doc)

    def read(self, ref=BRANCH):
        need(ref == BRANCH or re.fullmatch(r"[0-9a-f]{40}", ref), "state_ref_invalid")
        return self.decode(self.request("GET", f"contents/{FILE}?ref={ref}"))

    def current(self):
        """Read the saved commit, pin its contents, then confirm the branch stayed there."""
        def head():
            value = self.request("GET", f"git/ref/heads/{BRANCH}")
            obj = value.get("object", {}) if isinstance(value, dict) else {}
            need(obj.get("type") == "commit" and isinstance(obj.get("sha"), str)
                 and re.fullmatch(r"[0-9a-f]{40}", obj["sha"]), "state_history_invalid")
            return obj["sha"]
        commit = head()
        sha, doc = self.decode(self.request("GET", f"contents/{FILE}?ref={commit}"))
        need(head() == commit, "state_changed_concurrently")
        return commit, sha, doc

    def reconcile(self, expected_sha, expected_doc, previous_sha, original_commit=None):
        last = None
        for attempt, delay in enumerate(READBACK_DELAYS, 1):
            self.pause(delay)
            try:
                commit, sha, doc = self.current()
            except StateStoreError as exc:
                if str(exc) in TRANSIENT:
                    continue
                raise
            if sha == expected_sha and doc == expected_doc:
                self.note("saved_content_confirmed", attempt=attempt, matched=True)
                return True, commit
            need(sha == previous_sha, "state_changed_concurrently")
            need(original_commit is None or commit == original_commit, "state_changed_concurrently")
            last = commit
            self.note("previous_content_confirmed", attempt=attempt, matched=False)
        return False, last

    def write(self, previous_sha, doc):
        validate(doc)
        if self.target:
            validate_target(doc, self.target)
        raw = json.dumps(doc, ensure_ascii=False, sort_keys=True).encode()
        expected_sha = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
        payload = {"message": "Persist marketing alert monitor state", "branch": BRANCH, "sha": previous_sha,
                   "content": base64.b64encode(raw).decode()}
        with self.budget():
            base_commit, sha, _ = self.current()
            need(sha == previous_sha, "state_changed_concurrently")
            for attempt in (1, 2):
                error = None
                try:
                    self.request("PUT", f"contents/{FILE}", payload)
                except StateStoreError as exc:
                    error = exc
                    if str(exc) not in TRANSIENT | {"state_conflict", "state_response_invalid"}:
                        raise
                confirmed, observed_commit = self.reconcile(expected_sha, doc, previous_sha, base_commit)
                if confirmed:
                    return expected_sha
                # Conditional CAS retry only: a transient PUT error AND the original
                # commit/content are still present. Reuse the exact operation and old SHA.
                # An original request arriving late or another writer makes the CAS fail;
                # read back the intended content, never replay Slack/business operations.
                if attempt == 1 and error is not None and str(error) in TRANSIENT and observed_commit == base_commit:
                    self.pause(2)
                    retry_commit, retry_sha, _ = self.current()
                    need(retry_commit == base_commit and retry_sha == previous_sha, "state_changed_concurrently")
                    self.note("conditional_save_retry", attempt=2)
                    continue
                raise StateStoreError("state_write_unconfirmed")

    def confirm(self, expected_sha, expected_doc, previous_sha):
        with self.budget():
            if self.reconcile(expected_sha, expected_doc, previous_sha)[0]:
                return expected_sha, expected_doc
        raise StateStoreError("state_readback_unconfirmed")

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
        try:
            self._save(doc)
        except Exception:
            # Even a pre-write read/CAS failure must retain the owner: a Slack
            # acknowledgement may already exist when this save is attempted.
            self.uncertain = True
            raise

    def _save(self, doc):
        sha, fresh = self.store.read()
        if (sha != self.sha or fresh != self.state) and isinstance(self.store, GitStore):
            _, sha, fresh = self.store.current()
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
    def exclusive(self, recover=None):
        if isinstance(self.store, GitStore):
            self.store.phase = "acquire_lock"
        self.sha, self.state = self.store.read()
        if self.state["owner"] is not None:
            if recover is not None and any(r["status"] in {"sending", "needs_review"} for r in self.state["outbox"].values()):
                need(isinstance(self.store, GitStore) and self.store.previous_owner_finished(self.state["owner"]),
                     "monitor_lock_requires_review")
                self.store.phase = "recover_receipt"
                self.save(recover(self.sha, deepcopy(self.state)))
                self.store.phase = "acquire_lock"
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
            if isinstance(self.store, GitStore):
                self.store.phase = "record_observation_or_delivery"
            yield self
        finally:
            if not self.uncertain:
                if isinstance(self.store, GitStore):
                    self.store.phase = "release_lock"
                unlocked = deepcopy(self.state)
                need(unlocked["owner"] == self.owner, "monitor_lock_lost")
                unlocked["owner"] = None
                self.save(unlocked)
