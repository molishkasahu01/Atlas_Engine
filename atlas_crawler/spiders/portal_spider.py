import scrapy
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from scrapy.http import TextResponse

from atlas_crawler.items import ScholarshipItem
from crawler import (
    BAD_PATH_TOKENS,
    DEFAULT_TARGET,
    GENERIC_LINK_NAMES,
    NON_SCHOLARSHIP_HOSTS,
    ScholarshipDiscoveryEngine,
    canonicalize_url,
    clean_html_text,
    extract_pdf_text,
    extract_scholarship_record,
    has_verifiable_scholarship_core,
    is_valid_scholarship_page,
    normalize_scholarship_name,
)

KEYWORDS = (
    "scholarship", "fellowship", "financial-aid", "financial-assistance",
    "merit", "stipend", "bursar", "waiver", "endowment",
)
SKIP_EXT = (".jpg", ".jpeg", ".png", ".gif", ".svg", ".zip", ".mp4", ".css", ".js", ".webp")


class TrueDiscoverySpider(scrapy.Spider):
    name = "portal_spider"

    start_urls = [
        ScholarshipDiscoveryEngine.NSP_PORTAL,
        "https://www.ugc.gov.in/Scholarships",
        "https://www.tatatrusts.org/explore-our-work/enabling-human-potential/individual-grants/education-grants",
        "https://www.ashoka.edu.in/admissions/scholarships/",
    ]

    def __init__(self, run_mode="1", target=str(DEFAULT_TARGET), *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.run_mode = run_mode
        self.target = int(target)
        self.discovered_urls = set()
        self.seen_names = set()
        self.emitted_count = 0

    def _build_initial_requests(self):
        # 1. National Scholarship Portal directory (30+ official central government schemes)
        yield scrapy.Request(
            ScholarshipDiscoveryEngine.NSP_PORTAL,
            callback=self.parse_nsp_hub,
            errback=self.hub_failed,
            dont_filter=True,
            priority=10,
            meta={"hub_url": ScholarshipDiscoveryEngine.NSP_PORTAL},
        )

        # 2. Direct authoritative scholarship pages (active INSPIRE-SHE prioritized)
        for idx, meta in enumerate(ScholarshipDiscoveryEngine.DIRECT_PORTALS):
            canon = canonicalize_url(meta["url"])
            if canon in self.discovered_urls:
                continue
            self.discovered_urls.add(canon)
            req_meta = dict(meta, is_pdf=False, candidate_url=meta["url"])
            prio = 10 if "inspire" in meta["url"] else max(1, 5 - idx)
            yield scrapy.Request(
                meta["url"],
                callback=self.parse_scholarship,
                errback=self.errback_skip,
                dont_filter=True,
                priority=prio,
                meta=req_meta,
            )

        # 3. Additional university/foundation hubs for link discovery
        for url in self.start_urls:
            if url == ScholarshipDiscoveryEngine.NSP_PORTAL:
                continue
            yield scrapy.Request(
                url,
                callback=self.parse_hub,
                errback=self.hub_failed,
                dont_filter=True,
                priority=0,
                meta={"hub_url": url},
            )

    async def start(self):
        """Scrapy 2.13+ async entrypoint."""
        for req in self._build_initial_requests():
            yield req

    def start_requests(self):
        """Backward-compatible entrypoint for Scrapy < 2.13."""
        return self._build_initial_requests()

    def hub_failed(self, failure):
        self.logger.warning(f"HUB FAILED: {failure.request.url} -> {failure.value!r}")

    def errback_skip(self, failure):
        self.logger.warning(f"PAGE FAILED (skipped without fallback): {failure.request.url} -> {failure.value!r}")

    def parse_nsp_hub(self, response):
        if response.status != 200 or not isinstance(response, TextResponse):
            self.logger.warning(f"NSP HUB SKIPPED: HTTP {response.status}")
            return

        nsp_candidates = ScholarshipDiscoveryEngine.parse_nsp_directory(response.text, response.url)
        # Prioritize open schemes first, then closed schemes, without skipping valid targets
        nsp_candidates.sort(key=lambda c: (1 if c.get("is_closed") else 0))

        queued = 0
        for meta in nsp_candidates:
            canon = canonicalize_url(meta["url"])
            norm_name = normalize_scholarship_name(meta.get("inferred_name", ""))
            if canon in self.discovered_urls or (norm_name and norm_name in self.seen_names):
                continue
            self.discovered_urls.add(canon)
            meta["candidate_url"] = meta["url"]
            queued += 1
            yield scrapy.Request(
                meta["url"],
                callback=self.parse_scholarship,
                errback=self.errback_skip,
                dont_filter=True,
                priority=2 if meta.get("is_closed") else 8,
                meta=meta,
            )
        self.logger.info(f"NSP HUB OK: queued {queued} distinct central government schemes")

    def parse_hub(self, response):
        hub_url = response.meta["hub_url"]
        if response.status != 200 or not isinstance(response, TextResponse):
            self.logger.warning(f"HUB SKIPPED: {hub_url} (HTTP {response.status})")
            return

        soup = BeautifulSoup(response.text, "html.parser")
        candidates = []
        for link in soup.find_all("a", href=True):
            href = link["href"].strip()
            full_url = urljoin(response.url, href).split("#")[0]
            text = link.get_text(" ", strip=True)
            if not full_url.startswith("http") or len(text) <= 5 or text.lower() in GENERIC_LINK_NAMES:
                continue
            low_u = full_url.lower()
            host = urlparse(full_url).netloc.lower()
            if low_u.endswith(SKIP_EXT) or full_url.rstrip("/") == response.url.rstrip("/"):
                continue
            if any(bad in host for bad in NON_SCHOLARSHIP_HOSTS):
                continue
            if any(tok in low_u for tok in BAD_PATH_TOKENS) or low_u.rstrip("/").endswith("/individual-grants"):
                continue
            score = sum(k in (text.lower() + " " + low_u) for k in KEYWORDS)
            if score == 0:
                continue
            candidates.append((score, full_url, text))

        candidates.sort(key=lambda c: -c[0])
        queued = 0
        for _, full_url, text in candidates[:6]:
            canon = canonicalize_url(full_url)
            if canon in self.discovered_urls:
                continue
            self.discovered_urls.add(canon)
            source_type, provider = ScholarshipDiscoveryEngine.classify_source_type(full_url)
            queued += 1
            yield scrapy.Request(
                full_url,
                callback=self.parse_scholarship,
                errback=self.errback_skip,
                dont_filter=True,
                priority=-5,
                meta={
                    "inferred_name": text,
                    "provider": provider,
                    "source_type": source_type,
                    "candidate_url": full_url,
                    "url": full_url,
                    "is_pdf": full_url.lower().endswith(".pdf"),
                },
            )
        self.logger.info(f"HUB OK: {hub_url} queued {queued} filtered scholarship links")

    def parse_scholarship(self, response):
        if self.emitted_count >= self.target:
            return

        meta = dict(response.meta)
        meta["url"] = response.url if response.status == 200 else meta.get("url", response.url)

        if response.status != 200:
            self.logger.info(f"Rejected {response.url}: HTTP {response.status}")
            return

        pdf_text = ""
        html_text = ""
        page_title = meta.get("inferred_name", "")

        is_pdf_resp = (
            meta.get("is_pdf")
            or response.body.startswith(b"%PDF")
            or b"application/pdf" in response.headers.get(b"Content-Type", b"").lower()
        )

        if is_pdf_resp:
            pdf_text = extract_pdf_text(response.body)
            if len(pdf_text) < 100:
                alt_url = meta.pop("alt_doc_url", "")
                if alt_url and canonicalize_url(alt_url) not in self.discovered_urls:
                    self.discovered_urls.add(canonicalize_url(alt_url))
                    meta["url"] = alt_url
                    meta["candidate_url"] = alt_url
                    yield scrapy.Request(
                        alt_url,
                        callback=self.parse_scholarship,
                        errback=self.errback_skip,
                        dont_filter=True,
                        meta=meta,
                    )
                else:
                    self.logger.info(f"Rejected {response.url}: PDF has insufficient extractable text")
                return
            check_text = pdf_text
        elif isinstance(response, TextResponse):
            page_title, html_text = clean_html_text(response.text)
            supp_url = meta.pop("supplement_url", "")
            if supp_url:
                meta["base_html_text"] = html_text
                meta["base_page_title"] = page_title
                yield scrapy.Request(
                    supp_url,
                    callback=self.parse_supplement_pdf,
                    errback=self.errback_skip,
                    dont_filter=True,
                    meta=meta,
                )
                return
            check_text = html_text
        else:
            self.logger.info(f"Rejected {response.url}: Unsupported non-text response")
            return

        yield from self._finalize_item(meta, page_title, check_text)

    def parse_supplement_pdf(self, response):
        if self.emitted_count >= self.target:
            return
        meta = dict(response.meta)
        base_html_text = meta.pop("base_html_text", "")
        page_title = meta.pop("base_page_title", meta.get("inferred_name", ""))
        supp_text = extract_pdf_text(response.body) if response.status == 200 else ""
        check_text = f"{supp_text}\n\n{base_html_text}".strip()
        yield from self._finalize_item(meta, page_title, check_text)

    def _finalize_item(self, meta: dict, page_title: str, check_text: str):
        is_valid, reason = is_valid_scholarship_page(meta["url"], page_title, check_text)
        if not is_valid:
            self.logger.info(f"Rejected {meta['url']}: {reason}")
            return

        record, evidence_list = extract_scholarship_record(meta, check_text, page_title=page_title)
        if not has_verifiable_scholarship_core(record):
            self.logger.info(f"Rejected {meta['url']}: Lacks extractable amount, deadline, or income/merit criteria")
            return
        norm_name = normalize_scholarship_name(record["name"])
        if norm_name in self.seen_names:
            self.logger.info(f"Duplicate skipped: {record['name']}")
            return
        self.seen_names.add(norm_name)

        item = ScholarshipItem()
        for field in (
            "slug", "name", "provider", "source_type", "official_source_url", "application_url",
            "amount_benefit", "education_level", "income_criteria", "gender_criteria",
            "category_criteria", "domicile_criteria", "opening_date", "closing_date",
            "selection_process", "documents_required",
        ):
            item[field] = record[field]
        item["evidence_list"] = evidence_list

        self.emitted_count += 1
        yield item
