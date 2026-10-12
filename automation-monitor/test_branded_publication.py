"""Publication checks for both sides of the branded-search cutover."""
import copy
from pathlib import Path
import unittest

import yaml
import monitor as m
from test_monitor import NOW, evidence, run


LEGACY = "Read and build (no Sheet writes)"
PRODUCTION = ("Execute checked production operation", "Validate generated production page")
PUBLISH = ("Deploy", "Verify exact delivery and noindex")


def production(**overrides):
    steps = {LEGACY: "skipped", **{s: "success" for s in PRODUCTION + PUBLISH}}
    steps.update(overrides)
    return run("branded", steps=steps, created="2026-10-08T06:00:00Z")


def assess(candidate):
    return m.assess("branded", evidence("branded", [candidate]), NOW)


class BrandedPublicationTests(unittest.TestCase):
    def test_new_production_route_is_verified(self):
        for event in ("schedule", "workflow_dispatch"):
            candidate = production()
            candidate["event"] = event
            self.assertEqual(assess(candidate)["condition"], "publication_verified")

    def test_all_production_and_publication_steps_are_required(self):
        for name in PRODUCTION + PUBLISH:
            for conclusion in (None, "skipped", "failure", "cancelled"):
                with self.subTest(step=name, conclusion=conclusion):
                    candidate = production(**{name: conclusion})
                    self.assertNotIn(assess(candidate)["state"], {"healthy", "recovered"})

    def test_missing_steps_never_count_as_success(self):
        for name in PRODUCTION + PUBLISH:
            candidate = production()
            candidate["jobs"][0]["steps"] = [s for s in candidate["jobs"][0]["steps"] if s["name"] != name]
            self.assertNotIn(assess(candidate)["state"], {"healthy", "recovered"})

    def test_whole_run_failure_is_not_masked_by_successful_steps(self):
        for conclusion in ("failure", "cancelled", "timed_out"):
            candidate = production()
            candidate["conclusion"] = conclusion
            self.assertEqual(assess(candidate)["state"], "action_required")

    def test_legacy_route_remains_supported(self):
        for extra in ({}, {s: "skipped" for s in PRODUCTION}):
            steps = {LEGACY: "success", **{s: "success" for s in PUBLISH}, **extra}
            self.assertEqual(assess(run("branded", steps=steps, created="2026-10-08T06:00:00Z"))["state"], "healthy")

    def test_active_or_failed_other_route_is_not_ignored(self):
        candidate = production(**{LEGACY: "success"})
        self.assertNotIn(assess(candidate)["state"], {"healthy", "recovered"})
        for name in PRODUCTION:
            candidate = run("branded", steps={LEGACY: "success", **{s: "success" for s in PUBLISH}, name: "failure"}, created="2026-10-08T06:00:00Z")
            self.assertNotIn(assess(candidate)["state"], {"healthy", "recovered"})
        self.assertNotIn(assess(production(**{LEGACY: "failure"}))["state"], {"healthy", "recovered"})

    def test_duplicate_step_names_are_ambiguous(self):
        for candidate in (production(), run("branded", steps={s: "success" for s in (LEGACY,) + PUBLISH}, created="2026-10-08T06:00:00Z")):
            for name in (LEGACY,) + PRODUCTION + PUBLISH:
                variant = copy.deepcopy(candidate)
                variant["jobs"].append({"name": "duplicate", "steps": [{"name": name, "conclusion": "success"}, {"name": name, "conclusion": "success"}]})
                self.assertNotIn(assess(variant)["state"], {"healthy", "recovered"})

    def test_compare_only_cannot_resolve_previous_failure(self):
        failed = production()
        failed["conclusion"] = "failure"
        compare = production(**{"Collect and compare without writing": "success"})
        compare.update(id=11, event="workflow_dispatch", created_at="2026-10-08T06:30:00Z")
        result = m.assess("branded", evidence("branded", [compare, failed]), NOW)
        self.assertEqual(result["latest_run"], 10)
        self.assertEqual(result["state"], "action_required")

    def test_build_only_cannot_resolve_previous_failure(self):
        failed = production()
        failed["conclusion"] = "failure"
        build = production(**{"Deploy": "skipped", "Verify exact delivery and noindex": "skipped"})
        build.update(id=11, event="workflow_dispatch", created_at="2026-10-08T06:30:00Z")
        result = m.assess("branded", evidence("branded", [build, failed]), NOW)
        self.assertEqual(result["latest_run"], 10)
        self.assertEqual(result["state"], "action_required")

    def test_monitored_stages_exist_once_in_business_workflow(self):
        workflow = yaml.safe_load((Path(__file__).resolve().parents[1] / ".github/workflows/update.yml").read_text(encoding="utf-8"))
        names = [s.get("name") for j in workflow["jobs"].values() for s in j.get("steps", [])]
        for name in (LEGACY,) + PRODUCTION + PUBLISH:
            self.assertEqual(names.count(name), 1, name)


if __name__ == "__main__":
    unittest.main()
