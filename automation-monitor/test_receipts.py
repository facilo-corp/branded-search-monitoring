"""Offline fault injection; all network and Slack transports are fixtures."""
import base64
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

import delivery as d
import monitor as m
import receipts as r
import runtime
from state_store import GitStore, Journal
from test_delivery import authorization
from test_repository_scope import args
from test_state_recovery import payload

REPO = m.CONFIG["branded"]["repo"]
HEAD = "a" * 40
OWNER = "run:101:1:" + "b" * 32
NEXT_OWNER = "run:202:1:" + "c" * 32


def planned(now):
    observation = m.base_result("branded", now)
    observation.update(state="unknown", condition="read_unavailable")
    return d.plan(d.initial_state(), {"results": [observation]}, now), observation


class Sender:
    def __init__(self, now, fail=False):
        self.calls = []
        self.now, self.fail = now, fail

    def post(self, channel, text, client_id):
        self.calls.append((channel, text, client_id))
        if self.fail:
            raise TimeoutError("SECRET private@example.com")
        return {"channel": channel, "ts": f"{self.now.timestamp():.6f}"}


class Store(GitStore):
    def __init__(self, doc):
        super().__init__("fixture-token", REPO, "branded")
        self.doc, self.puts, self.fail_sent = deepcopy(doc), 0, False
        self.api = {}

    def read(self, ref=None):
        return payload(self.doc)["sha"], deepcopy(self.doc)

    def current(self):
        return 'f' * 40, *self.read()

    def confirm(self, expected, doc, previous):
        assert self.read() == (expected, doc)
        return self.read()

    def write(self, previous, doc):
        if self.fail_sent and any(row["status"] == "sent" and self.doc["outbox"].get(row["id"], {}).get("status") != "sent"
                                  for row in doc["outbox"].values()):
            raise ValueError("state_write_unconfirmed")
        if previous != self.read()[0]:
            raise ValueError("state_changed_concurrently")
        self.puts += 1
        self.doc = deepcopy(doc)
        return self.read()[0]

    def request(self, method, path, body=None):
        assert method == "GET", "Fixture must never perform a real mutation"
        return deepcopy(self.api[path])


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = datetime.now(timezone.utc)
        state, self.observation = planned(self.now)
        history = deepcopy(next(iter(state["outbox"].values())))
        history.update(id="e" * 64, signature="earlier-case", status="sent", sent_at=m.iso(self.now - timedelta(days=1)),
                       receipt={"channel": m.CHANNEL_ID, "ts": f"{(self.now - timedelta(days=1)).timestamp():.6f}"})
        state["outbox"][history["id"]] = history
        self.history = deepcopy(history)
        self.store = Store(state)
        self.sender = Sender(self.now)
        env = {"GITHUB_RUN_ID": "101", "GITHUB_RUN_ATTEMPT": "1", "GITHUB_REPOSITORY": REPO,
               "GITHUB_SHA": HEAD}
        with patch.dict(os.environ, env):
            self.log = r.ReceiptLog(self.tmp.name, REPO, "branded", OWNER)
            self.store.fail_sent = True
            with self.assertRaisesRegex(ValueError, "state_write_unconfirmed"):
                with Journal(self.store, OWNER).exclusive() as journal:
                    d.dispatch(journal, self.sender, authorization(journal.state), self.now, self.log)
        self.store.fail_sent = False
        self.proof = json.loads(self.log.path.read_text(encoding="utf-8"))
        self.run = {"id": 101, "run_attempt": 1, "path": r.WORKFLOW, "head_branch": "main",
                    "event": "schedule", "status": "completed", "head_sha": HEAD,
                    "repository": {"full_name": REPO, "id": 123}, "head_repository": {"full_name": REPO},
                    "run_started_at": m.iso(self.now - timedelta(seconds=5)),
                    "updated_at": m.iso(self.now + timedelta(seconds=5))}
        self.artifact = {"id": 987, "name": r.artifact_name(101, 1), "size_in_bytes": 2000,
                         "expired": False, "created_at": m.iso(self.now),
                         "workflow_run": {"id": 101, "head_sha": HEAD, "head_branch": "main", "repository_id": 123}}
        self.store.api = {"actions/runs/101": self.run, "actions/runs/101/attempts/1": self.run,
                          "actions/runs/101/artifacts?per_page=100": {"total_count": 1, "artifacts": [self.artifact]}}

    def recover(self, proof=None, source=True):
        with patch.dict(os.environ, {"GITHUB_RUN_ID": "202"}), patch.object(r, "download", return_value=proof or self.proof):
            with Journal(self.store, NEXT_OWNER).exclusive(recover=r.Recovery(self.store, lambda head: source)) as journal:
                journal.save(d.plan(journal.state, {"results": [self.observation]}, self.now + timedelta(minutes=15)))
                return d.dispatch(journal, self.sender, authorization(journal.state), self.now + timedelta(minutes=15))

    def assert_held(self, **kwargs):
        before, puts = deepcopy(self.store.doc), self.store.puts
        with self.assertRaises(ValueError):
            self.recover(**kwargs)
        self.assertEqual(self.store.doc, before)
        self.assertEqual(self.store.puts, puts)
        self.assertEqual(len(self.sender.calls), 1)

    def test_successful_slack_failed_state_write_recovers_without_second_send(self):
        self.assertEqual(len(self.proof["receipts"]), 1)
        self.assertEqual(self.store.doc["owner"], OWNER)
        self.assertEqual(next(iter(self.store.doc["outbox"].values()))["status"], "sending")
        result = self.recover()
        self.assertEqual(result, {"sent": 0, "needs_review": 0, "approval_blocked": 0})
        self.assertIsNone(self.store.doc["owner"])
        row = next(iter(self.store.doc["outbox"].values()))
        self.assertEqual(row["status"], "sent")
        self.assertEqual(row["receipt"], self.proof["receipts"][0]["receipt"])
        self.assertEqual(self.store.doc["outbox"][self.history["id"]], self.history)
        with Journal(self.store, NEXT_OWNER).exclusive() as journal:
            journal.save(d.plan(journal.state, {"results": [self.observation]}, self.now + timedelta(minutes=30)))
            d.dispatch(journal, self.sender, authorization(journal.state), self.now + timedelta(minutes=30))
        self.assertEqual(len(self.sender.calls), 1)

    def test_runtime_recovers_through_verified_archive_and_returns_to_normal(self):
        raw = archive_bytes(content=json.dumps(self.proof).encode())
        self.artifact["digest"] = "sha256:" + hashlib.sha256(raw).hexdigest()
        first = Mock(status_code=302, headers={"Location": "https://fixture.blob.core.windows.net/archive"})
        blob = Mock(status_code=200)
        blob.iter_content.return_value = [raw]
        context = Mock()
        context.__enter__ = Mock(return_value=blob)
        context.__exit__ = Mock(return_value=False)
        with tempfile.TemporaryDirectory() as output, patch.dict(os.environ,
                {"GITHUB_RUN_ID": "202", "GITHUB_RUN_ATTEMPT": "1", "GITHUB_REPOSITORY": REPO, "GITHUB_SHA": HEAD}), \
                patch("requests.get", side_effect=[first, context]), \
                patch.object(runtime, "approved_source", return_value=True), \
                patch.object(d, "SlackSender", return_value=self.sender):
            sent, pending, state, recovered = runtime.run_journal(
                args("branded", persist=True, send=True), self.store, {"results": [self.observation]},
                authorization(self.store.doc), self.now, Path(output))
        self.assertEqual(sent["sent"], 0)
        self.assertEqual(pending, [])
        self.assertEqual(recovered[0]["run_id"], 101)
        self.assertIsNone(state["owner"])
        self.assertEqual(len(self.sender.calls), 1)
        self.assertEqual(state["outbox"][self.history["id"]], self.history)

    def test_missing_expired_wrong_run_or_ambiguous_artifacts_stay_held(self):
        cases = [{"total_count": 0, "artifacts": []}, {"total_count": 2, "artifacts": [self.artifact, self.artifact]},
                 {"total_count": 101, "artifacts": [self.artifact]}]
        for update in ({"expired": True}, {"name": r.artifact_name(101, 2)},
                       {"workflow_run": {**self.artifact["workflow_run"], "id": 999}},
                       {"workflow_run": {**self.artifact["workflow_run"], "repository_id": 999}},
                       {"created_at": m.iso(self.now - timedelta(days=1))}):
            cases.append({"total_count": 1, "artifacts": [{**self.artifact, **update}]})
        for listing in cases:
            with self.subTest(listing=listing):
                self.store.api["actions/runs/101/artifacts?per_page=100"] = listing
                self.assert_held()

    def test_active_owner_wrong_workflow_fork_or_new_attempt_never_releases(self):
        for update in ({"status": "in_progress"}, {"path": ".github/workflows/other.yml"},
                       {"head_repository": {"full_name": "other/repo"}}, {"run_attempt": 2},
                       {"head_sha": "d" * 40}):
            with self.subTest(update=update):
                self.store.api["actions/runs/101"] = {**self.run, **update}
                self.assert_held()

    def test_unapproved_code_and_altered_proof_are_rejected(self):
        self.assert_held(source=False)
        for field, value in (("code_sha256", "changed"), ("run_id", 999), ("run_attempt", 2),
                             ("repository", "other/repo"), ("owner", NEXT_OWNER), ("head_sha", "f" * 40)):
            with self.subTest(field=field):
                self.assert_held(proof={**self.proof, field: value})
        for field, value in (("id", "f" * 64), ("text_sha256", "changed"), ("channel", "other"),
                             ("intent_sha", "changed"), ("row_sha256", "changed"), ("state_sha256", "changed"),
                             ("receipt", {"channel": m.CHANNEL_ID, "ts": "bad"}),
                             ("receipt", {"channel": m.CHANNEL_ID, "ts": "1.000001"}),
                             ("sent_at", m.iso(self.now - timedelta(days=1)))):
            proof = deepcopy(self.proof)
            proof["receipts"][0][field] = value
            with self.subTest(field=field):
                self.assert_held(proof=proof)

    def test_missing_acknowledgement_and_multiple_uncertain_rows_are_rejected(self):
        self.assert_held(proof={**self.proof, "receipts": []})
        proof = deepcopy(self.proof)
        proof["receipts"].append(proof["receipts"][0])
        self.assert_held(proof=proof)
        row = next(iter(self.store.doc["outbox"].values()))
        extra = {**deepcopy(row), "id": "f" * 64}
        self.store.doc["outbox"][extra["id"]] = extra
        self.assert_held()
        del self.store.doc["outbox"][extra["id"]]
        row["status"] = "needs_review"
        self.assert_held()

    def test_state_changed_after_proof_read_cannot_be_overwritten(self):
        before_puts = self.store.puts
        recovery = r.Recovery(self.store, lambda head: True)
        def changed(sha, state):
            candidate = recovery(sha, state)
            self.store.doc["operation_id"] = "external-change"
            return candidate
        with patch.dict(os.environ, {"GITHUB_RUN_ID": "202"}), patch.object(r, "download", return_value=self.proof):
            with self.assertRaisesRegex(ValueError, "state_changed_concurrently"):
                with Journal(self.store, NEXT_OWNER).exclusive(recover=changed):
                    self.fail("Must not acquire owner")
        self.assertEqual(self.store.puts, before_puts)
        self.assertEqual(self.store.doc["operation_id"], "external-change")
        self.assertEqual(len(self.sender.calls), 1)


class FailureBoundaryTests(unittest.TestCase):
    def test_prewrite_read_failure_after_slack_success_keeps_recoverable_owner(self):
        now = datetime.now(timezone.utc)
        store, sender = Store(planned(now)[0]), Sender(now)
        log = Mock()
        original_read = store.read
        def fail_after_receipt(*args):
            if log.record.called:
                raise ValueError("state_api_unavailable")
            return original_read(*args)
        with patch.object(store, "read", side_effect=fail_after_receipt):
            with self.assertRaisesRegex(ValueError, "state_api_unavailable"):
                with Journal(store, OWNER).exclusive() as journal:
                    d.dispatch(journal, sender, authorization(journal.state), now, log)
        self.assertEqual(store.doc["owner"], OWNER)
        self.assertEqual(next(iter(store.doc["outbox"].values()))["status"], "sending")
        self.assertEqual(len(sender.calls), 1)
        log.record.assert_called_once()

    def test_lost_slack_response_records_no_receipt_and_never_retries(self):
        now = datetime.now(timezone.utc)
        store = Store(planned(now)[0])
        sender = Sender(now, fail=True)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,
                {"GITHUB_RUN_ID": "101", "GITHUB_RUN_ATTEMPT": "1", "GITHUB_REPOSITORY": REPO, "GITHUB_SHA": HEAD}):
            log = r.ReceiptLog(tmp, REPO, "branded", OWNER)
            with Journal(store, OWNER).exclusive() as journal:
                result = d.dispatch(journal, sender, authorization(journal.state), now, log)
            self.assertEqual(result["needs_review"], 1)
            self.assertEqual(json.loads(log.path.read_text())["receipts"], [])
            with Journal(store, NEXT_OWNER).exclusive() as journal:
                result = d.dispatch(journal, sender, authorization(journal.state), now)
            self.assertEqual(result["needs_review"], 1)
            self.assertEqual(len(sender.calls), 1)

    def test_local_receipt_io_failure_keeps_owner_and_does_not_resend(self):
        now = datetime.now(timezone.utc)
        store = Store(planned(now)[0])
        sender = Sender(now)
        log = Mock()
        log.record.side_effect = OSError("SECRET filesystem error")
        with self.assertRaisesRegex(ValueError, "receipt_record_unconfirmed"):
            with Journal(store, OWNER).exclusive() as journal:
                d.dispatch(journal, sender, authorization(journal.state), now, log)
        self.assertEqual(store.doc["owner"], OWNER)
        self.assertEqual(next(iter(store.doc["outbox"].values()))["status"], "sending")
        self.assertEqual(len(sender.calls), 1)

    def test_success_receipt_file_cannot_be_reinitialized(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,
                {"GITHUB_RUN_ID": "101", "GITHUB_RUN_ATTEMPT": "1", "GITHUB_REPOSITORY": REPO, "GITHUB_SHA": HEAD}):
            r.ReceiptLog(tmp, REPO, "branded", OWNER)
            with self.assertRaisesRegex(ValueError, "receipt_file_already_exists"):
                r.ReceiptLog(tmp, REPO, "branded", OWNER)

    def test_observation_survives_before_journal_failure_without_claiming_completion(self):
        now = datetime.now(timezone.utc)
        snapshot = {"fetched_at": m.iso(now), "workflows": []}
        with tempfile.TemporaryDirectory() as tmp, patch.object(m, "collect", return_value=snapshot), \
                patch.object(runtime.verification, "enrich"), \
                patch.object(runtime, "run_journal", side_effect=ValueError("state_write_unconfirmed")), \
                patch.object(d.SlackSender, "post") as send:
            with self.assertRaisesRegex(ValueError, "state_write_unconfirmed"):
                runtime.run(args("branded", output=tmp))
            report = json.loads((Path(tmp) / "report.json").read_text(encoding="utf-8"))
            self.assertFalse(report["monitor_complete"])
            self.assertEqual(report["results"][0]["key"], "branded")
            self.assertEqual(json.loads((Path(tmp) / "evidence.json").read_text()), snapshot)
            send.assert_not_called()

    def test_policy_mismatch_is_caught_before_healthy_journal_or_send(self):
        now = datetime.now(timezone.utc)
        policy = {"approved": True, "repository": REPO, "target": "branded", "channel": m.CHANNEL_ID,
                  "approval_reference": "fixture", "mode": "recurring_template_exception",
                  "exception_scope": "only_these_marketing_alerts", "template_version": d.TEMPLATE_VERSION,
                  "code_sha256": d.code_fingerprint(), "not_before": m.iso(now - timedelta(days=1))}
        runtime.validate_release(policy, "branded", now)
        for field, value in (("approved", False), ("code_sha256", "old"), ("template_version", "old"),
                             ("channel", "other"), ("exception_scope", "all"), ("repository", "other/repo"),
                             ("expires_at", m.iso(now - timedelta(seconds=1)))):
            with self.subTest(field=field), self.assertRaises(ValueError):
                runtime.validate_release({**policy, field: value}, "branded", now)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,
                {"MONITOR_ENABLED": "true", "GITHUB_REPOSITORY": REPO, "GITHUB_REF": "refs/heads/main"}), \
                patch.object(m, "collect", return_value={"fetched_at": m.iso(now), "workflows": []}), \
                patch.object(runtime.verification, "enrich"), patch.object(runtime, "approved_checkout", return_value=True), \
                patch.object(runtime, "run_journal") as journal, patch.object(d.SlackSender, "post") as send:
            path = Path(tmp) / "policy.json"
            path.write_text(json.dumps({**policy, "code_sha256": "old"}))
            with self.assertRaisesRegex(ValueError, "monitor_policy_not_approved"):
                runtime.run(args("branded", persist=True, send=True, authorization=str(path), output=tmp))
            journal.assert_not_called()
            send.assert_not_called()


def archive_bytes(filename=r.FILE, content=b"{}"):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(filename, content)
    return stream.getvalue()


class DownloadTests(unittest.TestCase):
    def attempt(self, raw=None, digest=None, location=None, status=200):
        raw = archive_bytes() if raw is None else raw
        first = Mock(status_code=302, headers={"Location": location or "https://fixture.blob.core.windows.net/archive?sig=SECRET"})
        blob = Mock(status_code=status)
        blob.iter_content.return_value = [raw]
        context = Mock()
        context.__enter__ = Mock(return_value=blob)
        context.__exit__ = Mock(return_value=False)
        artifact = {"id": 1, "digest": digest or "sha256:" + hashlib.sha256(raw).hexdigest()}
        with patch("requests.get", side_effect=[first, context]) as get:
            result = r.download(Store(d.initial_state()), artifact)
        return result, get

    def test_signed_download_never_receives_token_and_cannot_redirect(self):
        result, get = self.attempt()
        self.assertEqual(result, {})
        self.assertIn("Authorization", get.call_args_list[0].kwargs["headers"])
        self.assertNotIn("headers", get.call_args_list[1].kwargs)
        self.assertTrue(all(call.kwargs["allow_redirects"] is False for call in get.call_args_list))

    def test_corrupt_tampered_oversized_wrong_filename_and_redirect_are_rejected(self):
        for options in ({"raw": b"bad zip"}, {"digest": "sha256:wrong"},
                        {"raw": b"x" * (r.MAX_ARCHIVE + 1)},
                        {"raw": archive_bytes("../receipt.json")},
                        {"raw": archive_bytes(content=b"x" * (r.MAX_DOCUMENT + 1))},
                        {"raw": archive_bytes(content=b"not json")},
                        {"location": "https://malicious.invalid/archive?token=SECRET"},
                        {"location": "http://fixture.blob.core.windows.net/archive"},
                        {"status": 302}, {"status": 410}):
            with self.subTest(options=list(options)), self.assertRaisesRegex(ValueError, "^receipt_artifact_unavailable$"):
                self.attempt(**options)


if __name__ == "__main__":
    unittest.main()
