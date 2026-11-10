import json
from datetime import datetime
from scrapy.exceptions import DropItem

from crawler import canonicalize_url, normalize_scholarship_name
from database import get_connection, init_db
from verification_engine import VerificationEngine


class AtlasVerificationPipeline:

    def open_spider(self, spider):
        """Fires when the crawler starts."""
        init_db()
        self.conn = get_connection()
        self.cursor = self.conn.cursor()
        self.seen_urls = set()
        self.seen_names = set()
        self.discovered_count = 0
        self.updated_count = 0
        self.unchanged_count = 0
        self.stale_count = 0

    def close_spider(self, spider):
        """Fires when the crawler finishes."""
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.cursor.execute("""
            INSERT INTO crawl_runs (run_timestamp, discovered_count, updated_count, unchanged_count, stale_count)
            VALUES (?, ?, ?, ?, ?)
        """, (now_str, self.discovered_count, self.updated_count, self.unchanged_count, self.stale_count))
        self.conn.commit()
        self.conn.close()

    def process_item(self, item, spider):
        """Verify, deduplicate, and persist every discovered scholarship item."""
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        record = dict(item)
        evidence_list = record.pop("evidence_list", [])

        canon_url = canonicalize_url(record.get("official_source_url", ""))
        norm_name = normalize_scholarship_name(record.get("name", ""))
        if canon_url in self.seen_urls or (norm_name and norm_name in self.seen_names):
            raise DropItem(f"Duplicate scholarship dropped: {record.get('name')}")
        if canon_url:
            self.seen_urls.add(canon_url)
        if norm_name:
            self.seen_names.add(norm_name)

        # 1. Verification & Scoring
        score, breakdown, status = VerificationEngine.evaluate(record, evidence_list)

        # 2. Database Lookup & Stable Slug
        slug = record.pop("slug", None) or record["name"].lower().replace(" ", "-")[:50]
        self.cursor.execute("SELECT * FROM scholarships WHERE slug = ?", (slug,))
        existing = self.cursor.fetchone()

        if not existing:
            self.cursor.execute("""
                INSERT INTO scholarships (
                    slug, name, provider, source_type, official_source_url, application_url,
                    amount_benefit, education_level, income_criteria, gender_criteria,
                    category_criteria, domicile_criteria, opening_date, closing_date,
                    selection_process, documents_required, status, confidence_score,
                    confidence_breakdown, last_verified, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                slug, record["name"], record["provider"], record["source_type"],
                record["official_source_url"], record["application_url"],
                record["amount_benefit"], record["education_level"], record["income_criteria"],
                record["gender_criteria"], record["category_criteria"], record["domicile_criteria"],
                record["opening_date"], record["closing_date"], record["selection_process"],
                record["documents_required"], status, score, json.dumps(breakdown), now_str, now_str
            ))
            sch_id = self.cursor.lastrowid
            self.discovered_count += 1

            for ev in evidence_list:
                self.cursor.execute("""
                    INSERT INTO field_evidence (scholarship_id, field_name, extracted_value, source_snippet, verified_at)
                    VALUES (?, ?, ?, ?, ?)
                """, (sch_id, ev["field_name"], ev["extracted_value"], ev["source_snippet"], now_str))

        else:
            sch_id = existing["id"]
            diffs = []
            fields_to_track = ["closing_date", "amount_benefit", "income_criteria", "application_url", "status"]

            for f in fields_to_track:
                old_val = str(existing[f]) if existing[f] is not None else ""
                new_val = str(status if f == "status" else record.get(f, ""))
                if old_val != new_val:
                    diffs.append((f, old_val, new_val))

            if diffs:
                for field_name, old_val, new_val in diffs:
                    ev_snippet = next(
                        (e["source_snippet"] for e in evidence_list if e["field_name"] == field_name),
                        "Live re-crawl difference detected",
                    )
                    self.cursor.execute("""
                        INSERT INTO change_logs (scholarship_id, field_name, old_value, new_value, change_date, evidence)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (sch_id, field_name, old_val, new_val, now_str, ev_snippet))

                self.cursor.execute("""
                    UPDATE scholarships SET
                        amount_benefit = ?, income_criteria = ?, closing_date = ?,
                        application_url = ?, status = ?, confidence_score = ?,
                        confidence_breakdown = ?, last_verified = ?
                    WHERE id = ?
                """, (
                    record["amount_benefit"], record["income_criteria"], record["closing_date"],
                    record["application_url"], status, score, json.dumps(breakdown), now_str, sch_id
                ))
                self.cursor.execute("DELETE FROM field_evidence WHERE scholarship_id = ?", (sch_id,))
                for ev in evidence_list:
                    self.cursor.execute("""
                        INSERT INTO field_evidence (scholarship_id, field_name, extracted_value, source_snippet, verified_at)
                        VALUES (?, ?, ?, ?, ?)
                    """, (sch_id, ev["field_name"], ev["extracted_value"], ev["source_snippet"], now_str))
                self.updated_count += 1
            else:
                self.unchanged_count += 1
                self.cursor.execute("UPDATE scholarships SET last_verified = ? WHERE id = ?", (now_str, sch_id))

        if status == "EXPIRED":
            self.stale_count += 1

        self.conn.commit()
        return item
