import hashlib
import io
import json
import logging
import re
import shutil
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urljoin, urlparse, urlunparse
from urllib.robotparser import RobotFileParser

import requests
import urllib3
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from database import get_connection, init_db
from verification_engine import VerificationEngine, normalize_date_str

# Disable SSL warnings for government websites with incomplete intermediate chains
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

DEFAULT_TARGET = 22

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

NON_SCHOLARSHIP_HOSTS = (
    "facebook.com", "twitter.com", "x.com", "instagram.com",
    "youtube.com", "youtu.be", "linkedin.com", "play.google.com",
)

BAD_PATH_TOKENS = (
    "privacy-policy", "copyright-policy", "terms-conditions", "contact-us",
    "disclaimer", "sitemap", "login", "user_sessions", "errorpage", "404",
    "about-us", "legal-disclaimers", "wp-admin", "our-scholars-",
    "/administration/", "financial_advisor", "annual-reports",
    "financial-declaration", "opportunities-for-grantees", "canarabank",
)

GENERIC_LINK_NAMES = {
    "click here", "click here.", "read more", "know more", "learn more",
    "apply", "apply now", "individual grants", "opportunities for grantees",
    "annual reports & financial declaration", "financial advisor",
}

BAD_AMOUNT_CONTEXT = (
    "application fee", "registration fee", "exam fee", "processing fee",
    "online application fees", "total visitors", "helpline", "toll free",
    "pin code", "copyright", "fee payable", "reimbursement cost for conduct",
    "mock test", "examination fee",
)

INCOME_CONTEXT_TOKENS = (
    "income", "ceiling", "not exceeding", "not exceed", "should not be more than",
    "below rs", "below ₹", "less than rs", "less than ₹", "upto rs", "upto ₹",
    "up to rs", "up to ₹", "economically weaker", "relaxations in upper limit",
)


def build_retry_session(timeout_retries: int = 2) -> requests.Session:
    """Create a requests.Session with bounded retries and exponential backoff."""
    session = requests.Session()
    session.headers.update(HEADERS)
    retry = Retry(
        total=timeout_retries,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "HEAD"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


class RobotPolicyCache:
    """Thread-safe robots.txt cache per netloc so crawling respects site rules."""

    def __init__(self, user_agent: str = "AtlasScholarshipBot"):
        self.user_agent = user_agent
        self._cache: dict[str, RobotFileParser | None] = {}
        self._lock = threading.Lock()

    def can_fetch(self, session: requests.Session, url: str) -> bool:
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return False
        base = f"{parsed.scheme}://{parsed.netloc}"
        with self._lock:
            cached = self._cache.get(base, "MISS")
        if cached == "MISS":
            rp = RobotFileParser()
            robots_url = f"{base}/robots.txt"
            try:
                resp = session.get(robots_url, timeout=8, verify=False)
                if resp.status_code == 200:
                    rp.parse(resp.text.splitlines())
                    cached = rp
                elif resp.status_code in (401, 403):
                    cached = None
                else:
                    rp.parse([])
                    cached = rp
            except Exception:
                rp.parse([])
                cached = rp
            with self._lock:
                self._cache[base] = cached

        if cached is None:
            return False
        return cached.can_fetch("*", url) and cached.can_fetch(self.user_agent, url)


def canonicalize_url(url: str) -> str:
    """Normalize URL for duplicate comparison without altering query parameters."""
    if not url:
        return ""
    p = urlparse(url.strip())
    path = re.sub(r"/+$", "", p.path) or "/"
    netloc = p.netloc.lower().removeprefix("www.")
    return urlunparse((p.scheme.lower(), netloc, path, "", p.query, ""))


def normalize_scholarship_name(name: str) -> str:
    """Normalize scholarship name for deduplication across portals."""
    if not name:
        return ""
    s = name.lower()
    s = re.sub(r"\((?:merit|welfare)\s+based\s+scheme\)", "", s)
    s = re.sub(r"\bjamsetjee\s+nusserwanjee\b", "j n", s)
    s = re.sub(r"\bthe\b|\bprogramme\b|\bprogram\b|\bscheme\b|\bsite\b", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    return re.sub(r"\s+", " ", s)


def make_stable_slug(name: str, url: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", (name or "scholarship").lower()).strip("-")[:42]
    digest = hashlib.md5((canonicalize_url(url) or name).encode("utf-8")).hexdigest()[:8]
    return f"{base}-{digest}"


_ABBREV_ENDINGS = (
    "rs.", "dr.", "mr.", "ms.", "mrs.", "st.", "no.", "s.no.", "govt.",
    "i.e.", "e.g.", "p.a.", "p.m.", "approx.", "etc.", "vs.",
)


def split_sentences(text: str) -> list[str]:
    """Split text into sentences without breaking inside 'Rs.', decimals, or wrapped lines."""
    if not text:
        return []
    # Preserve double-newline paragraph breaks and bullets, unwrap single PDF line breaks
    normalized = re.sub(r"\r\n?", "\n", text)
    blocks = re.split(r"\n{2,}|\s*[•]\s*", normalized)
    sentences: list[str] = []
    for block in blocks:
        flat = re.sub(r"\s+", " ", block).strip()
        if len(flat) < 12:
            continue
        start = 0
        for m in re.finditer(r"[.!?]\s+", flat):
            end_pos = m.start() + 1  # position right after punctuation
            chunk_lower = flat[max(0, end_pos - 12):end_pos].lower()
            # Do not split on abbreviations like "Rs." or decimals/section numbers like "4.5" or "2.0"
            if any(chunk_lower.endswith(ab) for ab in _ABBREV_ENDINGS):
                continue
            if re.search(r"\d\.$", chunk_lower):
                continue
            cand = flat[start:end_pos].strip()
            if len(cand) >= 12:
                sentences.append(cand)
            start = m.end()
        tail = flat[start:].strip()
        if len(tail) >= 12:
            sentences.append(tail)
    return sentences


def extract_pdf_text(content: bytes) -> str:
    """Extract text from PDF bytes using pdftotext with pure-Python pypdf fallback."""
    if not content or not content.startswith(b"%PDF"):
        return ""
    if shutil.which("pdftotext"):
        try:
            with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
                f.write(content)
                f.flush()
                out = subprocess.check_output(
                    ["pdftotext", f.name, "-"],
                    text=True,
                    stderr=subprocess.DEVNULL,
                    timeout=15,
                )
            text = re.sub(r"[ \t]+", " ", out).strip()
            if text:
                return text
        except Exception:
            pass
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(content))
        pages = [page.extract_text() or "" for page in reader.pages]
        return re.sub(r"[ \t]+", " ", "\n\n".join(pages)).strip()
    except Exception:
        return ""


def clean_html_text(html: str) -> tuple[str, str]:
    """Extract clean page title and main visible text from HTML."""
    if not html:
        return "", ""
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    # Prefer <main> or article body if large enough, else full body without nav/footer
    main = soup.find("main") or soup.find("article")
    if main and len(main.get_text(" ", strip=True)) >= 400:
        text = main.get_text("\n", strip=True)
    else:
        for tag in soup(["nav", "header", "footer"]):
            tag.decompose()
        text = soup.get_text("\n", strip=True)
    return title, re.sub(r"[ \t]+", " ", text).strip()


def is_valid_scholarship_page(url: str, title: str, text: str) -> tuple[bool, str]:
    """Validate that an accessed page is genuine scholarship content (not 404/login/social)."""
    if not url or not text or len(text) < 200:
        return False, "Empty or insufficient source text"
    host = urlparse(url).netloc.lower()
    if any(bad in host for bad in NON_SCHOLARSHIP_HOSTS):
        return False, f"Non-scholarship social host: {host}"
    low_url = url.lower()
    if any(tok in low_url for tok in BAD_PATH_TOKENS):
        return False, f"Non-scholarship/login/policy URL: {url}"
    low_title = (title or "").lower()
    if any(err in low_title for err in ("404", "page not found", "errorpage", "access denied", "not found")):
        return False, f"Error page title: {title}"
    low_text = text.lower()
    if not any(kw in low_text for kw in ("scholarship", "fellowship", "financial assistance", "stipend", "endowment", "grant")):
        return False, "Page text does not describe a scholarship or fellowship"
    return True, "Valid scholarship source"


def has_verifiable_scholarship_core(record: dict) -> bool:
    """Reject thin index/nav pages that lack any award amount, deadline, or income/merit criteria."""
    return any(
        record.get(f) and record[f] != "Not specified"
        for f in ("amount_benefit", "closing_date", "income_criteria")
    )


def _extract_amount(text: str) -> tuple[str, str]:
    amt_rx = re.compile(
        r"(?:₹|\bRs\.?\s*|\bINR\s*|\bUSD\s*|\$)\s*\d[\d,]*(?:\.\d+)?(?:\s*(?:lakhs?|lakh|crores?|k))?(?:\s*/-)?(?:\s*(?:per\s+(?:annum|year|month)|p\.a\.|p\.m\.|/month|/annum|/year|for\s+\d+\s+months(?:\s+in\s+a\s+year)?))?",
        re.IGNORECASE,
    )
    slash_period_rx = re.compile(
        r"\b\d[\d,]*/-\s*(?:per\s+(?:month|annum|year)|p\.m\.|p\.a\.)",
        re.IGNORECASE,
    )
    per_period_rx = re.compile(
        r"\b\d[\d,]{3,}(?:\.\d+)?\s*(?:per\s+(?:month|annum|year)|p\.m\.|p\.a\.)",
        re.IGNORECASE,
    )
    pct_rx = re.compile(
        r"\b(?:up\s+to\s+)?(?:100|75|50|25)%\s+(?:tuition\s+)?(?:fee\s+)?(?:waiver|scholarship|financial\s+aid)",
        re.IGNORECASE,
    )

    sentences = split_sentences(text)
    # Pass 1: prioritize explicit scholarship amount/rate/fellowship sentences
    def _scan(priority_only: bool, allow_ocr_suffix: bool):
        for sent in sentences:
            sl = sent.lower()
            if any(bad in sl for bad in BAD_AMOUNT_CONTEXT):
                continue
            has_amt_cue = any(cue in sl for cue in (
                "rate of scholarship", "amount of scholarship", "amount:", "fellowship",
                "stipend", "scholarship amount", "duration and value", "scholarship package",
                "at the rate of", "will be awarded", "per month", "p.m.", "valued at",
                "interest-free loan scholarship", "awards interest-free",
            ))
            if priority_only and not has_amt_cue:
                continue
            for m in amt_rx.finditer(sent):
                clause_prefix = sent[max(0, m.start() - 120):m.start()].lower()
                if any(inc_tok in clause_prefix for inc_tok in INCOME_CONTEXT_TOKENS):
                    # Allow only if explicit scholarship award phrase appears AFTER the income clause
                    if not any(c in clause_prefix[-35:] for c in ("rate of scholarship", "amount of scholarship", "awarded")):
                        continue
                val = re.sub(r"\s+", " ", m.group(0)).strip()
                num_m = re.search(r"\d[\d,]*(?:\.\d+)?", val)
                if num_m:
                    raw_digits = num_m.group(0).replace(",", "")
                    if not allow_ocr_suffix and re.search(r"0001$", raw_digits):
                        continue
                    try:
                        num_val = float(raw_digits)
                    except ValueError:
                        continue
                    if num_val < 100 and not any(u in val.lower() for u in ("lakh", "crore", "usd", "$")):
                        continue
                    if num_val > 50_000_000:
                        continue
                snippet = sent[:220] + "..." if len(sent) > 220 else sent
                return val, snippet
            for rx, prefix_rs in ((slash_period_rx, "Rs. "), (per_period_rx, "Rs. ")):
                m2 = rx.search(sent)
                if m2:
                    clause_prefix = sent[max(0, m2.start() - 120):m2.start()].lower()
                    if any(inc_tok in clause_prefix for inc_tok in INCOME_CONTEXT_TOKENS):
                        continue
                    val = prefix_rs + re.sub(r"\s+", " ", m2.group(0)).strip()
                    snippet = sent[:220] + "..." if len(sent) > 220 else sent
                    return val, snippet
        return None

    res = (
        _scan(priority_only=True, allow_ocr_suffix=False)
        or _scan(priority_only=False, allow_ocr_suffix=False)
        or _scan(priority_only=True, allow_ocr_suffix=True)
        or _scan(priority_only=False, allow_ocr_suffix=True)
    )
    if res:
        return res

    for sent in sentences:
        m3 = pct_rx.search(sent)
        if m3:
            val = re.sub(r"\s+", " ", m3.group(0)).strip()
            snippet = sent[:220] + "..." if len(sent) > 220 else sent
            return val, snippet

    return "Not specified", ""


def _extract_income(text: str) -> tuple[str, str]:
    inc_rx = re.compile(
        r"(?:annual\s+|family\s+|parental\s+|household\s+)?income[^.\n]{0,55}?(?:should\s+not\s+(?:be\s+more\s+than|exceed)|should\s+be\s+(?:below|less\s+than)|not\s+exceeding|below|less\s+than|up\s*to|ceiling(?:\s+of)?|limit(?:\s+of)?|does\s+not\s+exceed|not\s+more\s+than)\s*(?:(?:₹|\bRs\.?|\bINR)\s*)?\d[\d,.]*(?:\s*(?:lakhs?|lakh|crores?|/-))?(?:\s*per\s+annum|\s*p\.a\.)?",
        re.IGNORECASE,
    )
    sentences = split_sentences(text)
    for sent in sentences:
        m = inc_rx.search(sent)
        if m:
            val = re.sub(r"\s+", " ", m.group(0)).strip()
            snippet = sent[:220] + "..." if len(sent) > 220 else sent
            return val, snippet

    merit_only_rx = re.compile(
        r"(?:based\s+only\s+on\s+merit|100%\s+merit\s+based|solely\s+on\s+merit|no\s+income\s+(?:ceiling|limit))",
        re.IGNORECASE,
    )
    for sent in sentences:
        if merit_only_rx.search(sent):
            snippet = sent[:220] + "..." if len(sent) > 220 else sent
            return "Merit-based (no income limit stated)", snippet

    return "Not specified", ""


def _extract_education(text: str, name: str = "") -> tuple[str, str]:
    sentences = split_sentences(text)
    title_low = (name or "").lower()

    # Determine primary level from title first to avoid false hits from entry prerequisites ("passed Class XII")
    title_map = [
        (r"\b(?:pre\s*-?\s*matric|in\s+schools|means\s+cum\s+merit)\b", "School (Pre-Matric / Class IX-XII)"),
        (r"\b(?:post\s*-?\s*matric)\b", "Post-Matric (Class XI to Postgraduate)"),
        (r"\b(?:ph\.?d|doctoral|jrf|srf|research\s+fellows|pmrf)\b", "PhD / Doctoral Research"),
        (r"\b(?:post\s*-?\s*graduate|postgraduate|\bpg\b|\bnts-pg\b)\b", "Postgraduate"),
        (r"\b(?:technical\s+diploma|diploma)\b", "Diploma / Technical"),
        (r"\b(?:scholarship\s+for\s+higher\s+education)\b", "Undergraduate / Integrated Postgraduate"),
        (r"\b(?:technical\s+degree|undergraduate|\bnts-ug\b|college\s+and\s+university\s+students|in\s+college|engineering)\b", "Undergraduate / Degree"),
    ]
    for pat, label in title_map:
        if re.search(pat, title_low, re.IGNORECASE):
            # Find genuine supporting sentence in the document body
            body_kw = {
                "School (Pre-Matric / Class IX-XII)": r"\b(?:class\s+(?:viii|ix|x|xi|xii|8|9|10|11|12)|school|pre-?matric)\b",
                "Post-Matric (Class XI to Postgraduate)": r"\b(?:post-?matric|class\s+(?:xi|xii)|diploma|degree|undergraduate|postgraduate)\b",
                "PhD / Doctoral Research": r"\b(?:ph\.?d|doctoral|research|jrf|srf)\b",
                "Postgraduate": r"\b(?:post\s*-?\s*graduat|master|pg|m\.tech|m\.sc)\b",
                "Diploma / Technical": r"\b(?:diploma|polytechnic|aicte\s+approved)\b",
                "Undergraduate / Integrated Postgraduate": r"\b(?:undergraduate|postgraduate|b\.sc|m\.sc|basic\s+and\s+natural\s+science)\b",
                "Undergraduate / Degree": r"\b(?:under\s*-?\s*graduat|degree\s+level|b\.tech|b\.e\.|first\s+year|college|university)\b",
            }[label]
            rx = re.compile(body_kw, re.IGNORECASE)
            for sent in sentences:
                if rx.search(sent) and not any(neg in sent.lower() for neg in ("not meant for", "not eligible")):
                    return label, (sent[:220] + "..." if len(sent) > 220 else sent)
            if sentences:
                return label, (sentences[0][:220] + "..." if len(sentences[0]) > 220 else sentences[0])
            return label, ""

    body_patterns = [
        (r"\b(?:ph\.?d\.?|doctoral\s+research|post-?doctoral)\b", "PhD / Doctoral"),
        (r"\b(?:post\s*-?\s*graduat(?:e|ion)|master['’]?s|m\.tech|m\.sc|m\.des|mba|overseas\s+stud(?:y|ies))\b", "Postgraduate"),
        (r"\b(?:under\s*-?\s*graduat(?:e|ion)|bachelor['’]?s|b\.tech|b\.e\.|b\.sc|degree\s+(?:level\s+)?course|college\s+and\s+university)\b", "Undergraduate / Degree"),
        (r"\b(?:diploma\s+(?:level\s+)?course|polytechnic|iti|vocational)\b", "Diploma / Technical"),
        (r"\b(?:studying\s+in\s+class\s+(?:viii|ix|x|xi|xii|8|9|10|11|12)|pre-?matric)\b", "School (Pre-Matric / Class IX-XII)"),
    ]
    found = []
    snippet = ""
    for pat, label in body_patterns:
        rx = re.compile(pat, re.IGNORECASE)
        for sent in sentences:
            if any(neg in sent.lower() for neg in ("not meant for", "not eligible", "not applicable")):
                continue
            if rx.search(sent):
                found.append(label)
                if not snippet:
                    snippet = sent[:220] + "..." if len(sent) > 220 else sent
                break
    if found:
        return ", ".join(dict.fromkeys(found[:2])), snippet
    return "Not specified", ""


def _extract_dates(text: str, meta: dict) -> tuple[str, str, str, str, bool]:
    """Return (opening_date, closing_date, opening_snippet, closing_snippet, is_closed)."""
    if meta.get("closing_date"):
        open_dt = meta.get("opening_date") or "Not specified"
        close_dt = meta["closing_date"]
        open_snip = meta.get("opening_snippet", "")
        close_snip = meta.get("closing_snippet", "")
        is_closed = bool(meta.get("is_closed"))
        return open_dt, close_dt, open_snip, close_snip, is_closed

    sentences = split_sentences(text)
    open_dt = "Not specified"
    close_dt = "Not specified"
    open_snip = ""
    close_snip = ""
    is_closed = False

    # Look for explicit open/close window sentences (e.g., INSPIRE, J.N. Tata, Inlaks, KCMET, NSF)
    date_token = r"(?:\d{1,2}(?:st|nd|rd|th)?\s+(?:January|February|March|April|May|June|July|August|September|October|November|December),?\s+\d{4}|\d{1,2}[-/]\d{1,2}[-/]\d{4}|\d{4}[-/]\d{1,2}[-/]\d{1,2})"

    range_rx = re.compile(
        rf"(?:started\s+from|open\s+from|available[^.\n]{{0,40}}?from)\s+({date_token})[^.\n]{{0,60}}?(?:till|until|to)\s+({date_token}(?:\s*\([^)]+\))?)",
        re.IGNORECASE,
    )
    close_rx = re.compile(
        rf"(?:last\s+date|deadline|closing\s+date|open\s+till|will\s+remain\s+open\s+till|closed\s+on)[:\s]*({date_token})",
        re.IGNORECASE,
    )
    closed_cycle_rx = re.compile(
        r"(?:applications\s+(?:are\s+)?closed(?:\s+for\s+(?:the\s+academic\s+year\s+)?20\d{2})?|presently\s+all\s+seats[^.\n]{0,40}filled\s+up)",
        re.IGNORECASE,
    )

    for sent in sentences:
        rm = range_rx.search(sent)
        if rm:
            o_norm = normalize_date_str(rm.group(1))
            c_norm = normalize_date_str(rm.group(2))
            if o_norm:
                open_dt = o_norm
                open_snip = sent[:220] + "..." if len(sent) > 220 else sent
            if c_norm:
                close_dt = c_norm
                close_snip = sent[:220] + "..." if len(sent) > 220 else sent
                return open_dt, close_dt, open_snip, close_snip, is_closed

    for sent in sentences:
        if "sbi bank passbook" in sent.lower():
            continue
        cm = close_rx.search(sent)
        if cm:
            c_norm = normalize_date_str(cm.group(1))
            if c_norm:
                close_dt = c_norm
                close_snip = sent[:220] + "..." if len(sent) > 220 else sent
                if "closed on" in sent.lower():
                    is_closed = True
                return open_dt, close_dt, open_snip, close_snip, is_closed

    for sent in sentences:
        clm = closed_cycle_rx.search(sent)
        if clm:
            is_closed = True
            yr_m = re.search(r"20\d{2}", clm.group(0))
            close_dt = f"Closed ({yr_m.group(0)} cycle)" if yr_m else "Closed (Current cycle)"
            close_snip = sent[:220] + "..." if len(sent) > 220 else sent
            return open_dt, close_dt, open_snip, close_snip, is_closed

    return open_dt, close_dt, open_snip, close_snip, is_closed


def _extract_section_sentence(text: str, keywords: tuple[str, ...]) -> tuple[str, str]:
    for sent in split_sentences(text):
        sl = sent.lower()
        if any(k in sl for k in keywords) and len(sent) >= 25:
            val = sent[:180] + "..." if len(sent) > 180 else sent
            return val, (sent[:220] + "..." if len(sent) > 220 else sent)
    return "Not specified", ""


def extract_scholarship_record(meta: dict, text: str, page_title: str = "") -> tuple[dict, list[dict]]:
    """
    Build a strictly source-grounded scholarship record + field-level evidence citations.
    Never infers or invents missing dates, amounts, or criteria.
    """
    source_url = meta["url"]
    app_url = meta.get("application_url") or source_url
    name = meta.get("inferred_name") or page_title or "Unknown Scholarship"
    name = re.sub(r"\s+", " ", name).strip()[:160]

    provider = meta.get("provider") or "Independent Trust"
    source_type = meta.get("source_type") or "Foundation/Trust"

    # Prefer clean page title when inferred_name is generic
    if page_title and (len(name) < 10 or name.lower() in GENERIC_LINK_NAMES):
        clean_title = page_title.split("|")[0].split("–")[0].strip()
        if len(clean_title) >= 8:
            name = clean_title[:160]

    evidence: list[dict] = []

    amt, amt_snip = _extract_amount(text)
    if amt != "Not specified" and amt_snip:
        evidence.append({"field_name": "amount_benefit", "extracted_value": amt, "source_snippet": amt_snip})

    inc, inc_snip = _extract_income(text)
    if inc != "Not specified" and inc_snip:
        evidence.append({"field_name": "income_criteria", "extracted_value": inc, "source_snippet": inc_snip})

    edu, edu_snip = _extract_education(text, name)
    if edu != "Not specified" and edu_snip:
        evidence.append({"field_name": "education_level", "extracted_value": edu, "source_snippet": edu_snip})

    open_dt, close_dt, open_snip, close_snip, is_closed = _extract_dates(text, meta)
    if open_dt != "Not specified" and open_snip:
        evidence.append({"field_name": "opening_date", "extracted_value": open_dt, "source_snippet": open_snip})
    if close_dt != "Not specified" and close_snip:
        evidence.append({"field_name": "closing_date", "extracted_value": close_dt, "source_snippet": close_snip})

    # Gender, category, domicile extracted from explicit source cues
    combined_low = f"{name} {text}".lower()
    if any(k in combined_low for k in ("girl students", "girl child", "female candidates", "women", "only for girls")):
        gender = "Female only"
        _, g_snip = _extract_section_sentence(f"{name}. {text}", ("girl", "female", "women"))
        if g_snip:
            evidence.append({"field_name": "gender_criteria", "extracted_value": gender, "source_snippet": g_snip})
    else:
        gender = "All"

    if any(k in combined_low for k in ("specially abled", "specially-abled", "disabilities", "pwd", "divyang")):
        category = "Persons with Disabilities (PwD)"
    elif any(k in combined_low for k in ("sc students", "scheduled caste")):
        category = "SC"
    elif any(k in combined_low for k in ("st students", "schedule tribe", "scheduled tribe")):
        category = "ST"
    elif any(k in combined_low for k in ("obc", "ebc", "dnt")):
        category = "OBC / EBC / DNT"
    elif any(k in combined_low for k in ("merit", "fellowship", "talent")):
        category = "Merit"
    else:
        category = "General / Welfare"

    if "north eastern region" in combined_low or " ner " in combined_low:
        domicile = "North Eastern Region (NER), India"
    elif "jammu" in combined_low and "ladakh" in combined_low:
        domicile = "Jammu & Kashmir and Ladakh, India"
    elif any(k in combined_low for k in ("indian citizen", "indian passport", "indian national", "india", "resident in india")):
        domicile = "Indian National / Resident"
    else:
        domicile = "Not specified"

    sel_proc, sel_snip = _extract_section_sentence(
        text, (
            "selection", "merit list", "shortlisted", "interview", "aptitude test",
            "screening", "all india entrance", "entrance examination", "basis of merit", "counselling",
        ),
    )
    if sel_proc != "Not specified" and sel_snip:
        evidence.append({"field_name": "selection_process", "extracted_value": sel_proc, "source_snippet": sel_snip})

    docs_req, docs_snip = _extract_section_sentence(
        text, ("documents required", "aadhaar", "income certificate", "marksheet", "mark sheet", "domicile certificate", "bonafide"),
    )
    if docs_req != "Not specified" and docs_snip:
        evidence.append({"field_name": "documents_required", "extracted_value": docs_req, "source_snippet": docs_snip})

    # Official citation metadata record (unique field_name key)
    verified_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    is_off, _ = VerificationEngine.is_official_domain(source_url, provider)
    source_tier = "Official Primary Source" if is_off else "Secondary / Unverified Source"
    citation_title = (page_title or name).strip()[:120]
    evidence.append({
        "field_name": "source_citation",
        "extracted_value": f"{source_tier}: {provider}",
        "source_snippet": f"Title: {citation_title} | URL: {source_url} | Verified: {verified_at}",
    })

    record = {
        "slug": make_stable_slug(name, source_url),
        "name": name,
        "provider": provider,
        "source_type": source_type,
        "official_source_url": source_url,
        "application_url": app_url,
        "amount_benefit": amt,
        "education_level": edu,
        "income_criteria": inc,
        "gender_criteria": gender,
        "category_criteria": category,
        "domicile_criteria": domicile,
        "opening_date": open_dt,
        "closing_date": close_dt,
        "selection_process": sel_proc,
        "documents_required": docs_req,
        "is_closed": is_closed,
    }
    return record, evidence


class ScholarshipDiscoveryEngine:
    """
    Phase 1: Multi-source Discovery.
    Queries official government portals (NSP, DST INSPIRE, MoE PMRF),
    institutional university pages, and verified foundation/CSR portals while
    honoring robots.txt and filtering non-scholarship links.
    """

    NSP_PORTAL = "https://scholarships.gov.in/All-Scholarships"
    NSP_APPLY_URL = "https://scholarships.gov.in/ApplicationForm/"

    # Direct verified scholarship pages across Government, Research, Corporate, Foundations & Universities
    DIRECT_PORTALS = [
        {
            "url": "https://www.online-inspire.gov.in/",
            "supplement_url": "https://www.online-inspire.gov.in/Content/SHEFAQs2025.pdf",
            "inferred_name": "DST INSPIRE Scholarship for Higher Education (SHE)",
            "source_type": "Government",
            "provider": "Department of Science & Technology (DST), Govt. of India",
        },
        {
            "url": "https://www.pmrf.in/",
            "inferred_name": "Prime Minister's Research Fellows (PMRF) Scheme",
            "source_type": "Government",
            "provider": "Ministry of Education (PMRF), Govt. of India",
        },
        {
            "url": "https://licindia.in/golden-jubilee-scholarship-scheme",
            "inferred_name": "LIC Golden Jubilee Scholarship Scheme",
            "source_type": "Corporate CSR / Trust",
            "provider": "Life Insurance Corporation of India (LIC)",
        },
        {
            "url": "https://www.adityabirlascholars.net/the-scholarship/eligibility-application-process/",
            "inferred_name": "Aditya Birla Group Scholarships",
            "source_type": "Corporate CSR / Trust",
            "provider": "Aditya Birla Group",
        },
        {
            "url": "https://jntataendowment.org/",
            "inferred_name": "The J N Tata Endowment Loan Scholarship",
            "source_type": "Foundation/Trust",
            "provider": "J. N. Tata Endowment",
        },
        {
            "url": "https://inlaksfoundation.org/opportunities/scholarship/",
            "inferred_name": "The Inlaks Scholarship",
            "source_type": "Foundation/Trust",
            "provider": "Inlaks Shivdasani Foundation",
        },
        {
            "url": "https://www.kcmet.org/what-we-do-Scholarship-Grants.aspx",
            "inferred_name": "K C Mahindra Scholarship for Postgraduate Studies",
            "source_type": "Foundation/Trust",
            "provider": "K. C. Mahindra Education Trust (KCMET)",
        },
        {
            "url": "https://pg.nsfoundation.co.in/",
            "inferred_name": "Narotam Sekhsaria Foundation PG Scholarship Programme",
            "source_type": "Foundation/Trust",
            "provider": "Narotam Sekhsaria Foundation",
        },
        {
            "url": "https://www.iisc.ac.in/admissions/fees-and-scholarships/",
            "inferred_name": "IISc Bangalore Research & Postgraduate Scholarships / Fellowships",
            "source_type": "University",
            "provider": "Indian Institute of Science (IISc)",
        },
        {
            "url": "https://www.srmist.edu.in/admission-india/engineering/scholarships/",
            "inferred_name": "SRMIST Engineering Merit & Founder's Scholarships",
            "source_type": "University",
            "provider": "SRM Institute of Science and Technology (SRMIST)",
        },
        {
            "url": "https://www.ashoka.edu.in/undergraduate-programme-scholarships/",
            "inferred_name": "Ashoka University Undergraduate Merit & Need-Based Scholarships",
            "source_type": "University",
            "provider": "Ashoka University",
        },
    ]

    # Legacy hub SEEDS preserved for compatibility and sub-link discovery
    SEEDS = [
        "https://scholarships.gov.in/All-Scholarships",
        "https://www.ugc.gov.in/Scholarships",
        "https://www.tatatrusts.org/explore-our-work/enabling-human-potential/individual-grants/education-grants",
        "https://www.iisc.ac.in/admissions/fees-and-scholarships/",
        "https://www.ashoka.edu.in/admissions/scholarships/",
    ]

    KEYWORDS = [
        "scholarship", "fellowship", "grant", "scheme", "pragati",
        "saksham", "swanath", "financial-aid", "financial-assistance",
        "endowment", "merit", "postgraduate", "undergraduate",
    ]

    def __init__(self):
        self.robots = RobotPolicyCache()
        self.stats = {
            "sources_accessed": 0,
            "discovery_errors": [],
        }

    @staticmethod
    def classify_source_type(url: str) -> tuple[str, str]:
        """Dynamically classify the source type and provider name based on the domain."""
        domain = urlparse(url).netloc.lower()
        if domain.endswith(".gov.in") or domain.endswith(".nic.in") or "pmrf.in" in domain:
            if "aicte" in domain:
                return "Government", "All India Council for Technical Education (AICTE)"
            if "ugc" in domain:
                return "Government", "University Grants Commission (UGC)"
            if "inspire" in domain:
                return "Government", "Department of Science & Technology (DST)"
            if "pmrf" in domain:
                return "Government", "Ministry of Education (PMRF)"
            return "Government", "Government of India Portal"
        if domain.endswith(".ac.in") or domain.endswith(".edu.in"):
            if "iitb" in domain:
                return "University", "Indian Institute of Technology Bombay"
            if "iisc" in domain:
                return "University", "Indian Institute of Science (IISc)"
            if "ashoka" in domain:
                return "University", "Ashoka University"
            if "srmist" in domain:
                return "University", "SRMIST"
            return "University", "Educational Institution"
        if "tatatrusts" in domain or "jntata" in domain:
            return "Foundation/Trust", "J. N. Tata Endowment / Tata Trusts"
        if "licindia" in domain:
            return "Corporate CSR / Trust", "Life Insurance Corporation of India (LIC)"
        if "adityabirla" in domain:
            return "Corporate CSR / Trust", "Aditya Birla Group"
        if "kcmet" in domain:
            return "Foundation/Trust", "K. C. Mahindra Education Trust (KCMET)"
        if "inlaks" in domain:
            return "Foundation/Trust", "Inlaks Shivdasani Foundation"
        if "nsfoundation" in domain:
            return "Foundation/Trust", "Narotam Sekhsaria Foundation"
        if "reliance" in domain:
            return "Corporate CSR / Trust", "Reliance Foundation"
        return "Foundation/Trust", "Independent Trust"

    @classmethod
    def parse_nsp_directory(cls, html: str, base_url: str) -> list[dict]:
        """Parse individual central government schemes from scholarships.gov.in/All-Scholarships."""
        soup = BeautifulSoup(html, "html.parser")
        candidates: list[dict] = []
        used_doc_urls: set[str] = set()

        for acc in soup.select(".accordion-item"):
            hdr = acc.select_one(".accordion-header")
            body = acc.select_one(".accordion-body")
            if not hdr or not body:
                continue
            ministry = " ".join(hdr.get_text(" ", strip=True).split())

            for row in body.select("div.row"):
                h6 = row.find("h6")
                if not h6:
                    continue
                scheme_name = " ".join(h6.get_text(" ", strip=True).split())
                spans = [" ".join(sp.get_text(" ", strip=True).split()) for sp in row.find_all("span")]
                links = {
                    a.get_text(" ", strip=True): urljoin(base_url, a["href"])
                    for a in row.find_all("a", href=True)
                }

                spec_url = links.get("Specifications", "")
                faq_url = links.get("FAQ", "")
                # Choose a unique official guideline/FAQ PDF per scheme
                doc_url = ""
                alt_url = ""
                for cand in (spec_url, faq_url):
                    if cand and "null" not in cand.lower() and "not_available" not in cand.lower() and cand.startswith("http"):
                        if cand not in used_doc_urls and not doc_url:
                            doc_url = cand
                        elif cand != doc_url and not alt_url:
                            alt_url = cand
                if not doc_url:
                    continue
                used_doc_urls.add(doc_url)

                open_dt = "Not specified"
                close_dt = "Not specified"
                open_snip = ""
                close_snip = ""
                is_closed = False

                for sp in spans:
                    sl = sp.lower()
                    if "open from" in sl:
                        dm = re.search(r"(\d{1,2}[-/]\d{1,2}[-/]\d{4})", sp)
                        if dm:
                            open_dt = normalize_date_str(dm.group(1)) or "Not specified"
                            open_snip = f"{sp} (Source: {base_url})"
                    elif "student application" in sl:
                        dm = re.search(r"(\d{1,2}[-/]\d{1,2}[-/]\d{4})", sp)
                        if dm:
                            close_dt = normalize_date_str(dm.group(1)) or "Not specified"
                            close_snip = f"{sp} (Source: {base_url})"
                        if "closed on" in sl:
                            is_closed = True

                candidates.append({
                    "url": doc_url,
                    "alt_doc_url": alt_url,
                    "inferred_name": scheme_name,
                    "source_type": "Government",
                    "provider": ministry,
                    "application_url": cls.NSP_APPLY_URL,
                    "opening_date": open_dt,
                    "closing_date": close_dt,
                    "opening_snippet": open_snip,
                    "closing_snippet": close_snip,
                    "is_closed": is_closed,
                    "is_pdf": True,
                    "origin": "nsp",
                })
        return candidates

    def discover_urls(self, session: requests.Session) -> list[dict]:
        logging.info("--- PHASE 1: DISCOVERY ENGINE STARTED ---")
        discovered: list[dict] = []
        visited_canonical: set[str] = set()
        self.stats = {"sources_accessed": 0, "discovery_errors": []}

        def _add_candidate(meta: dict):
            canon = canonicalize_url(meta["url"])
            if not canon or canon in visited_canonical:
                return
            host = urlparse(meta["url"]).netloc.lower()
            if any(bad in host for bad in NON_SCHOLARSHIP_HOSTS):
                return
            if any(tok in meta["url"].lower() for tok in BAD_PATH_TOKENS):
                return
            visited_canonical.add(canon)
            discovered.append(meta)

        # 1. Direct authoritative scholarship portals
        for meta in self.DIRECT_PORTALS:
            if self.robots.can_fetch(session, meta["url"]):
                _add_candidate(dict(meta, is_pdf=False, origin="direct"))

        # 2. National Scholarship Portal directory (30+ official central schemes)
        try:
            if self.robots.can_fetch(session, self.NSP_PORTAL):
                self.stats["sources_accessed"] += 1
                resp = session.get(self.NSP_PORTAL, timeout=15, verify=False)
                if resp.status_code == 200:
                    for item in self.parse_nsp_directory(resp.text, resp.url):
                        _add_candidate(item)
                else:
                    self.stats["discovery_errors"].append(f"{self.NSP_PORTAL}: HTTP {resp.status_code}")
        except Exception as e:
            self.stats["discovery_errors"].append(f"{self.NSP_PORTAL}: {e}")
            logging.warning(f"Failed to spider NSP directory {self.NSP_PORTAL}: {e}")

        # 3. Hub seed link expansion (filtered strictly to scholarship sub-pages)
        for seed in self.SEEDS:
            if seed == self.NSP_PORTAL:
                continue
            logging.info(f"Spidering seed directory: {seed}")
            try:
                if not self.robots.can_fetch(session, seed):
                    continue
                self.stats["sources_accessed"] += 1
                resp = session.get(seed, timeout=12, verify=False)
                if resp.status_code != 200:
                    self.stats["discovery_errors"].append(f"{seed}: HTTP {resp.status_code}")
                    continue

                soup = BeautifulSoup(resp.text, "html.parser")
                for link in soup.find_all("a", href=True):
                    href = link["href"].strip()
                    full_url = urljoin(resp.url, href).split("#")[0]
                    if not full_url.startswith("http") or full_url.lower().endswith((".jpg", ".png", ".svg", ".css", ".js", ".zip")):
                        continue
                    link_text = link.get_text(" ", strip=True)
                    if len(link_text) < 6 or link_text.lower() in GENERIC_LINK_NAMES:
                        continue
                    if full_url.lower().rstrip("/").endswith("/individual-grants"):
                        continue
                    if any(kw in full_url.lower() or kw in link_text.lower() for kw in self.KEYWORDS):
                        source_type, provider = self.classify_source_type(full_url)
                        _add_candidate({
                            "url": full_url,
                            "inferred_name": link_text,
                            "source_type": source_type,
                            "provider": provider,
                            "is_pdf": full_url.lower().endswith(".pdf"),
                            "origin": "hub",
                        })
            except Exception as e:
                self.stats["discovery_errors"].append(f"{seed}: {e}")
                logging.warning(f"Failed to spider {seed}: {e}")

        logging.info(f"Discovery Complete: Found {len(discovered)} potential scholarship URLs.")
        return discovered


class LiveWebCrawler:
    """
    Phase 2 & 3: Crawling, Extraction, Deduplication, and Verification.
    """

    HEADERS = HEADERS

    def __init__(self, target: int = DEFAULT_TARGET):
        self.target = int(target)
        self.session = build_retry_session()
        self._thread_local = threading.local()
        self.discovery_engine = ScholarshipDiscoveryEngine()

    def _get_worker_session(self) -> requests.Session:
        sess = getattr(self._thread_local, "session", None)
        if sess is None:
            sess = build_retry_session()
            self._thread_local.session = sess
        return sess

    def extract_evidence_sentence(self, text: str, match_str: str) -> str:
        if not text or not match_str:
            return ""
        m_low = match_str.lower()
        for s in split_sentences(text):
            if m_low in s.lower():
                return s[:220] + "..." if len(s) > 220 else s
        return ""

    def dynamic_extract(self, meta: dict, html: str, pdf_text: str = "") -> tuple[dict, list]:
        """Dynamically extract fields and real citations without hardcoded mock fallbacks."""
        if pdf_text and html:
            page_title, html_clean = clean_html_text(html)
            combined = f"{pdf_text}\n\n{html_clean}"
            return extract_scholarship_record(meta, combined, page_title=page_title or meta.get("inferred_name", ""))
        if pdf_text:
            return extract_scholarship_record(meta, pdf_text, page_title=meta.get("inferred_name", ""))
        page_title, clean_text = clean_html_text(html)
        return extract_scholarship_record(meta, clean_text, page_title=page_title)

    def _fetch_target_content(self, meta: dict) -> tuple[str, str, str]:
        """Fetch target HTML or PDF text cleanly with robots.txt check and optional supplement PDF."""
        session = self._get_worker_session()
        url = meta["url"]
        if not self.discovery_engine.robots.can_fetch(session, url):
            return "", "", "Blocked by robots.txt"

        try:
            resp = session.get(url, timeout=15, verify=False)
        except Exception as e:
            return "", "", f"Fetch error: {type(e).__name__}"

        if resp.status_code != 200:
            return "", "", f"HTTP {resp.status_code}"

        content_type = resp.headers.get("Content-Type", "").lower()
        if meta.get("is_pdf") or "application/pdf" in content_type or resp.content.startswith(b"%PDF"):
            txt = extract_pdf_text(resp.content)
            # If primary PDF is scanned/image-only (< 100 chars) and an alt FAQ PDF exists, try alt PDF
            if len(txt) < 100 and meta.get("alt_doc_url"):
                alt_url = meta["alt_doc_url"]
                if self.discovery_engine.robots.can_fetch(session, alt_url):
                    try:
                        alt_resp = session.get(alt_url, timeout=15, verify=False)
                        if alt_resp.status_code == 200:
                            alt_txt = extract_pdf_text(alt_resp.content)
                            if len(alt_txt) >= 100:
                                meta["url"] = alt_url
                                return "", alt_txt, ""
                    except Exception:
                        pass
            if len(txt) < 100:
                return "", "", "PDF contained insufficient extractable text"
            return "", txt, ""

        supp_text = ""
        supp_url = meta.get("supplement_url")
        if supp_url and self.discovery_engine.robots.can_fetch(session, supp_url):
            try:
                sr = session.get(supp_url, timeout=15, verify=False)
                if sr.status_code == 200:
                    supp_text = extract_pdf_text(sr.content)
            except Exception:
                supp_text = ""

        return resp.text, supp_text, ""

    def run(self, simulate_changes: bool = False):
        init_db()
        conn = get_connection()
        cursor = conn.cursor()

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        discovered_count = 0
        updated = 0
        unchanged = 0
        stale = 0

        candidates = self.discovery_engine.discover_urls(self.session)

        # Pre-deduplicate candidate queue before bounded concurrent fetching
        pre_filtered: list[dict] = []
        pre_seen_urls: set[str] = set()
        pre_seen_names: set[str] = set()
        duplicates_removed = 0

        for meta in candidates:
            canon = canonicalize_url(meta["url"])
            norm_name = normalize_scholarship_name(meta.get("inferred_name", ""))
            if canon in pre_seen_urls or (norm_name and norm_name in pre_seen_names):
                duplicates_removed += 1
                continue
            pre_seen_urls.add(canon)
            if norm_name:
                pre_seen_names.add(norm_name)
            pre_filtered.append(dict(meta))

        crawl_pool = pre_filtered[: max(self.target * 2 + 6, 50)]
        fetched_results: dict[int, tuple[dict, str, str, str]] = {}
        with ThreadPoolExecutor(max_workers=6) as pool:
            future_map = {
                pool.submit(self._fetch_target_content, m): idx
                for idx, m in enumerate(crawl_pool)
            }
            for fut in as_completed(future_map):
                idx = future_map[fut]
                m = crawl_pool[idx]
                try:
                    html, pdf_txt, err = fut.result()
                except Exception as e:
                    html, pdf_txt, err = "", "", f"Worker error: {type(e).__name__}"
                fetched_results[idx] = (m, html, pdf_txt, err)

        valid_items: list[tuple[dict, list, float, dict, str]] = []
        seen_names: set[str] = set()
        seen_urls: set[str] = set()
        rejected: list[tuple[str, str]] = []
        pages_crawled = 0

        for idx in range(len(crawl_pool)):
            meta, html, pdf_txt, err = fetched_results[idx]
            if err:
                rejected.append((meta["url"], err))
                continue
            pages_crawled += 1

            page_title = meta.get("inferred_name", "")
            if html and pdf_txt:
                page_title, html_clean = clean_html_text(html)
                check_text = f"{html_clean}\n\n{pdf_txt}"
            elif pdf_txt:
                check_text = pdf_txt
            else:
                page_title, check_text = clean_html_text(html)

            is_valid, val_reason = is_valid_scholarship_page(meta["url"], page_title, check_text)
            if not is_valid:
                rejected.append((meta["url"], val_reason))
                continue

            record, evidence_list = self.dynamic_extract(meta, html, pdf_txt)
            if not has_verifiable_scholarship_core(record):
                rejected.append((meta["url"], "No extractable amount, deadline, or income/merit criteria"))
                continue
            final_canon = canonicalize_url(record["official_source_url"])
            final_norm_name = normalize_scholarship_name(record["name"])

            if final_canon in seen_urls or (final_norm_name and final_norm_name in seen_names):
                duplicates_removed += 1
                continue

            score, breakdown, status = VerificationEngine.evaluate(record, evidence_list)
            seen_urls.add(final_canon)
            if final_norm_name:
                seen_names.add(final_norm_name)
            valid_items.append((record, evidence_list, score, breakdown, status))

        # Prioritize ACTIVE / EXPIRING_SOON verified opportunities, then REVIEW_REQUIRED / EXPIRED, up to self.target
        status_rank = {"ACTIVE": 0, "EXPIRING_SOON": 1, "REVIEW_REQUIRED": 2, "EXPIRED": 3}
        valid_items.sort(key=lambda x: (status_rank.get(x[4], 4), -x[2]))
        selected_items = valid_items[: self.target]

        logging.info(
            f"--- PHASE 2 & 3 COMPLETE: crawled={pages_crawled}, valid={len(valid_items)}, "
            f"selected={len(selected_items)}, duplicates={duplicates_removed}, rejected={len(rejected)} ---"
        )
        for bad_url, reason in rejected[:10]:
            logging.info(f"Rejected {bad_url}: {reason}")

        for record, evidence_list, score, breakdown, status in selected_items:
            slug = record["slug"]
            cursor.execute("SELECT * FROM scholarships WHERE slug = ?", (slug,))
            existing = cursor.fetchone()

            if not existing:
                cursor.execute("""
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
                    record["amount_benefit"], record["education_level"],
                    record["income_criteria"], record["gender_criteria"],
                    record["category_criteria"], record["domicile_criteria"],
                    record["opening_date"], record["closing_date"],
                    record["selection_process"], record["documents_required"],
                    status, score, json.dumps(breakdown), now_str, now_str
                ))
                sch_id = cursor.lastrowid
                discovered_count += 1

                for ev in evidence_list:
                    cursor.execute("""
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
                        cursor.execute("""
                        INSERT INTO change_logs (scholarship_id, field_name, old_value, new_value, change_date, evidence)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """, (sch_id, field_name, old_val, new_val, now_str, ev_snippet))

                    cursor.execute("""
                    UPDATE scholarships SET
                        amount_benefit = ?, income_criteria = ?, closing_date = ?,
                        application_url = ?, status = ?, confidence_score = ?,
                        confidence_breakdown = ?, last_verified = ?
                    WHERE id = ?
                    """, (
                        record["amount_benefit"], record["income_criteria"], record["closing_date"],
                        record["application_url"], status, score, json.dumps(breakdown), now_str, sch_id
                    ))
                    cursor.execute("DELETE FROM field_evidence WHERE scholarship_id = ?", (sch_id,))
                    for ev in evidence_list:
                        cursor.execute("""
                        INSERT INTO field_evidence (scholarship_id, field_name, extracted_value, source_snippet, verified_at)
                        VALUES (?, ?, ?, ?, ?)
                        """, (sch_id, ev["field_name"], ev["extracted_value"], ev["source_snippet"], now_str))
                    updated += 1
                else:
                    unchanged += 1
                    cursor.execute("UPDATE scholarships SET last_verified = ? WHERE id = ?", (now_str, sch_id))

            if status == "EXPIRED":
                stale += 1

        cursor.execute("""
        INSERT INTO crawl_runs (run_timestamp, discovered_count, updated_count, unchanged_count, stale_count)
        VALUES (?, ?, ?, ?, ?)
        """, (now_str, discovered_count, updated, unchanged, stale))

        conn.commit()
        conn.close()
        logging.info(
            f"Crawl Completed: {discovered_count} new discovered, {updated} updated, "
            f"{unchanged} unchanged, {stale} stale/expired."
        )


if __name__ == "__main__":
    crawler = LiveWebCrawler()
    crawler.run()
