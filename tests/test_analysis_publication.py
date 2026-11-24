from __future__ import annotations

import unittest

from silverwatch import analysis, cases, exports, lineage, publication, queries, registry, service
from silverwatch.contracts import Approval, Remediation
from silverwatch.errors import Problem
from tests.support import (DatabaseCase, decision_data, instant, resolve_case,
                          seed_case, touch_data)


def policy_data(**updates):
    value = {"policy_id": "policy-one", "minimum_exposure": 100,
             "minimum_supply_coverage_bp": 8000, "max_substantiated_rate_bp": 50,
             "severe_open_cases_block": True, "require_verified_remediation": True,
             "minimum_touch_coverage_bp": 8000}
    return value | updates


def verified_action(**updates):
    value = {"action_id": "fix-one", "case_id": "case-one", "provider_id": "provider-one",
             "owner": "operator-one", "due_at": "2026-10-20T00:00:00Z",
             "action": "Update the notice and contact affected residents.",
             "evidence_ref": "archive://replacement", "verified_by": "auditor-one",
             "verified_at": "2026-10-25T10:00:00Z", "status": "verified"}
    return value | updates


class AnalysisSemanticsTests(DatabaseCase, unittest.TestCase):
    def seed_complete(self, db):
        seed_case(db)
        resolve_case(db)
        cases.add_remediation(db, Remediation.model_validate(verified_action()))

    def run_analysis(self, db, *, as_of="2026-11-02T00:00:00Z", policy=None):
        return analysis.run(db, cut=registry.source_cut(db)["cut"],
                            as_of=instant(as_of), region="suburban", service_kind="meal",
                            cohort="70_79", policy=policy or __import__(
                                "silverwatch.contracts", fromlist=["AnalysisPolicy"]).AnalysisPolicy.model_validate(policy_data()))

    def test_clean_chain_shows_distinct_supply_and_risk_evidence(self):
        with self.transaction() as db:
            self.seed_complete(db)
            result = self.run_analysis(db)
            area = result["areas"][0]
            self.assertEqual(area["exposed"], 200)
            self.assertEqual(area["nonresponse"], 10)
            self.assertEqual(area["substantiated"], 1)
            self.assertEqual(area["supply_coverage_bp"], 12000)
            self.assertEqual(result["reconciliation"]["incomplete_batch_count"], 0)
            self.assertTrue(area["eligible"])

    def test_outreach_bounds_show_observed_and_possible_rate_separately(self):
        result = analysis.outreach_bounds(80, 20, 4, True)
        self.assertEqual(result["coverage_bp"], 8000)
        self.assertEqual(result["observed_rate"], "500.00bp")
        self.assertEqual(result["possible_rate_bounds"]["low"], "400.00bp")
        self.assertEqual(result["possible_rate_bounds"]["high"], "2400.00bp")
        self.assertFalse(result["coverage_complete"])

    def test_outreach_bounds_preserve_unknown_denominator_and_reject_invalid_counts(self):
        self.assertIsNone(analysis.outreach_bounds(0, 0, 0, False)["coverage_bp"])
        with self.assertRaises(Problem):
            analysis.outreach_bounds(3, 0, 4, True)

    def test_future_case_decisions_do_not_leak_into_past_analysis(self):
        with self.transaction() as db:
            seed_case(db)
            resolve_case(db)
            before = cases.finding(db, "case-one", as_of=instant("2026-10-11T00:00:00Z"))
            after = cases.finding(db, "case-one", as_of=instant("2026-10-13T00:00:00Z"))
            self.assertEqual(before["finding"], "pending")
            self.assertEqual(after["finding"], "substantiated")

    def test_source_observation_after_time_cut_is_excluded(self):
        with self.transaction() as db:
            seed_case(db, touch_observed_at="2026-10-01T00:00:00Z")
            resolve_case(db)
            cases.add_remediation(db, Remediation.model_validate(verified_action()))
            result = self.run_analysis(db, as_of="2026-10-11T12:00:00Z")
            self.assertEqual(result["case_counts"]["pending"], 1)
            self.assertEqual(result["case_counts"]["substantiated"], 0)

    def test_incomplete_coverage_blocks_eligibility_even_with_zero_nonresponse_count(self):
        with self.transaction() as db:
            seed_case(db, coverage_complete=False, nonresponse_people=0)
            resolve_case(db)
            cases.add_remediation(db, Remediation.model_validate(verified_action()))
            result = self.run_analysis(db)
            area = result["areas"][0]
            self.assertIn("coverage_unknown", area["gaps"])
            self.assertFalse(area["eligible"])

    def test_small_exposure_is_flagged_without_dividing_by_zero(self):
        with self.transaction() as db:
            seed_case(db, exposed_people=0, nonresponse_people=10)
            resolve_case(db)
            cases.add_remediation(db, Remediation.model_validate(verified_action()))
            result = self.run_analysis(db)
            area = result["areas"][0]
            self.assertIsNone(area["substantiated_rate"])
            self.assertIn("minimum_exposure", area["gaps"])

    def test_open_severe_case_blocks_area_until_case_management_closes_it(self):
        with self.transaction() as db:
            seed_case(db, severity="critical")
            resolve_case(db)
            cases.add_remediation(db, Remediation.model_validate(verified_action()))
            area = self.run_analysis(db)["areas"][0]
            self.assertIn("severe_open_cases", area["gaps"])
            from silverwatch import review
            review.close(db, "case-one", "substantiated", 1)
            closed = self.run_analysis(db, as_of="2026-11-25T00:00:00Z")["areas"][0]
            self.assertNotIn("severe_open_cases", closed["gaps"])

    def test_unmatched_report_intake_is_visible_and_blocks_area(self):
        with self.transaction() as db:
            seed_case(db, report_count=3)
            resolve_case(db)
            area = self.run_analysis(db)["areas"][0]
            self.assertEqual(area["reconciliation_incomplete_batches"], 1)
            self.assertIn("case_intake_incomplete", area["gaps"])

    def test_unverified_confirmed_case_cannot_claim_remediation_complete(self):
        with self.transaction() as db:
            seed_case(db)
            resolve_case(db)
            row = self.run_analysis(db)["areas"][0]
            self.assertIn("remediation_unverified", row["gaps"])

    def test_analysis_rejects_cut_beyond_current_ledger(self):
        with self.transaction() as db:
            self.seed_complete(db)
            with self.assertRaises(Problem) as captured:
                analysis.run(db, cut=99999, as_of=instant("2026-11-02T00:00:00Z"),
                            region="suburban", service_kind=None, cohort=None,
                            policy=__import__("silverwatch.contracts", fromlist=["AnalysisPolicy"])
                                .AnalysisPolicy.model_validate(policy_data()))
            self.assertEqual(captured.exception.code, "analysis_cut")

    def test_district_overview_keeps_report_count_and_case_finding_separate(self):
        with self.transaction() as db:
            self.seed_complete(db)
            overview = queries.district_overview(db, "suburban", as_of=instant("2026-11-02T00:00:00Z"))
            area = overview["areas"][0]
            self.assertEqual(area["reported_issues"], 1)
            self.assertEqual(area["substantiated"], 1)
            self.assertEqual(area["touch_coverage_bp"], 9524)


class PublicationLifecycleTests(DatabaseCase, unittest.TestCase):
    def create_analysis(self, db, suffix=""):
        seed_case(db)
        resolve_case(db)
        cases.add_remediation(db, Remediation.model_validate(verified_action()))
        request = {"analysis_id": "analysis" + suffix, "region": "suburban", "service_kind": "meal",
                   "cohort": "70_79", "as_of": "2026-11-02T00:00:00Z", "policy": policy_data(),
                   "through_cut": registry.source_cut(db)["cut"]}
        return service.analyze(db, request)

    def approve_both(self, db, created, suffix=""):
        approvals = []
        for role, reviewer in (("service_planning", "planner" + suffix),
                               ("consumer_protection", "ombuds" + suffix)):
            body = Approval(approval_id=f"approval-{role.replace('_', '-')}-{suffix}", analysis_id=created["analysis_id"],
                            reviewer=reviewer, role=role, approved=True,
                            analysis_digest=created["digest"], rationale="The frozen evidence is reviewed.")
            approvals.append(publication.approve(db, body))
        return approvals

    def test_approval_must_match_exact_analysis_digest(self):
        with self.transaction() as db:
            created = self.create_analysis(db)
            body = Approval(approval_id="stale-approval", analysis_id=created["analysis_id"],
                            reviewer="planner", role="service_planning", approved=True,
                            analysis_digest="0" * 64, rationale="Stale snapshot.")
            with self.assertRaises(Problem) as captured:
                publication.approve(db, body)
            self.assertEqual(captured.exception.code, "analysis_changed")

    def test_same_reviewer_cannot_fill_both_publication_roles(self):
        with self.transaction() as db:
            created = self.create_analysis(db)
            self.approve_both(db, created)
            with self.assertRaises(Problem) as captured:
                body = Approval(approval_id="third-role", analysis_id=created["analysis_id"],
                                reviewer="planner", role="service_planning", approved=True,
                                analysis_digest=created["digest"], rationale="Duplicate reviewer.")
                publication.approve(db, body)
            self.assertEqual(captured.exception.code, "role_already_reviewed")

    def test_publication_requires_both_approved_roles(self):
        with self.transaction() as db:
            created = self.create_analysis(db)
            with self.assertRaises(Problem) as captured:
                publication.publish(db, created["analysis_id"], 0)
            self.assertEqual(captured.exception.code, "publication_blocked")

    def test_approval_denial_prevents_publication(self):
        with self.transaction() as db:
            created = self.create_analysis(db)
            allowed = Approval(approval_id="planning-ok", analysis_id=created["analysis_id"],
                               reviewer="planner", role="service_planning", approved=True,
                               analysis_digest=created["digest"], rationale="Supply reviewed.")
            denied = Approval(approval_id="protection-no", analysis_id=created["analysis_id"],
                              reviewer="ombuds", role="consumer_protection", approved=False,
                              analysis_digest=created["digest"], rationale="Needs more evidence.")
            publication.approve(db, allowed)
            publication.approve(db, denied)
            self.assertIn("approval_denied", publication.approval_set(db, created["analysis_id"])["failures"])

    def test_publication_freezes_document_and_increments_edition(self):
        with self.transaction() as db:
            created = self.create_analysis(db)
            self.approve_both(db, created)
            first = publication.publish(db, created["analysis_id"], 0)
            second = publication.publish(db, created["analysis_id"], 1)
            self.assertEqual((first["edition"], second["edition"]), (1, 2))
            self.assertEqual(publication.bulletin(db, created["analysis_id"], 1), first)
            self.assertEqual(publication.compare(db, created["analysis_id"], 1, 2)["changed"], False)

    def test_stale_expected_bulletin_edition_is_rejected(self):
        with self.transaction() as db:
            created = self.create_analysis(db)
            self.approve_both(db, created)
            publication.publish(db, created["analysis_id"], 0)
            with self.assertRaises(Problem) as captured:
                publication.publish(db, created["analysis_id"], 0)
            self.assertEqual(captured.exception.code, "bulletin_revision")

    def test_export_integrity_and_csv_uncertainty_columns_are_preserved(self):
        with self.transaction() as db:
            created = self.create_analysis(db)
            self.approve_both(db, created)
            published = publication.publish(db, created["analysis_id"], 0)
            exported = exports.bulletin_csv(db, created["analysis_id"])
            self.assertTrue(exported["content"].startswith("\ufeffdistrict,"))
            self.assertIn("nonresponse", exported["content"])
            self.assertEqual(lineage.verify_bulletin(db, created["analysis_id"], 1)["valid"], True)
            self.assertEqual(exports.publication_manifest(db, created["analysis_id"])["bulletin_digest"],
                             published["bulletin_digest"])

    def test_analysis_revisions_do_not_reuse_approvals_for_changed_digest(self):
        with self.transaction() as db:
            first = self.create_analysis(db)
            self.approve_both(db, first)
            request = {"analysis_id": first["analysis_id"], "region": "suburban",
                       "as_of": "2026-11-03T00:00:00Z", "policy": policy_data(),
                       "through_cut": registry.source_cut(db)["cut"]}
            second = service.analyze(db, request)
            state = publication.approval_set(db, first["analysis_id"], 2)
            self.assertEqual(state["approvals"], [])
            self.assertNotEqual(first["digest"], second["digest"])

    def test_analysis_integrity_check_recomputes_document_and_ledger_digests(self):
        with self.transaction() as db:
            created = self.create_analysis(db)
            result = lineage.verify_analysis(db, created["analysis_id"])
            self.assertTrue(result["valid"])
            self.assertTrue(result["checks"]["source_ledger_valid"])

    def test_analysis_integrity_check_detects_document_tampering(self):
        with self.transaction() as db:
            created = self.create_analysis(db)
            db.execute("UPDATE analysis SET document=replace(document,'district-a','district-b') WHERE analysis_id=?",
                       (created["analysis_id"],))
            result = lineage.verify_analysis(db, created["analysis_id"])
            self.assertFalse(result["valid"])
            self.assertFalse(result["checks"]["stored_digest_valid"])


if __name__ == "__main__":
    unittest.main()
