"""Offline checks for the synthesis agent's deterministic contract."""

import unittest

from synthesis_dev.agent import calculate_trends, validate_output


def metric(identifier, year, concept, value, unit="USD"):
    return {
        "id": identifier, "year": year, "concept": concept,
        "label": concept, "value": value, "unit": unit,
        "period_type": "FY", "period_start_date": f"{year}-01-01",
        "period_end_date": f"{year}-12-31",
    }


class StandaloneSynthesisTests(unittest.TestCase):
    def request(self, years=(2023, 2024, 2025)):
        metrics = []
        for year, revenue in zip(years, (100, 120, 150)):
            metrics.extend([
                metric(f"r{year}", year, "Revenue", revenue),
                metric(f"p{year}", year, "OperatingIncomeLoss", revenue * .2),
                metric(f"a{year}", year, "AssetsCurrent", 80),
                metric(f"l{year}", year, "LiabilitiesCurrent", 40),
            ])
        return {
            "company": "GENERIC",
            "years": list(years),
            "material_changes": [{"id": "change-1"}, {"id": "change-2"}],
            "financial_metrics": metrics,
            "finding_target": 2,
        }

    def test_non_xbrl_labels_and_multi_year_trends(self):
        result = calculate_trends({"request": self.request()})
        self.assertTrue(any(t.get("concept") == "Revenue" for t in result["trends"]))
        self.assertTrue(any(t.get("name") == "Operating margin" for t in result["trends"]))
        self.assertTrue(any(t.get("name") == "Current ratio" for t in result["trends"]))
        self.assertTrue(all(t.get("source_values") for t in result["trends"]))
        self.assertTrue(all(t.get("formula") for t in result["trends"]))

    def test_two_year_comparison(self):
        result = calculate_trends({"request": self.request((2024, 2025))})
        self.assertTrue(any(t.get("from_year") == 2024 and t.get("to_year") == 2025
                            for t in result["trends"]))

    def test_validation_requires_complete_disclosure_coverage(self):
        state = {"evidence": self.request(), "trends": [], "report": {
            "executive_summary": "summary", "longitudinal_analysis": "analysis",
            "financial_analysis": "financial", "limitations": [],
            "key_findings": [{"title": "one", "analysis": "detail",
                               "evidence_ids": ["change-1"]}],
        }}
        result = validate_output(state)["validation"]
        self.assertFalse(result["valid"])
        self.assertEqual(result["uncited_material_change_ids"], ["change-2"])


if __name__ == "__main__":
    unittest.main()
