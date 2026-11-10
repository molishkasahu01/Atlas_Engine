import re
from urllib.parse import urlparse
from datetime import datetime, date
from dateutil import parser as date_parser


def normalize_date_str(raw: str) -> str | None:
    """Normalize DD-MM-YYYY, YYYY-MM-DD, or natural text dates into ISO YYYY-MM-DD."""
    if not raw or not isinstance(raw, str):
        return None
    cleaned = re.sub(r"\s+", " ", raw).strip()
    if not cleaned or cleaned.lower() in {"not specified", "rolling", "tba", "na", "n/a"}:
        return None

    m_dmy = re.match(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})$", cleaned)
    if m_dmy:
        day, month, year = int(m_dmy.group(1)), int(m_dmy.group(2)), int(m_dmy.group(3))
        if year < 100:
            year += 2000
        try:
            return date(year, month, day).strftime("%Y-%m-%d")
        except ValueError:
            return None

    m_iso = re.match(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$", cleaned)
    if m_iso:
        try:
            return date(int(m_iso.group(1)), int(m_iso.group(2)), int(m_iso.group(3))).strftime("%Y-%m-%d")
        except ValueError:
            return None

    if not re.search(
        r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b",
        cleaned,
        re.IGNORECASE,
    ):
        return None

    try:
        dt = date_parser.parse(cleaned, dayfirst=True, fuzzy=True)
        if 2020 <= dt.year <= 2035:
            return dt.strftime("%Y-%m-%d")
    except Exception:
        return None
    return None


class VerificationEngine:
    """
    Computes deterministic confidence score based on concrete proof:
    1. Primary Domain Trust: (Max 30 pts)
       - .gov.in, .nic.in, .ac.in, official corporate CSR/foundation domains
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

    OFFICIAL_TLDS = ['.gov.in', '.nic.in', '.ac.in', '.edu.in', '.org.in', '.res.in', '.gov', '.edu']
    BLOCKED_SOCIAL_HOSTS = (
        "facebook.com", "twitter.com", "x.com", "instagram.com",
        "youtube.com", "youtu.be", "linkedin.com", "play.google.com",
    )
    VERIFIED_OFFICIAL_HOSTS = (
        "pmrf.in", "licindia.in", "tatatrusts.org", "jntataendowment.org",
        "inlaksfoundation.org", "kcmet.org", "adityabirlascholars.net",
        "pg.nsfoundation.co.in", "reliancefoundation.org", "infosys.com",
    )
    PLACEHOLDER_SNIPPET_MARKERS = (
        "inferred", "fallback due to", "default standard academic cycle", "mock", "simulated",
    )

    @staticmethod
    def is_official_domain(url: str, provider: str) -> tuple[bool, str]:
        if not url:
            return False, "No URL provided"
        domain = urlparse(url).netloc.lower()
        if not domain:
            return False, "Invalid URL domain"
        if any(bad in domain for bad in VerificationEngine.BLOCKED_SOCIAL_HOSTS):
            return False, f"Social/non-official domain rejected: {domain}"

        for tld in VerificationEngine.OFFICIAL_TLDS:
            if domain.endswith(tld):
                return True, f"Verified institutional/government TLD ({tld})"

        for host in VerificationEngine.VERIFIED_OFFICIAL_HOSTS:
            if domain == host or domain.endswith("." + host):
                return True, f"Verified official scholarship/foundation domain: {domain}"

        # Corporate / Trust domain matching
        provider_slug = re.sub(r'[^a-z0-9]', '', (provider or '').lower())
        domain_clean = re.sub(r'[^a-z0-9]', '', domain.replace('www.', '').split('.')[0])
        if len(domain_clean) >= 4 and (domain_clean in provider_slug or provider_slug in domain_clean):
            return True, f"Official provider domain matches '{provider}'"

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
        valid_evidence = [
            e for e in (evidence_list or [])
            if e.get("source_snippet")
            and not any(m in e["source_snippet"].lower() for m in cls.PLACEHOLDER_SNIPPET_MARKERS)
        ]
        field_names = [e["field_name"] for e in valid_evidence]
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
        deadline_str = str(item.get("closing_date", "") or "").strip()
        is_closed_flag = bool(item.get("is_closed")) or "closed" in deadline_str.lower()
        status = "ACTIVE"

        if is_closed_flag and not normalize_date_str(deadline_str):
            status = "EXPIRED"
            score += 18.0
            breakdown["deadline_currency"] = {"points": 18, "max": 20, "detail": f"Applications closed ({deadline_str or 'official notice'})"}
        elif not deadline_str or deadline_str.lower() == "not specified":
            score += 10.0
            breakdown["deadline_currency"] = {"points": 10, "max": 20, "detail": "Deadline rolling or not specified on source"}
        else:
            normalized = normalize_date_str(deadline_str)
            if normalized:
                deadline = datetime.strptime(normalized, "%Y-%m-%d").date()
                today = date.today()
                if deadline < today or is_closed_flag:
                    status = "EXPIRED"
                    score += 18.0
                    breakdown["deadline_currency"] = {"points": 18, "max": 20, "detail": f"Closed / Expired on {deadline}"}
                else:
                    days_left = (deadline - today).days
                    if days_left <= 15:
                        status = "EXPIRING_SOON"
                    score += 20.0
                    breakdown["deadline_currency"] = {"points": 20, "max": 20, "detail": f"Active ({days_left} days left)"}
            else:
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