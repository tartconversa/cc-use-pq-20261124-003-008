from __future__ import annotations

import unittest

from silverwatch import analysis, ledger, planning, registry
from silverwatch.contracts import NeedBatch, Provider, TouchBatch
from silverwatch.errors import Problem
from tests.support import DatabaseCase, instant, need_data, provider_data, touch_data


class LedgerRevisionTests(DatabaseCase, unittest.TestCase):
    def test_identical_source_revision_is_replayed_without_new_sequence(self):
        with self.transaction() as db:
            first = registry.register_provider(db, Provider.model_validate(provider_data()), "feed-a", 1)
            second = registry.register_provider(db, Provider.model_validate(provider_data()), "feed-a", 1)
            self.assertFalse(first["replayed"])
            self.assertTrue(second["replayed"])
            self.assertEqual(first["seq"], second["seq"])
            self.assertEqual(ledger.current_cut(db), first["seq"])

    def test_same_revision_cannot_change_fact(self):
        with self.transaction() as db:
            registry.register_provider(db, Provider.model_validate(provider_data()), "feed-a", 1)
            changed = Provider.model_validate(provider_data(capacity_per_month=900))
            with self.assertRaises(Problem) as captured:
                registry.register_provider(db, changed, "feed-a", 1)
            self.assertEqual(captured.exception.code, "source_revision_changed")

    def test_older_source_revision_cannot_replace_a_correction(self):
        with self.transaction() as db:
            registry.register_provider(db, Provider.model_validate(provider_data()), "feed-a", 2)
            with self.assertRaises(Problem) as captured:
                registry.register_provider(db, Provider.model_validate(provider_data()), "feed-a", 1)
            self.assertEqual(captured.exception.code, "revision_out_of_order")

    def test_two_sources_resolve_to_the_last_observed_entity_state(self):
        with self.transaction() as db:
            registry.register_provider(db, Provider.model_validate(provider_data(capacity_per_month=700)), "feed-a", 1)
            registry.register_provider(db, Provider.model_validate(provider_data(capacity_per_month=950)), "feed-b", 1)
            self.assertEqual(registry.catalog(db)[0]["capacity_per_month"], 950)
            self.assertEqual(len(ledger.current_entities(db, "provider")), 1)

    def test_cut_keeps_the_previous_provider_state_after_correction(self):
        with self.transaction() as db:
            first = registry.register_provider(db, Provider.model_validate(provider_data(capacity_per_month=700)), "feed-a", 1)
            registry.register_provider(db, Provider.model_validate(provider_data(capacity_per_month=950)), "feed-a", 2)
            self.assertEqual(registry.catalog(db, cut=first["seq"])[0]["capacity_per_month"], 700)
            self.assertEqual(registry.catalog(db)[0]["capacity_per_month"], 950)

    def test_time_cut_omits_a_later_source_observation(self):
        with self.transaction() as db:
            registry.register_provider(db, Provider.model_validate(provider_data(updated_at="2026-10-09T00:00:00Z")), "feed-a", 1)
            registry.register_provider(db, Provider.model_validate(provider_data(capacity_per_month=999,
                                    updated_at="2026-11-05T00:00:00Z")), "feed-a", 2)
            before = ledger.current_entities(db, "provider", as_of=instant("2026-10-31T00:00:00Z"))
            self.assertEqual(before[0]["payload"]["capacity_per_month"], 1200)
            self.assertEqual(registry.catalog(db)[0]["capacity_per_month"], 999)

    def test_history_preserves_corrected_values_and_provenance(self):
        with self.transaction() as db:
            registry.register_provider(db, Provider.model_validate(provider_data()), "feed-a", 1)
            registry.register_provider(db, Provider.model_validate(provider_data(capacity_per_month=800)), "feed-a", 2)
            events = ledger.revisions(db, "provider", "provider-one", source_ref="feed-a")
            self.assertEqual([event["revision"] for event in events], [1, 2])
            self.assertEqual(events[-1]["payload"]["capacity_per_month"], 800)

    def test_integrity_audit_flags_unrecognized_event_contracts(self):
        with self.transaction() as db:
            db.execute("INSERT INTO event(event_type,entity_id,revision,source_ref,digest,payload,observed_at) VALUES(?,?,?,?,?,?,?)",
                       ("mystery", "item-one", 1, "feed", "0" * 64, "{}", "2026-10-01T00:00:00+00:00"))
            result = ledger.audit_integrity(db)
            self.assertFalse(result["valid"])
            self.assertIn("unknown_event_type", {issue["code"] for issue in result["issues"]})


class RegistryAndPlanningTests(DatabaseCase, unittest.TestCase):
    def test_touch_requires_registered_provider(self):
        with self.transaction() as db:
            with self.assertRaises(Problem) as captured:
                registry.register_touch(db, TouchBatch.model_validate(touch_data()))
            self.assertEqual(captured.exception.code, "provider_missing")

    def test_provider_active_window_is_half_open(self):
        with self.transaction() as db:
            body = Provider.model_validate(provider_data(active_from="2026-10-01T00:00:00Z",
                                                         active_until="2026-10-31T00:00:00Z"))
            registry.register_provider(db, body, "feed-a", 1)
            self.assertEqual(registry.provider_capacity(db, region="suburban", service_kind="meal",
                             at=instant("2026-10-30T23:59:59Z")), [registry.catalog(db)[0]])
            self.assertEqual(registry.provider_capacity(db, region="suburban", service_kind="meal",
                             at=instant("2026-10-31T00:00:00Z")), [])

    def test_need_contract_rejects_nonpositive_period(self):
        with self.assertRaises(ValueError):
            NeedBatch.model_validate(need_data(period_end="2026-10-01T00:00:00Z"))

    def test_plan_uses_only_capacity_active_at_requested_time(self):
        with self.transaction() as db:
            registry.register_provider(db, Provider.model_validate(provider_data(capacity_per_month=900,
                active_until="2026-10-15T00:00:00Z")), "feed-a", 1)
            registry.register_need(db, NeedBatch.model_validate(need_data()))
            plan = planning.plan_region(db, region="suburban", period_end=instant("2026-10-31T00:00:00Z"))
            self.assertEqual(plan["totals"]["monthly_capacity"], 0)
            self.assertEqual(plan["totals"]["unserved_estimate"], 1000)

    def test_plan_separates_need_from_missing_capacity(self):
        with self.transaction() as db:
            registry.register_need(db, NeedBatch.model_validate(need_data()))
            plan = planning.plan_region(db, region="suburban", period_end=instant("2026-11-01T00:00:00Z"))
            row = plan["areas"][0]
            self.assertEqual(row["estimated_people"], 1000)
            self.assertEqual(row["capacity_ratio"], "0.00bp")
            self.assertFalse(row["capacity_observed"])
            self.assertFalse(row["capacity_observed"])

    def test_plan_comparison_identifies_new_and_changed_service_area(self):
        before = {"region": "suburban", "cut": 4, "areas": [{"district": "district-a",
                 "service_kind": "meal", "unserved_estimate": 10, "monthly_capacity": 20,
                 "estimated_people": 30}]}
        after = {"region": "suburban", "cut": 7, "areas": [{"district": "district-a",
                "service_kind": "meal", "unserved_estimate": 0, "monthly_capacity": 30,
                "estimated_people": 30}, {"district": "district-b", "service_kind": "transport",
                "unserved_estimate": 5, "monthly_capacity": 0, "estimated_people": 5}]}
        change = planning.compare_plans(before, after)
        self.assertEqual(len(change["changes"]), 2)
        self.assertEqual(change["changes"][0]["unserved_delta"], -10)
        self.assertTrue(change["changes"][1]["appeared"])

    def test_fraction_rate_is_expressed_as_basis_points(self):
        self.assertEqual(analysis.format_rate(__import__("fractions").Fraction(1, 200)), "50.00bp")


if __name__ == "__main__":
    unittest.main()
