from __future__ import annotations

import unittest

from silverwatch import cases, channels, comparison, csvio, registry, trends
from silverwatch.contracts import AnalysisPolicy, Provider
from silverwatch.errors import Problem
from tests.support import DatabaseCase, provider_data, resolve_case, seed_case, touch_data, instant


class CsvAdapterTests(DatabaseCase, unittest.TestCase):
    def test_csv_import_preserves_quoted_commas_and_returns_source_digest(self):
        text = "provider_id,display_name,region,district,service_kind,capacity_per_month,active_from,active_until,status,updated_at\n"
        text += 'provider-one,"Meals, River District",suburban,district-a,meal,1200,2026-01-01T00:00:00Z,,active,2026-10-01T00:00:00Z\n'
        decoded = csvio.decode(text, kind="providers", batch_id="csv-batch",
                               source_ref="csv-source", revision=1)
        self.assertEqual(decoded["record_count"], 1)
        self.assertIn(",", decoded["records"][0]["data"]["display_name"])
        self.assertEqual(len(decoded["source_digest"]), 64)

    def test_csv_duplicate_headers_are_rejected_before_mapping_columns(self):
        text = "provider_id,provider_id\none,two\n"
        with self.assertRaises(Problem) as captured:
            csvio.decode(text, kind="providers", batch_id="csv-batch",
                         source_ref="csv-source", revision=1)
        self.assertEqual(captured.exception.code, "csv_header_duplicate")

    def test_csv_rows_with_too_many_or_too_few_cells_report_the_line(self):
        with self.assertRaises(Problem) as captured:
            csvio.decode("a,b\n1,2,3\n", kind="providers", batch_id="csv-batch",
                         source_ref="csv-source", revision=1)
        self.assertEqual(captured.exception.details["errors"][0]["line"], 2)

    def test_csv_encoder_quotes_values_and_adds_utf8_bom(self):
        text = csvio.encode(["name", "note"], [{"name": "Meals, River", "note": "line one\nline two"}])
        self.assertTrue(text.startswith("\ufeffname,note\n"))
        self.assertIn('"Meals, River"', text)
        self.assertIn('"line one\nline two"', text)

    def test_csv_encoder_refuses_unlisted_columns_instead_of_silently_dropping(self):
        with self.assertRaises(Problem) as captured:
            csvio.encode(["name"], [{"name": "Provider", "private": "unexpected"}])
        self.assertEqual(captured.exception.code, "csv_row_shape")


class RegionalComparisonTests(DatabaseCase, unittest.TestCase):
    def test_comparison_uses_one_cut_and_labels_regions_without_exposure(self):
        with self.transaction() as db:
            registry.register_provider(db, Provider.model_validate(provider_data()), "provider-feed", 1)
            result = comparison.compare_regions(db, regions=["suburban", "rural"],
                as_of=instant("2026-11-10T00:00:00Z"), policy=AnalysisPolicy(policy_id="policy-one"))
            self.assertEqual(result["cut"], registry.source_cut(db)["cut"])
            self.assertEqual(result["regions"][0]["disclosure_state"], "no_exposure")
            self.assertIsNone(result["regions"][0]["confirmed_rate_bp"])

    def test_comparison_rejects_duplicate_regions_and_unsupported_labels(self):
        with self.transaction() as db:
            with self.assertRaises(Problem) as duplicate:
                comparison.compare_regions(db, regions=["urban", "urban"],
                    as_of=instant("2026-11-10T00:00:00Z"), policy=AnalysisPolicy(policy_id="policy-one"))
            self.assertEqual(duplicate.exception.code, "comparison_regions")
            with self.assertRaises(Problem) as unsupported:
                comparison.compare_regions(db, regions=["urban", "overseas"],
                    as_of=instant("2026-11-10T00:00:00Z"), policy=AnalysisPolicy(policy_id="policy-one"))
            self.assertEqual(unsupported.exception.code, "comparison_region")

    def test_only_regions_with_observed_denominators_are_ranked(self):
        payload = {"regions": [{"region": "urban", "confirmed_rate_bp": None},
                               {"region": "rural", "confirmed_rate_bp": 25.0},
                               {"region": "suburban", "confirmed_rate_bp": 50.0}]}
        ranked = comparison.rank_by_confirmed_rate(payload)
        self.assertEqual([row["region"] for row in ranked], ["suburban", "rural"])
        self.assertEqual(ranked[0]["rank"], 1)


class ChannelAnalysisTests(DatabaseCase, unittest.TestCase):
    def test_channel_denominators_keep_outcomes_with_their_touch_batch(self):
        with self.transaction() as db:
            seed_case(db, touch_observed_at="2026-10-01T00:00:00Z")
            phone = touch_data(batch_id="phone-one", campaign_id="phone-campaign",
                channel="phone", exposed_people=200, nonresponse_people=0, report_count=0,
                completed_services=100, source_ref="phone-feed",
                observed_at="2026-10-01T00:00:00Z")
            from silverwatch.contracts import TouchBatch
            registry.register_touch(db, TouchBatch.model_validate(phone))
            resolve_case(db)
            report = channels.compare_channels(db, region="suburban",
                as_of=instant("2026-11-02T00:00:00Z"), cut=registry.source_cut(db)["cut"])
            areas = {row["channel"]: row for row in report["channels"]}
            self.assertEqual(areas["community"]["substantiated"], 1)
            self.assertEqual(areas["phone"]["substantiated"], 0)
            self.assertEqual(areas["community"]["exposed"], 200)
            self.assertEqual(areas["phone"]["exposed"], 200)
            self.assertEqual(channels.rate_differences(report)[0]["difference_bp"], 50.0)

    def test_channel_with_incomplete_coverage_is_not_ranked_as_zero_risk(self):
        with self.transaction() as db:
            seed_case(db, coverage_complete=False, nonresponse_people=0,
                      touch_observed_at="2026-10-01T00:00:00Z")
            resolve_case(db)
            report = channels.compare_channels(db, region="suburban",
                as_of=instant("2026-11-02T00:00:00Z"), cut=registry.source_cut(db)["cut"])
            row = report["channels"][0]
            self.assertFalse(row["eligible"])
            self.assertIn("coverage_unknown", row["gaps"])
            self.assertEqual(channels.rate_differences(report), [])


class TrendReplayTests(DatabaseCase, unittest.TestCase):
    def test_same_area_trend_distinguishes_pending_from_confirmed_cases(self):
        with self.transaction() as db:
            seed_case(db, touch_observed_at="2026-10-01T00:00:00Z")
            first_cut = registry.source_cut(db)["cut"]
            resolve_case(db)
            result = trends.build(db, region="suburban", periods=[
                {"label": "reported", "cut": first_cut, "as_of": "2026-10-11T12:00:00Z"},
                {"label": "reviewed", "cut": registry.source_cut(db)["cut"],
                 "as_of": "2026-10-13T00:00:00Z"}],
                policy=AnalysisPolicy(policy_id="policy-one"))
            points = result["series"][0]["points"]
            self.assertEqual(points[0]["pending"], 1)
            self.assertEqual(points[0]["substantiated"], 0)
            self.assertEqual(points[1]["pending"], 0)
            self.assertEqual(points[1]["substantiated"], 1)
            self.assertIsNotNone(points[1]["rate_delta_bp"])

    def test_trend_requires_increasing_dates_and_non_decreasing_sequence_cuts(self):
        with self.transaction() as db:
            seed_case(db)
            cut = registry.source_cut(db)["cut"]
            periods = [{"label": "later", "cut": cut, "as_of": "2026-11-12T00:00:00Z"},
                       {"label": "earlier", "cut": cut, "as_of": "2026-11-11T00:00:00Z"}]
            with self.assertRaises(Problem) as captured:
                trends.build(db, region="suburban", periods=periods,
                             policy=AnalysisPolicy(policy_id="policy-one"))
            self.assertEqual(captured.exception.code, "trend_order")

    def test_period_replay_exposes_areas_absent_from_earlier_cut(self):
        with self.transaction() as db:
            first_cut = registry.source_cut(db)["cut"]
            seed_case(db, touch_observed_at="2026-10-01T00:00:00Z")
            later_cut = registry.source_cut(db)["cut"]
            result = trends.build(db, region="suburban", periods=[
                {"label": "empty", "cut": first_cut, "as_of": "2026-10-15T00:00:00Z"},
                {"label": "observed", "cut": later_cut, "as_of": "2026-11-02T00:00:00Z"}],
                policy=AnalysisPolicy(policy_id="policy-one"))
            self.assertFalse(result["series"][0]["points"][0]["present"])
            self.assertTrue(result["series"][0]["points"][1]["present"])


if __name__ == "__main__":
    unittest.main()
