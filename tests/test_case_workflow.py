from __future__ import annotations

import unittest

from silverwatch import cases, evidence, lineage, queries, registry, review
from silverwatch.contracts import (Case, CaseEvidence, Remediation, ReviewDecision,
                                   TouchBatch)
from silverwatch.errors import Problem
from tests.support import (DatabaseCase, case_data, decision_data, evidence_data,
                          instant, provider_data, resolve_case, seed_case, touch_data)


def remediation_data(**updates):
    value = {"action_id": "fix-one", "case_id": "case-one", "provider_id": "provider-one",
             "owner": "operator-one", "due_at": "2026-10-20T00:00:00Z",
             "action": "Replace the public notice and contact recipients.",
             "evidence_ref": None, "verified_by": None, "verified_at": None, "status": "planned"}
    return value | updates


class EvidenceAndDecisionTests(DatabaseCase, unittest.TestCase):
    def test_evidence_reference_must_belong_to_the_named_case(self):
        with self.transaction() as db:
            seed_case(db)
            unknown = CaseEvidence.model_validate(evidence_data(evidence_id="other-evidence", case_id="other-case"))
            with self.assertRaises(Problem) as captured:
                evidence.add(db, unknown)
            self.assertEqual(captured.exception.status, 404)

    def test_identical_evidence_replay_is_idempotent(self):
        with self.transaction() as db:
            seed_case(db)
            body = CaseEvidence.model_validate(evidence_data())
            first = evidence.add(db, body)
            second = evidence.add(db, body)
            self.assertTrue(first["replayed"])
            self.assertTrue(second["replayed"])
            self.assertEqual(first["digest"], second["digest"])

    def test_evidence_identifier_cannot_be_reused_for_different_content(self):
        with self.transaction() as db:
            seed_case(db)
            first = CaseEvidence.model_validate(evidence_data())
            evidence.add(db, first)
            changed = CaseEvidence.model_validate(evidence_data(content_digest="b" * 64))
            with self.assertRaises(Problem) as captured:
                evidence.add(db, changed)
            self.assertEqual(captured.exception.code, "evidence_changed")

    def test_unverified_material_cannot_support_a_substantiated_finding(self):
        with self.transaction() as db:
            seed_case(db)
            evidence.add(db, CaseEvidence.model_validate(evidence_data(evidence_id="unverified-material", verified=False)))
            finding = ReviewDecision.model_validate(decision_data(evidence_refs=["unverified-material"]))
            with self.assertRaises(Problem) as captured:
                cases.submit_decision(db, finding)
            self.assertEqual(captured.exception.code, "unverified_evidence")

    def test_decisions_cannot_cite_evidence_not_yet_observed(self):
        with self.transaction() as db:
            seed_case(db)
            late_material = CaseEvidence.model_validate(evidence_data(evidence_id="late-material",
                                                                       observed_at="2026-10-13T00:00:00Z"))
            evidence.add(db, late_material)
            decision = ReviewDecision.model_validate(decision_data(decided_at="2026-10-12T10:00:00Z",
                                                                    evidence_refs=["late-material"]))
            with self.assertRaises(Problem) as captured:
                cases.submit_decision(db, decision)
            self.assertEqual(captured.exception.code, "evidence_reference")

    def test_two_roles_with_same_reviewer_do_not_form_independent_review(self):
        with self.transaction() as db:
            seed_case(db)
            first = ReviewDecision.model_validate(decision_data())
            cases.submit_decision(db, first)
            second = ReviewDecision.model_validate(decision_data("compliance", "reviewer-one"))
            with self.assertRaises(Problem) as captured:
                cases.submit_decision(db, second)
            self.assertEqual(captured.exception.code, "reviewer_not_independent")

    def test_role_decisions_must_use_distinct_role_slots(self):
        with self.transaction() as db:
            seed_case(db)
            cases.submit_decision(db, ReviewDecision.model_validate(decision_data()))
            changed_same_role = ReviewDecision.model_validate(decision_data(
                role="investigator", reviewer="reviewer-three", decision_id="second-decision"))
            with self.assertRaises(Problem) as captured:
                cases.submit_decision(db, changed_same_role)
            self.assertEqual(captured.exception.code, "decision_sealed")

    def test_identical_decision_replay_returns_without_duplicate_event(self):
        with self.transaction() as db:
            seed_case(db)
            body = ReviewDecision.model_validate(decision_data())
            first = cases.submit_decision(db, body)
            second = cases.submit_decision(db, body)
            self.assertFalse(first["replayed"])
            self.assertTrue(second["replayed"])
            self.assertEqual(len(cases.decisions(db, "case-one")), 1)

    def test_disagreeing_roles_remain_inconclusive_instead_of_majority_vote(self):
        with self.transaction() as db:
            seed_case(db)
            resolve_case(db, first="substantiated", second="not_substantiated")
            self.assertEqual(cases.finding(db, "case-one")["finding"], "inconclusive")

    def test_unreviewed_report_is_pending_and_not_an_established_case(self):
        with self.transaction() as db:
            seed_case(db)
            self.assertEqual(cases.finding(db, "case-one")["finding"], "pending")
            self.assertEqual(cases.finding(db, "case-one")["decisions"], [])


class RemediationLifecycleTests(DatabaseCase, unittest.TestCase):
    def test_remediation_requires_matching_confirmed_provider(self):
        with self.transaction() as db:
            seed_case(db)
            resolve_case(db, first="not_substantiated", second="not_substantiated")
            with self.assertRaises(Problem) as captured:
                cases.add_remediation(db, Remediation.model_validate(remediation_data()))
            self.assertEqual(captured.exception.code, "case_not_substantiated")

    def test_verified_remediation_requires_an_independent_reviewer(self):
        with self.transaction() as db:
            seed_case(db)
            resolve_case(db)
            invalid = remediation_data(evidence_ref="archive://proof", verified_by="operator-one",
                                       verified_at="2026-10-19T00:00:00Z", status="verified")
            with self.assertRaises(ValueError):
                Remediation.model_validate(invalid)

    def test_confirmed_case_cannot_close_before_remediation_is_verified(self):
        with self.transaction() as db:
            seed_case(db)
            resolve_case(db)
            cases.add_remediation(db, Remediation.model_validate(remediation_data()))
            with self.assertRaises(Problem) as captured:
                review.close(db, "case-one", "substantiated", 1)
            self.assertEqual(captured.exception.code, "remediation_open")

    def test_close_requires_the_expected_current_finding(self):
        with self.transaction() as db:
            seed_case(db)
            resolve_case(db)
            with self.assertRaises(Problem) as captured:
                review.close(db, "case-one", "not_substantiated", 1)
            self.assertEqual(captured.exception.code, "finding_changed")

    def test_verified_remediation_and_case_close_join_the_case_timeline(self):
        with self.transaction() as db:
            seed_case(db)
            resolve_case(db)
            planned = Remediation.model_validate(remediation_data())
            cases.add_remediation(db, planned)
            verified = Remediation.model_validate(remediation_data(
                evidence_ref="archive://proof", verified_by="auditor-one",
                verified_at="2026-10-19T00:00:00Z", status="verified"))
            cases.update_remediation(db, verified.action_id, verified, 2)
            closed = review.close(db, "case-one", "substantiated", 1)
            timeline = queries.case_timeline(db, "case-one")
            self.assertEqual(closed["seq"], timeline["case_event"])
            self.assertEqual(timeline["finding"]["finding"], "substantiated")
            self.assertEqual(timeline["remediations"][0]["status"], "verified")
            self.assertEqual(timeline["case"]["status"], "closed")

    def test_remediation_queue_sorts_due_and_submitted_actions(self):
        with self.transaction() as db:
            seed_case(db)
            resolve_case(db)
            first = Remediation.model_validate(remediation_data(action_id="overdue-action",
                due_at="2026-10-10T00:00:00Z"))
            cases.add_remediation(db, first)
            second = Remediation.model_validate(remediation_data(action_id="submitted-action",
                due_at="2026-10-20T00:00:00Z", status="submitted", evidence_ref="archive://claim"))
            cases.add_remediation(db, second)
            queue = review.remediation_queue(db, at=instant("2026-10-15T00:00:00Z"))
            self.assertEqual([row["action_id"] for row in queue["overdue"]], ["overdue-action"])
            self.assertEqual([row["action_id"] for row in queue["awaiting_verification"]], ["submitted-action"])

    def test_case_source_history_explains_external_and_management_events(self):
        with self.transaction() as db:
            seed_case(db)
            resolve_case(db)
            planned = Remediation.model_validate(remediation_data())
            cases.add_remediation(db, planned)
            verified = Remediation.model_validate(remediation_data(
                evidence_ref="archive://proof", verified_by="auditor-one",
                verified_at="2026-10-19T00:00:00Z", status="verified"))
            cases.update_remediation(db, verified.action_id, verified, 2)
            review.close(db, "case-one", "substantiated", 1)
            view = lineage.explain_event(db, "case", "case-one")
            sources = {row["source_ref"] for row in view["revision_history"]}
            self.assertEqual(sources, {"case-feed", "case-management"})


if __name__ == "__main__":
    unittest.main()
