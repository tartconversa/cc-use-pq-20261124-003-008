from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from silverwatch import intake, registry
from silverwatch.api import create_app
from silverwatch.errors import Problem
from silverwatch.ledger import current_cut
from silverwatch.storage import Store
from tests.support import DatabaseCase, need_data, provider_data, touch_data


def bundle_data(**updates):
    body = {"batch_id": "bundle-one", "source_ref": "regional-feed", "revision": 1,
            "records": [{"kind": "providers", "data": provider_data()},
                        {"kind": "needs", "data": need_data()},
                        {"kind": "touches", "data": touch_data()}]}
    return body | updates


class BundleImportTests(DatabaseCase, unittest.TestCase):
    def test_bundle_validation_reports_record_counts_without_writing(self):
        with self.transaction() as db:
            result = intake.validate_bundle(bundle_data())
            self.assertTrue(result["valid"])
            self.assertEqual(result["record_count"], 3)
            self.assertEqual(result["counts"]["providers"], 1)
            self.assertEqual(current_cut(db), 0)

    def test_preview_simulates_cross_record_writes_then_rolls_them_back(self):
        with self.transaction() as db:
            preview = intake.preview_import(db, bundle_data())
            self.assertTrue(preview["ready"])
            self.assertTrue(preview["would_write"])
            self.assertEqual(preview["would_change_cut"], 3)
            self.assertEqual(current_cut(db), 0)
            imported = intake.import_bundle(db, bundle_data())
            self.assertEqual(imported["records"][0]["seq"], 1)

    def test_preview_surfaces_missing_provider_dependency_without_partial_state(self):
        with self.transaction() as db:
            body = bundle_data()
            body["records"][2]["data"]["provider_id"] = "unknown-provider"
            preview = intake.preview_import(db, body)
            self.assertFalse(preview["ready"])
            self.assertEqual(preview["error"]["code"], "bundle_rejected")
            self.assertEqual(current_cut(db), 0)

    def test_preview_of_an_already_imported_bundle_predicts_replay(self):
        with self.transaction() as db:
            intake.import_bundle(db, bundle_data())
            before = current_cut(db)
            preview = intake.preview_import(db, bundle_data())
            self.assertTrue(preview["ready"])
            self.assertFalse(preview["would_write"])
            self.assertEqual(preview["resulting_cut"], before)
            self.assertEqual(current_cut(db), before)

    def test_bundle_is_committed_as_one_unit_and_returns_cut_interval(self):
        with self.transaction() as db:
            result = intake.import_bundle(db, bundle_data())
            self.assertEqual(result["accepted_count"], 3)
            self.assertEqual(result["start_cut"], 0)
            self.assertEqual(result["end_cut"], 3)
            self.assertEqual(len(registry.catalog(db)), 1)
            self.assertEqual(len(registry.touch_facts(db)), 1)

    def test_replaying_a_whole_bundle_reports_replayed_records(self):
        with self.transaction() as db:
            first = intake.import_bundle(db, bundle_data())
            replay = intake.import_bundle(db, bundle_data())
            self.assertEqual(first["end_cut"], replay["end_cut"])
            self.assertEqual(replay["replayed_count"], 3)
            self.assertEqual(current_cut(db), 3)

    def test_invalid_record_rejects_every_write_in_bundle(self):
        with self.assertRaises(Problem) as captured:
            with self.transaction() as db:
                body = bundle_data()
                body["records"][2]["data"]["provider_id"] = "missing-provider"
                intake.import_bundle(db, body)
        self.assertEqual(captured.exception.code, "bundle_rejected")
        with self.transaction(write=False) as db:
            self.assertEqual(current_cut(db), 0)

    def test_duplicate_entity_inside_same_bundle_is_rejected_before_double_apply(self):
        with self.assertRaises(Problem) as captured:
            with self.transaction() as db:
                body = bundle_data()
                body["records"].append(body["records"][0])
                intake.import_bundle(db, body)
        self.assertEqual(captured.exception.details["failures"][0]["code"], "duplicate_in_bundle")

    def test_bundle_refuses_unbounded_record_count(self):
        body = bundle_data(records=[{"kind": "needs", "data": need_data()}] * 501)
        with self.assertRaises(Problem) as captured:
            with self.transaction() as db:
                intake.import_bundle(db, body)
        self.assertEqual(captured.exception.code, "bundle_limit")

    def test_validate_endpoint_returns_contract_errors_without_mutating_ledger(self):
        with self.transaction() as db:
            body = bundle_data()
            body["records"][1]["data"]["estimated_people"] = -1
            response = intake.validate_bundle(body)
            self.assertFalse(response["valid"])
            self.assertEqual(response["errors"][0]["position"], 1)
            self.assertEqual(current_cut(db), 0)


class HttpWorkflowTests(DatabaseCase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.client = TestClient(create_app(self.store.location))

    def test_preview_route_checks_relationships_without_persisting_the_batch(self):
        response = self.client.post("/imports/preview", json=bundle_data())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ready"])
        with self.transaction(write=False) as db:
            self.assertEqual(current_cut(db), 0)

    def test_health_endpoint_reports_service_ready(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok", "database": "ready"})

    def test_provider_write_and_profile_read_share_the_committed_event(self):
        response = self.client.post("/providers?source_ref=feed&revision=1", json=provider_data())
        self.assertEqual(response.status_code, 200)
        profile = self.client.get("/providers/provider-one")
        self.assertEqual(profile.status_code, 200)
        self.assertEqual(profile.json()["provider"]["display_name"], "Community Meals")

    def test_wrong_case_path_is_rejected_before_any_event_is_added(self):
        response = self.client.post("/cases/path-case/evidence", json={
            "evidence_id": "ev-one", "case_id": "body-case", "evidence_type": "recording",
            "source_ref": "archive://x", "content_digest": "a" * 64,
            "observed_at": "2026-10-11T10:00:00Z", "verified": True})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "case_scope")
        with self.transaction(write=False) as db:
            self.assertEqual(current_cut(db), 0)

    def test_invalid_export_format_returns_domain_error(self):
        response = self.client.get("/bulletins/analysis/export?format=xml")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "export_format")

    def test_audit_routes_return_machine_readable_empty_state(self):
        response = self.client.get("/audit/reconciliation")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["complete"])
        reference = self.client.get("/audit/references")
        self.assertTrue(reference.json()["valid"])

    def test_service_plan_route_keeps_region_and_period_in_result(self):
        self.client.post("/providers?source_ref=feed&revision=1", json=provider_data())
        response = self.client.get("/regions/suburban/service-plan?period_end=2026-11-01T00:00:00Z")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["region"], "suburban")

    def test_error_response_does_not_expose_python_traceback(self):
        response = self.client.get("/cases/unknown-case")
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("Traceback", response.text)


if __name__ == "__main__":
    unittest.main()
