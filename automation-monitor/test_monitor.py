import copy
import json
import re
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import monitor as m

NOW = m.parse_time("2026-10-08T07:00:00Z")  # 16:00 JST


def run(key, rid=10, conclusion="success", steps=None, event="schedule", created="2026-10-08T00:06:00Z", summary=None):
    return {"id": rid, "attempt": 1, "event": event, "branch": "main", "workflow_id": m.CONFIG[key]["workflow"],
            "status": "completed", "conclusion": conclusion, "created_at": created, "started_at": created,
            "updated_at": m.iso(m.parse_time(created) + timedelta(minutes=5)), "url": f"https://github.com/{m.CONFIG[key]['repo']}/actions/runs/{rid}",
            "summary": summary, "signals": {}, "jobs": [{"name": "business", "steps": [{"name": k, "conclusion": v} for k,v in (steps or {}).items()]}]}


def evidence(key, runs):
    return {"key": key, "errors": [], "workflow": {"id": m.CONFIG[key]["workflow"], "path": m.CONFIG[key]["path"], "state": "active", "created_at": "2026-10-08T03:12:00Z" if key == "branded" else "2026-09-01T00:00:00Z"},
            "cron": m.CONFIG[key]["cron"], "flags": {}, "runs": runs,
            "run_headers": [{"id": 1, "event": "schedule", "created_at": "2026-10-07T21:16:00Z" if key == "bizibl" else "2026-10-08T00:06:00Z", "status": "completed", "conclusion": "failure"}]}


def summary(holds=None, pending=0, status=None, mode="execute"):
    holds = holds or {}
    return {"status": status or ("needs_review" if holds else "passed"), "mode": mode,
            "completed": 1, "invitation_pending": pending, "deferred": 0, "enrolled_without_resend": 0, "held_reasons": holds,
            "business_writes": {"account": 1, "enrollment": 1, "invite": 1}}


class BusinessAssessment(unittest.TestCase):
    def test_takken_hold_keeps_completed_work_visible(self):
        r = run("takken", conclusion="failure", summary=summary({"name_email_conflict": 2}, 1))
        result = m.assess("takken", evidence("takken", [r]), NOW)
        self.assertEqual(result["state"], "action_required")
        self.assertIn("2件", result["impact"])
        self.assertIn("会員作成 1件", result["done"])

    def test_invite_receipt_wait_is_not_registration_failure(self):
        r = run("takken", summary=summary(pending=1))
        result = m.assess("takken", evidence("takken", [r]), NOW)
        self.assertEqual(result["state"], "watching")
        self.assertEqual(result["condition"], "invitation_wait")

    def test_missing_or_invalid_business_summary_is_not_healthy(self):
        for value in [None, {**summary(), "invalid_counts": True}]:
            with self.subTest(value=value):
                r = run("takken", summary=value)
                self.assertEqual(m.assess("takken", evidence("takken", [r]), NOW)["state"], "unknown")

    def test_successful_preflight_does_not_hide_hold(self):
        hold = run("takken", 10, "failure", summary=summary({"name_email_conflict": 2}))
        trial = run("takken", 11, created="2026-10-08T06:00:00Z", event="workflow_dispatch", summary=summary(mode="preflight"))
        result = m.assess("takken", evidence("takken", [trial, hold]), NOW)
        self.assertEqual(result["latest_run"], 10)
        self.assertEqual(result["state"], "action_required")

    def test_bizibl_success_with_skipped_distribution_is_partial(self):
        r = run("bizibl", steps=dict(zip(m.BIZIBL_STEPS, ["success", "skipped", "skipped"])), created="2026-10-08T06:26:00Z", event="workflow_dispatch")
        r["signals"] = {"rows_written": 2179}
        result = m.assess("bizibl", evidence("bizibl", [r]), NOW)
        self.assertEqual(result["state"], "watching")
        self.assertEqual(result["condition"], "partial_recovery")
        self.assertEqual(m.jp(result["deadline"]), "10/08 19:30")

    def test_all_bizibl_steps_and_output_marker_required(self):
        old = run("bizibl", 9, "failure", created="2026-10-07T21:16:00Z")
        new = run("bizibl", 10, steps={x: "success" for x in m.BIZIBL_STEPS}, event="workflow_dispatch", created="2026-10-08T06:20:00Z")
        new["signals"] = {"rows_written": 2179}
        self.assertEqual(m.assess("bizibl", evidence("bizibl", [new, old]), NOW)["state"], "recovered")
        new["signals"] = {}
        self.assertNotIn(m.assess("bizibl", evidence("bizibl", [new, old]), NOW)["state"], {"recovered", "healthy"})

    def test_brand_compare_success_does_not_mask_publication_failure(self):
        failed = run("branded", 10, "failure", created="2026-10-08T03:20:00Z")
        compare = run("branded", 11, steps={"Collect and compare without writing": "success", "Deploy": "skipped"}, created="2026-10-08T06:18:00Z", event="workflow_dispatch")
        result = m.assess("branded", evidence("branded", [compare, failed]), NOW)
        self.assertEqual(result["state"], "action_required")
        self.assertEqual(result["latest_run"], 10)

    def test_brand_uses_verified_publication_not_newer_compare(self):
        published = run("branded", 10, steps={x: "success" for x in m.BRAND_STEPS}, created="2026-10-08T03:21:00Z", event="workflow_dispatch")
        compare = run("branded", 11, steps={"Collect and compare without writing": "success"}, created="2026-10-08T06:18:00Z", event="workflow_dispatch")
        published["prior_failure"] = True
        result = m.assess("branded", evidence("branded", [compare, published]), NOW)
        self.assertEqual(result["state"], "recovered")
        self.assertEqual(result["latest_run"], 10)

    def test_brand_build_only_is_not_recovery(self):
        r = run("branded", steps={m.BRAND_STEPS[0]: "success"}, event="workflow_dispatch", created="2026-10-08T03:20:00Z")
        self.assertEqual(m.assess("branded", evidence("branded", [r]), NOW)["state"], "unknown")

    def test_recent_error_waits_then_requires_action(self):
        r = run("bizibl", conclusion="failure", created=m.iso(NOW-timedelta(minutes=10)))
        r["signals"]["click_timeout"] = True
        self.assertEqual(m.assess("bizibl", evidence("bizibl", [r]), NOW)["state"], "watching")
        self.assertEqual(m.assess("bizibl", evidence("bizibl", [r]), NOW+timedelta(minutes=20))["state"], "action_required")

    def test_other_workflow_or_branch_cannot_resolve_failure(self):
        failed = run("takken", summary=summary({"name_email_conflict": 2}), conclusion="failure")
        wrong = run("takken", 11, summary=summary(), created="2026-10-08T06:00:00Z")
        for field, value in [("branch", "experiment"), ("workflow_id", 999), ("event", "pull_request")]:
            with self.subTest(field=field):
                candidate = {**wrong, field: value}
                result = m.assess("takken", evidence("takken", [candidate, failed]), NOW)
                self.assertEqual(result["state"], "action_required")


class ScheduleAssessment(unittest.TestCase):
    def test_schedule_missing_even_if_manual_check_succeeded(self):
        e = evidence("takken", [run("takken", summary=summary(), event="workflow_dispatch")])
        e["run_headers"] = []
        self.assertEqual(m.assess("takken", e, NOW)["condition"], "schedule_missing")

    def test_schedule_delay_grace(self):
        e = evidence("takken", [run("takken", summary=summary(), created="2026-10-07T09:00:00Z")])
        e["run_headers"] = []
        self.assertNotEqual(m.assess("takken", e, "2026-10-08T00:30:00Z")["condition"], "schedule_missing")

    def test_weekly_workflow_not_due_before_it_was_created(self):
        r = run("branded", steps={s: "success" for s in m.BRAND_STEPS}, created="2026-10-08T03:21:00Z")
        self.assertEqual(m.assess("branded", evidence("branded", [r]), NOW)["state"], "healthy")

    def test_disabled_or_changed_schedule_never_reported_healthy(self):
        original = evidence("takken", [run("takken", summary=summary())])
        for variant in ["disabled", "changed"]:
            e = copy.deepcopy(original)
            if variant == "disabled": e["flags"] = {"AUTOMATION_ENABLED": "false"}
            else: e["cron"] = ["* * * * *"]
            self.assertEqual(m.assess("takken", e, NOW)["state"], "unknown")

    def test_long_running_job_is_not_normal(self):
        r = run("bizibl", created="2026-10-08T06:00:00Z")
        r["status"] = "in_progress"; r["conclusion"] = None
        result = m.assess("bizibl", evidence("bizibl", [r]), NOW)
        self.assertEqual(result["condition"], "run_stalled")

    def test_utc_jst_boundary_and_weekly_next(self):
        due, after = m.slots_around(m.CONFIG["bizibl"], "2026-10-07T21:01:00Z")
        self.assertEqual(m.jp(due), "10/08 06:00")
        self.assertEqual(m.jp(after), "10/08 18:00")
        self.assertEqual(m.jp(m.slots_around(m.CONFIG["branded"], NOW)[1]), "10/12 09:17")
        with self.assertRaises(ValueError): m.parse_time("2026-10-08T16:00:00")


class NotificationsAndPrivacy(unittest.TestCase):
    def result(self):
        return m.assess("takken", evidence("takken", [run("takken", conclusion="failure", summary=summary({"name_email_conflict": 2}))]), NOW)

    def test_same_observation_deduplicated_but_new_held_people_not_assumed_same(self):
        r = self.result()
        event, state = m.preview_event(r, None, NOW)
        self.assertEqual(event, "new")
        self.assertEqual(m.preview_event(copy.deepcopy(r), state, NOW+timedelta(minutes=15))[0], "suppressed")
        changed = copy.deepcopy(r); changed["signature_data"]["source_run"] = 11
        self.assertEqual(m.preview_event(changed, state, NOW+timedelta(minutes=15))[0], "changed")

    def test_resolved_problem_notifies_recovery_only_after_observed_problem(self):
        r = self.result(); _, state = m.preview_event(r, None, NOW)
        good = {**r, "state": "recovered", "condition": "registration_passed", "signature_data": {}}
        self.assertEqual(m.preview_event(good, state, NOW+timedelta(minutes=15))[0], "recovery")
        self.assertEqual(m.preview_event(good, None, NOW)[0], "digest")

    def test_reminder_requires_both_deadline_and_24hours(self):
        r = self.result(); _, state = m.preview_event(r, None, NOW)
        self.assertEqual(m.preview_event(copy.deepcopy(r), state, NOW+timedelta(hours=3))[0], "suppressed")
        self.assertEqual(m.preview_event(copy.deepcopy(r), state, NOW+timedelta(hours=25))[0], "reminder")

    def test_partial_recovery_deadline_cannot_slide_forever(self):
        r = {**self.result(), "state": "watching", "condition": "partial_recovery", "signature_data": {}, "deadline": "2026-10-08T10:30:00Z"}
        _, state = m.preview_event(r, None, NOW)
        later = {**r, "deadline": "2026-10-09T10:30:00Z"}
        event, saved = m.preview_event(later, state, "2026-10-08T11:00:00Z")
        self.assertEqual(later["state"], "action_required")
        self.assertEqual(saved["deadline"], "2026-10-08T10:30:00Z")
        self.assertEqual(event, "changed")

    def test_raw_log_and_unrecognized_reason_are_not_copied(self):
        raw = {**summary({"name_email_conflict": 2, "private-person@example.com": 1}), "email": "private-person@example.com", "token": "xoxp-private-value"}
        prefixed = "\n".join("registration\tRun registration or preflight\t2026-10-08T00:10:00Z " + line for line in json.dumps(raw, indent=2).splitlines())
        parsed = m.extract_summary(prefixed)
        serialized = json.dumps(parsed)
        self.assertNotIn("example.com", serialized)
        self.assertNotIn("xoxp", serialized)
        self.assertEqual(parsed["held_reasons"]["unrecognized_reason"], 1)

    def test_stale_evidence_and_single_connection_failure_visible(self):
        snapshot = {"fetched_at": m.iso(NOW), "workflows": [evidence("takken", [run("takken", summary=summary())]), {"key": "bizibl", "errors": ["offline"]}]}
        report, _ = m.build_report(snapshot)
        by_key = {r["key"]: r for r in report["results"]}
        self.assertEqual(by_key["bizibl"]["state"], "unknown")
        self.assertEqual(by_key["takken"]["state"], "healthy")
        stale, _ = m.build_report(snapshot, now=NOW+timedelta(hours=1))
        self.assertTrue(all(r["state"] == "unknown" for r in stale["results"]))

    def test_truncated_success_summary_is_not_accepted(self):
        parsed = m.extract_summary('{"status":"passed","mode":"execute"}')
        self.assertTrue(parsed["invalid_counts"])

    def test_replay_cannot_present_future_evidence_as_current(self):
        snapshot = {"fetched_at": m.iso(NOW+timedelta(hours=1)), "workflows": []}
        report, _ = m.build_report(snapshot, now=NOW)
        self.assertTrue(all(r["condition"] == "future_snapshot" for r in report["results"]))

    def test_transport_only_permits_scoped_read_operations(self):
        calls = []
        reader = m.GitHubReader(m.CONFIG["bizibl"]["repo"])
        with patch.object(reader, "_run", side_effect=lambda args: calls.append(args) or '{}'):
            reader.get("actions/workflows/301946418")
            self.assertEqual(calls[0][:3], ["api", "--method", "GET"])
            with self.assertRaises(ValueError): reader.get("../../other-repo/secrets")
        with self.assertRaises(ValueError): m.GitHubReader("other/private")

    def test_preview_html_escapes_content_and_has_no_live_sender(self):
        r = self.result(); r["title"] = '<script>alert("secret")</script>'
        r["message_text"] = m.message(r); r["preview_event"] = "new"
        output = m.render_html({"evidence_at": m.iso(NOW), "results": [r]})
        self.assertNotIn("<script>", output)
        self.assertIn("&lt;script&gt;", output)
        self.assertIn("noindex,nofollow", output)


class LiveEvidenceReplay(unittest.TestCase):
    @unittest.skipUnless((m.ROOT / "work/261008_automation_monitor/evidence.json").exists(), "Live evidence has not been collected")
    def test_real_october_8_cases_and_transition(self):
        folder = m.ROOT / "work/261008_automation_monitor"
        snapshot = json.loads((folder / "evidence.json").read_text(encoding="utf-8"))
        report, state = m.build_report(snapshot)
        self.assertEqual({r["key"]: r["state"] for r in report["results"]}, {"takken": "action_required", "bizibl": "watching", "branded": "healthy"})
        again, _ = m.build_report(snapshot, state, m.parse_time(snapshot["fetched_at"]) + timedelta(minutes=15))
        self.assertEqual({r["key"]: r["preview_event"] for r in again["results"]}, {"takken": "suppressed", "bizibl": "suppressed", "branded": "digest"})
        old = copy.deepcopy(snapshot)
        cutoff = m.parse_time("2026-10-08T06:04:00Z")
        old["fetched_at"] = m.iso(cutoff)
        for workflow in old["workflows"]:
            workflow["runs"] = [r for r in workflow["runs"] if m.parse_time(r["updated_at"]) <= cutoff]
            workflow["run_headers"] = [r for r in workflow["run_headers"] if m.parse_time(r["created_at"]) <= cutoff]
        before, previous = m.build_report(old)
        self.assertEqual({r["key"]: r["state"] for r in before["results"]}, {"takken": "action_required", "bizibl": "action_required", "branded": "healthy"})
        changed, _ = m.build_report(snapshot, previous)
        self.assertEqual(next(r for r in changed["results"] if r["key"] == "bizibl")["preview_event"], "changed")
        for name in ("evidence.json", "report.json", "preview-state.json"):
            self.assertIsNone(re.search(r"\bxox[baprs]-|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}|-----BEGIN [A-Z ]*PRIVATE KEY", (folder / name).read_text(encoding="utf-8")))
        output = m.render_html(report)
        self.assertEqual(output.count("<article "), 3)
        self.assertIn("Slackへの送信は0件", output)


if __name__ == "__main__":
    unittest.main()
