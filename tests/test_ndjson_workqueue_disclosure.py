from __future__ import annotations

import json
import unittest

from silverwatch import ndjson, workqueue
from silverwatch.errors import Problem
from silverwatch.exports import public_view
from silverwatch.contracts import CaseEvidence, ReviewDecision
from tests.support import (DatabaseCase, decision_data, evidence_data, provider_data,
                          resolve_case, seed_case, instant)


class NdjsonContractTests(unittest.TestCase):
    def make_text(self, *records):
        return "\n".join(json.dumps(record) for record in records)

    def test_valid_stream_is_canonical_and_digest_can_be_rechecked(self):
        text = self.make_text({"kind": "providers", "data": provider_data()})
        result = ndjson.decode(text, batch_id="batch-one", source_ref="feed-one", revision=1)
        self.assertEqual(result["record_count"], 1)
        self.assertEqual(result["line_count"], 1)
        self.assertTrue(ndjson.verify_digest(result))

    def test_utf8_bom_and_blank_lines_do_not_change_record_positions(self):
        text = "\ufeff\n" + self.make_text({"kind": "providers", "data": provider_data()}) + "\n\n"
        result = ndjson.decode(text, batch_id="batch-one", source_ref="feed-one", revision=1)
        self.assertEqual(result["record_count"], 1)
        self.assertEqual(result["line_count"], 3)

    def test_duplicate_object_keys_are_rejected_as_ambiguous(self):
        text = '{"kind":"providers","kind":"needs","data":{}}'
        with self.assertRaises(Problem) as captured:
            ndjson.decode(text, batch_id="batch-one", source_ref="feed-one", revision=1)
        self.assertEqual(captured.exception.code, "ndjson_parse")

    def test_malformed_rows_report_physical_line_number(self):
        text = self.make_text({"kind": "providers", "data": provider_data()}) + "\n{broken"
        with self.assertRaises(Problem) as captured:
            ndjson.decode(text, batch_id="batch-one", source_ref="feed-one", revision=1)
        self.assertEqual(captured.exception.details["errors"][0]["line"], 2)

    def test_contract_failure_maps_back_to_source_line(self):
        invalid = provider_data(capacity_per_month=-1)
        text = self.make_text({"kind": "providers", "data": invalid})
        with self.assertRaises(Problem) as captured:
            ndjson.decode(text, batch_id="batch-one", source_ref="feed-one", revision=1)
        self.assertEqual(captured.exception.code, "ndjson_contract")
        self.assertEqual(captured.exception.details["errors"][0]["line"], 1)

    def test_stream_size_and_record_limits_prevent_unbounded_imports(self):
        with self.assertRaises(Problem) as captured:
            ndjson.decode("x" * (ndjson.MAX_BYTES + 1), batch_id="b", source_ref="s", revision=1)
        self.assertEqual(captured.exception.code, "ndjson_size")
        rows = [{"kind": "providers", "data": provider_data(provider_id=f"p-{index}")}
                for index in range(ndjson.MAX_RECORDS + 1)]
        with self.assertRaises(Problem) as count_error:
            ndjson.decode(ndjson.encode(rows), batch_id="b", source_ref="s", revision=1)
        self.assertEqual(count_error.exception.code, "ndjson_records")

    def test_encoder_rejects_non_contract_envelopes(self):
        with self.assertRaises(Problem) as captured:
            ndjson.encode([{"type": "providers"}])
        self.assertEqual(captured.exception.code, "record_envelope")


class OperationalQueueTests(DatabaseCase, unittest.TestCase):
    def test_investigation_queue_explains_missing_roles_and_material(self):
        with self.transaction() as db:
            seed_case(db)
            queue = workqueue.investigation_queue(db, as_of=instant("2026-10-15T00:00:00Z"))
            row = queue["items"][0]
            self.assertEqual(row["finding"], "pending")
            self.assertEqual(row["missing_roles"], ["compliance", "investigator"])
            self.assertIn("review_incomplete", row["reasons"])

    def test_investigation_queue_orders_critical_work_before_low_severity(self):
        with self.transaction() as db:
            seed_case(db, severity="low")
            extra = json.loads(json.dumps({"case_id": "critical-case", "provider_id": "provider-one",
                "batch_id": "touch-one", "kind": "suspected_fraud", "severity": "critical",
                "reported_at": "2026-10-12T00:00:00Z", "allegation": "A suspicious payment request was reported.",
                "source_ref": "hotline-two", "affected_people": 1, "status": "open"}))
            from silverwatch.contracts import Case
            from silverwatch import cases
            cases.create(db, Case.model_validate(extra), "intake-two", 1)
            queue = workqueue.investigation_queue(db, as_of=instant("2026-10-15T00:00:00Z"))
            self.assertEqual(queue["items"][0]["case_id"], "critical-case")

    def test_reviewed_case_leaves_investigation_queue_after_close(self):
        with self.transaction() as db:
            seed_case(db)
            resolve_case(db)
            from silverwatch import cases, review
            from silverwatch.contracts import Remediation
            action = Remediation.model_validate({"action_id": "fix-one", "case_id": "case-one",
                "provider_id": "provider-one", "owner": "owner-one", "due_at": "2026-10-20T00:00:00Z",
                "action": "Replace the notice.", "evidence_ref": "archive://proof",
                "verified_by": "auditor-one", "verified_at": "2026-10-19T00:00:00Z", "status": "verified"})
            cases.add_remediation(db, action)
            review.close(db, "case-one", "substantiated", 1)
            queue = workqueue.investigation_queue(db, as_of=instant("2026-11-25T00:00:00Z"))
            self.assertEqual(queue["count"], 0)

    def test_remediation_queue_marks_overdue_and_verified_status_separately(self):
        with self.transaction() as db:
            seed_case(db)
            resolve_case(db)
            from silverwatch import cases
            from silverwatch.contracts import Remediation
            open_action = Remediation.model_validate({"action_id": "open-fix", "case_id": "case-one",
                "provider_id": "provider-one", "owner": "owner-one", "due_at": "2026-10-12T00:00:00Z",
                "action": "Correct the notice.", "status": "planned"})
            cases.add_remediation(db, open_action)
            queue = workqueue.remediation_queue(db, as_of=instant("2026-10-15T00:00:00Z"))
            self.assertEqual(queue["items"][0]["queue_state"], "overdue")
            self.assertEqual(queue["items"][0]["days_to_due"], -3)


class DisclosureTests(unittest.TestCase):
    def test_small_cohort_suppresses_counts_and_rates_but_keeps_gap_reason(self):
        row = {"district": "district-a", "service_kind": "meal", "cohort": "80_plus",
               "exposed": 12, "nonresponse": 3, "uncertainty_people": 3, "reports": 2,
               "substantiated": 1, "pending": 1, "inconclusive": 0, "verified_remediations": 0,
               "substantiated_rate": "833.33bp", "potential_rate_bounds": {"low": "666.67bp", "high": "2666.67bp"},
               "reconciliation_incomplete_batches": 1, "batches": ["batch-private"],
               "capacity_providers": ["provider-private"], "eligible": False,
               "gaps": ["case_intake_incomplete"]}
        bulletin = {"edition": 1, "document": {"policy": {"minimum_exposure": 20},
                    "areas": [row], "risk_flags": [dict(row)], "supply": {},
                    "case_counts": {"substantiated": 1, "pending": 1, "inconclusive": 0}}}
        view = public_view(bulletin)
        published = view["document"]["areas"][0]
        self.assertIsNone(published["substantiated"])
        self.assertIsNone(published["substantiated_rate"])
        self.assertEqual(published["gaps"], ["case_intake_incomplete"])
        self.assertNotIn("batch-private", str(view))
        self.assertNotIn("provider-private", str(view))
        self.assertEqual(view["document"]["case_counts"]["substantiated"], None)

    def test_large_group_keeps_aggregates_but_removes_source_identifiers(self):
        bulletin = {"edition": 1, "document": {"policy": {"minimum_exposure": 20},
                    "areas": [{"district": "district-a", "service_kind": "meal", "cohort": "70_79",
                               "exposed": 200, "reports": 3, "batches": ["internal-batch"],
                               "capacity_providers": ["internal-provider"]}],
                    "risk_flags": [], "supply": {"meal": {"estimated_people": 100,
                    "monthly_capacity": 80, "capacity_ratio": "8000bp",
                    "capacity_providers": ["internal-provider"]}},
                    "case_counts": {"substantiated": 2, "pending": 1, "inconclusive": 0}}}
        view = public_view(bulletin)
        self.assertEqual(view["document"]["areas"][0]["exposed"], 200)
        self.assertNotIn("batches", view["document"]["areas"][0])
        self.assertNotIn("internal-provider", str(view))


if __name__ == "__main__":
    unittest.main()
