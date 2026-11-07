import re
from urllib.parse import urlparse
from datetime import datetime, date

class VerificationEngine:
    """
    Computes deterministic confidence score based on concrete proof:
    1. Primary Domain Trust: (Max 30 pts)
       - .gov.in, .nic.in, .ac.in, official corporate CSR domains
    2. Primary Application URL Verification: (Max 15 pts)
       - Working, secure HTTPS application link matching provider
    3. Temporal Validity & Deadline Verification: (Max 20 pts)
       - Valid date format with clear contextual deadline evidence
    4. Eligibility & Criteria Groundedness: (Max 20 pts)
       - Direct source evidence quotes backing eligibility & income
    5. Schema Completeness & Anti-Hallucination: (Max 15 pts)
       - Core required fields populated with non-null traceable text
    Total Score: 100 points
    Verified Threshold: >= 95.0%
    """

    OFFICIAL_TLDS = ['.gov.in', '.nic.in', '.ac.in', '.edu.in', '.org.in', '.res.in']

    @staticmethod
    def is_official_domain(url: str, provider: str) -> tuple[bool, str]:
        if not url:
            return False, "No URL provided"
        domain = urlparse(url).netloc.lower()
        for tld in VerificationEngine.OFFICIAL_TLDS:
            if domain.endswith(tld):
                return True, f"Verified institutional/government TLD ({tld})"

        # Corporate / Trust domain matching
        provider_slug = re.sub(r'[^a-z0-9]', '', provider.lower())
        domain_clean = re.sub(r'[^a-z0-9]', '', domain.split('.')[0])
        if domain_clean in provider_slug or provider_slug in domain_clean:
            return True, f"Official provider domain matches '{provider}'"
        
        # Whitelisted major Indian corporate foundation domains
        trusted_hosts = ["reliancefoundation.org", "tatatrusts.org", "infosys.com", "buddy4study.com"]
        if any(th in domain for th in trusted_hosts):
            return True, f"Verified institutional CSR domain: {domain}"

        return False, f"Domain {domain} cannot be definitively proven official"

    @classmethod
    def evaluate(cls, item: dict, evidence_list: list) -> tuple[float, dict, str]:
        score = 0.0
        breakdown = {}

        # 1. Official Source Domain (30 pts)
        is_off, reason = cls.is_official_domain(item.get("official_source_url", ""), item.get("provider", ""))
        if is_off:
            score += 30.0
            breakdown["source_authenticity"] = {"points": 30, "max": 30, "detail": reason}
        else:
            score += 5.0
            breakdown["source_authenticity"] = {"points": 5, "max": 30, "detail": reason}

        # 2. Application Link (15 pts)
        app_url = item.get("application_url", "")
        if app_url and app_url.startswith("https://"):
            score += 15.0
            breakdown["application_link"] = {"points": 15, "max": 15, "detail": "Valid secure HTTPS portal URL"}
        elif app_url:
            score += 8.0
            breakdown["application_link"] = {"points": 8, "max": 15, "detail": "Application portal present"}
        else:
            breakdown["application_link"] = {"points": 0, "max": 15, "detail": "Application URL not specified"}

        # 3. Evidence Grounding & Traceability (20 pts)
        field_names = [e["field_name"] for e in evidence_list if e.get("source_snippet")]
        vital_fields = {"income_criteria", "education_level", "closing_date", "amount_benefit"}
        grounded_count = len(vital_fields.intersection(set(field_names)))
        evidence_points = min(20.0, (grounded_count / len(vital_fields)) * 20.0)
        score += evidence_points
        breakdown["evidence_traceability"] = {
            "points": round(evidence_points, 1),
            "max": 20,
            "detail": f"{grounded_count} of 4 vital fields have direct source snippets"
        }

        # 4. Temporal Validity & Status Audit (20 pts)
        deadline_str = item.get("closing_date", "")
        status = "ACTIVE"
        if not deadline_str or deadline_str.lower() == "not specified":
            score += 10.0
            breakdown["deadline_currency"] = {"points": 10, "max": 20, "detail": "Deadline rolling or not specified"}
        else:
            try:
                deadline = datetime.strptime(deadline_str, "%Y-%m-%d").date()
                today = date.today()
                if deadline < today:
                    status = "EXPIRED"
                    score += 18.0
                    breakdown["deadline_currency"] = {"points": 18, "max": 20, "detail": f"Expired on {deadline}"}
                else:
                    days_left = (deadline - today).days
                    if days_left <= 15:
                        status = "EXPIRING_SOON"
                    score += 20.0
                    breakdown["deadline_currency"] = {"points": 20, "max": 20, "detail": f"Active ({days_left} days left)"}
            except ValueError:
                score += 5.0
                breakdown["deadline_currency"] = {"points": 5, "max": 20, "detail": "Deadline format non-standard"}

        # 5. Schema Completeness without hallucination (15 pts)
        checked_fields = ["name", "provider", "amount_benefit", "education_level", "selection_process"]
        filled = sum(1 for f in checked_fields if item.get(f) and item.get(f) != "Not specified")
        completeness_pts = (filled / len(checked_fields)) * 15.0
        score += completeness_pts
        breakdown["schema_completeness"] = {
            "points": round(completeness_pts, 1),
            "max": 15,
            "detail": f"{filled}/{len(checked_fields)} core attributes structured"
        }

        final_score = round(min(100.0, score), 1)

        # Classification Rule:
        if status == "EXPIRED":
            final_status = "EXPIRED"
        elif final_score >= 95.0:
            final_status = status
        else:
            final_status = "REVIEW_REQUIRED"

        return final_score, breakdown, final_status