import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

# Ensure Atlas_Engine root is importable
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import api
import crawler
import database
from verification_engine import VerificationEngine, normalize_date_str


class TestDateNormalizationAndVerification(unittest.TestCase):
    """Isolated unit tests for date normalization, domain trust, and confidence scoring."""

    def test_normalize_date_formats(self):
        self.assertEqual(normalize_date_str("30-11-2026"), "2026-11-30")
        self.assertEqual(normalize_date_str("07/12/2026"), "2026-12-07")
        self.assertEqual(normalize_date_str("2026-11-15"), "2026-11-15")
        self.assertEqual(normalize_date_str("7th December 2026"), "2026-12-07")
        self.assertIsNone(normalize_date_str("Not specified"))
        self.assertIsNone(normalize_date_str("Closed (2026 cycle)"))

    def test_rejects_social_and_invalid_urls(self):
        ok, _ = VerificationEngine.is_official_domain("https://www.facebook.com/UniversityGrantsCommission", "UGC")
        self.assertFalse(ok)
        ok_gov, _ = VerificationEngine.is_official_domain("https://scholarships.gov.in/public/schemeGuidelines/AICTE_2010_G.pdf", "AICTE")
        self.assertTrue(ok_gov)
        ok_inst, _ = VerificationEngine.is_official_domain("https://www.iisc.ac.in/admissions/fees-and-scholarships/", "IISc")
        self.assertTrue(ok_inst)

    def test_rejects_placeholder_citations_in_scoring(self):
        item = {
            "name": "Test Fixture Scholarship",
            "provider": "Test Govt Agency",
            "official_source_url": "https://example.gov.in/scheme",
            "application_url": "https://example.gov.in/apply",
            "amount_benefit": "Rs. 50,000 per annum",
            "education_level": "Undergraduate / Degree",
            "income_criteria": "Not exceeding Rs. 8 lakh",
            "closing_date": "2026-12-30",
            "selection_process": "Merit list",
        }
        fake_evidence = [
            {"field_name": "closing_date", "extracted_value": "2026-12-30", "source_snippet": "Default standard academic cycle deadline inferred."},
            {"field_name": "amount_benefit", "extracted_value": "Rs. 50,000", "source_snippet": "Fallback due to timeout"},
        ]
        score, breakdown, status = VerificationEngine.evaluate(item, fake_evidence)
        self.assertEqual(breakdown["evidence_traceability"]["points"], 0.0)
        self.assertEqual(status, "REVIEW_REQUIRED")
        self.assertLess(score, 95.0)

    def test_expired_and_closed_scholarships(self):
        past_item = {
            "name": "Test Fixture Past Scholarship",
            "provider": "AICTE",
            "official_source_url": "https://scholarships.gov.in/past",
            "application_url": "https://scholarships.gov.in/apply",
            "amount_benefit": "Rs. 50,000 per annum",
            "education_level": "Undergraduate / Degree",
            "closing_date": "2024-10-31",
            "selection_process": "Merit",
        }
        _, _, status_past = VerificationEngine.evaluate(past_item, [])
        self.assertEqual(status_past, "EXPIRED")

        closed_text_item = dict(past_item, closing_date="Closed (2026 cycle)", is_closed=True)
        _, _, status_closed = VerificationEngine.evaluate(closed_text_item, [])
        self.assertEqual(status_closed, "EXPIRED")


class TestExtractionDeduplicationAndValidation(unittest.TestCase):
    """Isolated unit tests using synthetic fixtures clearly labeled as test fixtures."""

    def test_sentence_split_preserves_rs_and_decimals(self):
        text = "1) Family income should not exceed Rs. 4.50 lakh per annum. 2) An amount of Rs. 20,000/- per annum is awarded."
        sents = crawler.split_sentences(text)
        self.assertEqual(len(sents), 2)
        self.assertIn("Rs. 4.50 lakh", sents[0])
        self.assertIn("Rs. 20,000/- per annum", sents[1])

    def test_does_not_confuse_income_or_application_fee_with_scholarship_amount(self):
        fixture_text = (
            "Test Fixture University Portal. Online Application Fees: Rs. 800 for General candidates. "
            "Eligibility: Candidates whose parental annual income does not exceed Rs. 2,50,000 per annum may apply. "
            "Rate of Scholarship: An amount of Rs. 25,000/- per annum will be awarded to selected UG students. "
            "Selection is based on academic merit in Class XII."
        )
        meta = {
            "url": "https://test-univ.ac.in/scholarships/merit",
            "inferred_name": "Test Fixture Merit Scholarship",
            "provider": "Test Fixture University",
            "source_type": "University",
        }
        record, evidence = crawler.extract_scholarship_record(meta, fixture_text, page_title="Test Fixture Merit Scholarship")
        self.assertEqual(record["amount_benefit"], "Rs. 25,000/- per annum")
        self.assertIn("2,50,000", record["income_criteria"])
        self.assertEqual(record["closing_date"], "Not specified")  # Never inferred!
        ev_fields = [e["field_name"] for e in evidence]
        self.assertEqual(len(ev_fields), len(set(ev_fields)))  # Unique keys for UI
        self.assertIn("source_citation", ev_fields)

    def test_rejects_invalid_and_404_pages(self):
        ok_404, _ = crawler.is_valid_scholarship_page("https://example.ac.in/404", "404 Page Not Found", "Sorry page not found " * 20)
        self.assertFalse(ok_404)
        ok_fb, _ = crawler.is_valid_scholarship_page("https://www.facebook.com/UGC", "UGC", "University Grants Commission scholarship " * 20)
        self.assertFalse(ok_fb)
        ok_valid, _ = crawler.is_valid_scholarship_page(
            "https://example.gov.in/fellowship",
            "National Fellowship",
            "This scholarship fellowship provides Rs. 35,000 per month to PhD scholars based on academic merit and research proposal. " * 4,
        )
        self.assertTrue(ok_valid)
        self.assertFalse(crawler.has_verifiable_scholarship_core({
            "amount_benefit": "Not specified",
            "closing_date": "Not specified",
            "income_criteria": "Not specified",
        }))
        self.assertTrue(crawler.has_verifiable_scholarship_core({
            "amount_benefit": "Rs. 50,000 per annum",
            "closing_date": "Not specified",
            "income_criteria": "Not specified",
        }))

    def test_deduplication_across_urls_and_names(self):
        self.assertEqual(
            crawler.canonicalize_url("https://www.jntataendowment.org/"),
            crawler.canonicalize_url("https://jntataendowment.org"),
        )
        self.assertEqual(
            crawler.normalize_scholarship_name("The Jamsetjee Nusserwanjee Tata Endowment"),
            crawler.normalize_scholarship_name("The J N Tata Endowment"),
        )

    def test_extract_pdf_text_uses_pypdf_fallback_when_pdftotext_missing(self):
        # Minimal valid PDF bytes containing simple text "Test Fixture Scholarship"
        from unittest.mock import MagicMock
        fake_page = MagicMock()
        fake_page.extract_text.return_value = "Test Fixture Scholarship Rs. 50,000 per annum"
        fake_reader = MagicMock()
        fake_reader.pages = [fake_page]
        with patch("crawler.shutil.which", return_value=None), \
             patch("pypdf.PdfReader", return_value=fake_reader):
            extracted = crawler.extract_pdf_text(b"%PDF-1.4\n%fake-fixture")
            self.assertIn("Test Fixture Scholarship", extracted)


class TestPipeline22RecordsAndApiSerialization(unittest.TestCase):
    """Test full pipeline (22+ records, partial failures, DB persistence, and FastAPI serialization) with isolated test fixtures."""

    def test_retrieves_22_unique_records_under_partial_failures_and_serializes_api(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test_scholarships.db")

            # Build 26 synthetic test-fixture candidates (24 distinct + 2 duplicates + 2 failing URLs)
            candidates = []
            for i in range(1, 25):
                candidates.append({
                    "url": f"https://fixture{i}.gov.in/scheme/{i}",
                    "inferred_name": f"Test Fixture National Scholarship {i}",
                    "source_type": "Government",
                    "provider": f"Ministry Fixture {i}",
                    "application_url": f"https://fixture{i}.gov.in/apply/{i}",
                    "opening_date": "2026-06-01",
                    "closing_date": "2026-12-20",
                    "opening_snippet": "Scheme Open from : 01-06-2026",
                    "closing_snippet": "Student Application Open till : 20-12-2026",
                    "is_closed": False,
                    "is_pdf": False,
                    "origin": "nsp",
                })
            # Inject 2 duplicates and 2 failing targets
            candidates.append(dict(candidates[0], url="https://www.fixture1.gov.in/scheme/1/"))
            candidates.append(dict(candidates[1], inferred_name="The Test Fixture National Scholarship 2"))
            candidates.append({
                "url": "https://broken-fixture.gov.in/fail",
                "inferred_name": "Broken Fixture Scholarship",
                "source_type": "Government",
                "provider": "Broken Ministry",
                "is_pdf": False,
                "origin": "direct",
            })

            def fake_fetch(self_crawler, meta):
                if "broken-fixture" in meta["url"]:
                    return "", "", "HTTP 500"
                html = (
                    f"<html><head><title>{meta['inferred_name']}</title></head><body><main>"
                    f"<h1>{meta['inferred_name']}</h1>"
                    f"<p>Amount of Scholarship: Rs. {10000 + len(meta['url']) * 100}/- per annum is awarded to eligible undergraduate students.</p>"
                    f"<p>Eligibility: Annual family income should not exceed Rs. 6 lakh per annum.</p>"
                    f"<p>Selection Process: Candidates are selected purely on academic merit list.</p>"
                    f"</main></body></html>"
                )
                return html, "", ""

            with patch.object(database, "DB_NAME", db_path), \
                 patch.object(crawler.ScholarshipDiscoveryEngine, "discover_urls", return_value=candidates), \
                 patch.object(crawler.LiveWebCrawler, "_fetch_target_content", fake_fetch):
                c = crawler.LiveWebCrawler(target=22)
                c.run()

                rows = api.get_scholarships()
                self.assertEqual(len(rows), 22)
                self.assertEqual(len({r["url"] for r in rows}), 22)
                self.assertEqual(len({r["name"] for r in rows}), 22)
                for row in rows:
                    self.assertEqual(row["status"], "ACTIVE")
                    self.assertGreaterEqual(row["score"], 95.0)
                    ev_fields = {e["field"] for e in row["evidence"]}
                    self.assertIn("source_citation", ev_fields)
                    self.assertIn("amount_benefit", ev_fields)
                    self.assertIn("closing_date", ev_fields)


if __name__ == "__main__":
    unittest.main()
