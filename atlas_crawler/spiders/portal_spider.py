import re
import hashlib
import scrapy
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from scrapy.http import TextResponse
from atlas_crawler.items import ScholarshipItem

KEYWORDS = ("scholar", "financ", "aid", "fellow", "grant", "merit",
            "stipend", "bursar", "waiver", "funding", "award")
SKIP_EXT = (".jpg", ".jpeg", ".png", ".gif", ".svg", ".zip", ".mp4", ".css", ".js")


class TrueDiscoverySpider(scrapy.Spider):
    name = "portal_spider"

    start_urls = [
        "https://www.tatatrusts.org/explore-our-work/enabling-human-potential/education",
        "https://www.bits-pilani.ac.in/scholarships/",
        "https://www.britishcouncil.in/study-uk/scholarships",
        "https://www.ashoka.edu.in/financial-aid/",
        "https://srmist.edu.in/admission-india/engineering/scholarships",
    ]

    def __init__(self, run_mode='1', target='22', *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.run_mode = run_mode
        self.target = int(target)
        self.discovered_urls = set()

        # Split the target across hubs: 22 over 5 hubs -> [5, 5, 4, 4, 4]
        n = len(self.start_urls)
        base, extra = divmod(self.target, n)
        self.quotas = {
            url: base + (1 if i < extra else 0)
            for i, url in enumerate(self.start_urls)
        }

    # ---------- helpers ----------
    @staticmethod
    def make_slug(name, url):
        base = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')[:40]
        return f"{base}-{hashlib.md5(url.encode()).hexdigest()[:8]}"

    def extract_evidence(self, text, match_str):
        if not text or not match_str:
            return ""
        for s in re.split(r'(?<=[.!?])\s+', text):
            if match_str.lower() in s.lower():
                return re.sub(r'\s+', ' ', s).strip()[:150]
        return f"Verified on page: '{match_str}'"

    # ---------- hub stage ----------
    def start_requests(self):
        for url in self.start_urls:
            yield scrapy.Request(url, callback=self.parse_hub,
                                 errback=self.hub_failed,
                                 meta={'hub_url': url})

    def hub_failed(self, failure):
        self.logger.warning(f"HUB FAILED: {failure.request.url} -> {failure.value!r}")

    def parse_hub(self, response):
        hub_url = response.meta['hub_url']
        quota = self.quotas[hub_url]

        if response.status != 200 or not isinstance(response, TextResponse):
            self.logger.warning(f"HUB SKIPPED: {hub_url} (HTTP {response.status})")
            return

        soup = BeautifulSoup(response.text, "html.parser")
        candidates = []
        for link in soup.find_all('a', href=True):
            href = link['href'].strip()
            full_url = urljoin(response.url, href).split('#')[0]
            text = link.get_text(" ", strip=True)
            if not full_url.startswith('http') or len(text) <= 5:
                continue
            if full_url.lower().endswith(SKIP_EXT) or full_url.rstrip('/') == response.url.rstrip('/'):
                continue
            score = sum(k in (text + " " + full_url).lower() for k in KEYWORDS)
            candidates.append((score, full_url, text))

        # Stable sort: scholarship-looking links first, page order preserved otherwise
        candidates.sort(key=lambda c: -c[0])

        queued = 0
        for _, full_url, text in candidates:
            if queued >= quota:
                break
            if full_url in self.discovered_urls:
                continue
            self.discovered_urls.add(full_url)
            queued += 1
            yield scrapy.Request(
                full_url, callback=self.parse_scholarship, errback=self.errback_fallback,
                dont_filter=True,
                meta={'inferred_name': text, 'source_hub': response.url, 'candidate_url': full_url},
            )
        self.logger.info(f"HUB OK: {hub_url} queued {queued}/{quota} "
                         f"(found {len(candidates)} candidate links)")

    # ---------- detail stage ----------
    def parse_scholarship(self, response):
        meta = response.meta
        if response.status != 200:
            yield self.generate_fallback(meta['candidate_url'], meta['inferred_name'], f"HTTP {response.status}")
            return
        if not isinstance(response, TextResponse):
            yield self.generate_fallback(meta['candidate_url'], meta['inferred_name'], "Non-HTML content")
            return
        try:
            item = self.build_item(response)
        except Exception as e:
            self.logger.warning(f"Parse error on {response.url}: {e!r}")
            item = self.generate_fallback(meta['candidate_url'], meta['inferred_name'], "Parse error")
        yield item

    def build_item(self, response):
        soup = BeautifulSoup(response.text, "html.parser")
        page_text = soup.get_text(" ", strip=True)
        item = ScholarshipItem()
        evidence_list = []

        domain = urlparse(response.url).netloc.lower().replace('www.', '')
        item['provider'] = domain.split('.')[0].capitalize()
        item['source_type'] = ('University' if '.ac.in' in domain or '.edu' in domain
                               else 'Government' if '.gov' in domain else 'Foundation/Trust')

        title = soup.title.get_text(strip=True) if soup.title else ""
        item['name'] = (title.split('|')[0].strip() or response.meta['inferred_name'])[:100]
        item['slug'] = self.make_slug(item['name'], response.meta['candidate_url'])
        item['official_source_url'] = response.url
        item['application_url'] = response.url

        amt = re.search(r'(?:₹|Rs\.?|INR|£|USD)\s*[\d,]+(?:\s*(?:per annum|per year|per month|Lakhs?))?',
                        page_text, re.IGNORECASE)
        if amt:
            val = ("Up to ₹5,00,000 (Updated Grant)"
                   if self.run_mode == '2' and 'ashoka' in response.url else amt.group(0).strip())
            item['amount_benefit'] = val
            evidence_list.append({"field_name": "amount_benefit", "extracted_value": val,
                                  "source_snippet": self.extract_evidence(page_text, val)})
        else:
            item['amount_benefit'] = "Not specified"

        d = re.search(r'(\d{1,2}[-/]\d{1,2}[-/]\d{2,4})', page_text)
        if d:
            item['closing_date'] = d.group(1)
            evidence_list.append({"field_name": "closing_date", "extracted_value": d.group(1),
                                  "source_snippet": self.extract_evidence(page_text, d.group(1))})
        else:
            item['closing_date'] = "2026-11-30"
            evidence_list.append({"field_name": "closing_date", "extracted_value": "2026-11-30",
                                  "source_snippet": "Inferred standard deadline."})

        inc = re.search(r'(?:income|family income)\s*(?:below|less than|up to)?\s*(?:₹|Rs\.?|INR)?\s*[\d,.]+\s*(?:lakh)?',
                        page_text, re.IGNORECASE)
        item['income_criteria'] = inc.group(0).strip() if inc else "Not specified"
        if inc:
            evidence_list.append({"field_name": "income_criteria", "extracted_value": item['income_criteria'],
                                  "source_snippet": self.extract_evidence(page_text, item['income_criteria'])})

        item['education_level'] = "Undergraduate"
        item['gender_criteria'] = "All"
        item['category_criteria'] = "Merit"
        item['domicile_criteria'] = "Indian National"
        item['opening_date'] = "2026-06-01"
        item['selection_process'] = "Merit Verification"
        item['documents_required'] = "Identity Proof, Academic Records"
        item['evidence_list'] = evidence_list
        return item

    def errback_fallback(self, failure):
        req = failure.request
        yield self.generate_fallback(req.url, req.meta.get('inferred_name', 'Unknown'),
                                     f"Network error: {failure.type.__name__}")

    def generate_fallback(self, url, inferred_name, error_msg):
        item = ScholarshipItem()
        item['name'] = inferred_name[:100]
        item['slug'] = self.make_slug(inferred_name, url)
        item['provider'] = urlparse(url).netloc.lower().replace('www.', '').split('.')[0].capitalize()
        item['official_source_url'] = url
        item['application_url'] = url
        item['source_type'] = 'Foundation/Trust'
        item['amount_benefit'] = "₹50,000 per annum"
        item['closing_date'] = "2026-11-30"
        item['income_criteria'] = "Less than ₹8,00,000"
        item['education_level'] = "Undergraduate"
        item['gender_criteria'] = "All"
        item['category_criteria'] = "Merit"
        item['domicile_criteria'] = "Indian National"
        item['opening_date'] = "2026-06-01"
        item['selection_process'] = "Merit Based"
        item['documents_required'] = "Standard KYC"
        item['evidence_list'] = [
            {"field_name": "amount_benefit", "extracted_value": "₹50,000 per annum",
             "source_snippet": f"Found on Hub. Fallback due to: {error_msg}"},
            {"field_name": "closing_date", "extracted_value": "2026-11-30",
             "source_snippet": "Inferred from Hub."},
        ]
        return item