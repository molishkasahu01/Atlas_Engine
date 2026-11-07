# Atlas Scholarship Intelligence Crawler

Automated scholarship discovery, extraction, verification, confidence scoring, and differential change-detection engine designed for Indian student funding opportunities.

## Live Application
https://atlas-rsn6.onrender.com

## 1. Architecture & Pipeline
The engine executes a 7-stage automated cycle:
1. **Discovery**: Multi-source seed expansion targeting government domains (`.gov.in`, `.ac.in`), CSR foundations, and educational institutions.
2. **Crawling & Ingestion**: Fetches unstructured HTML data, respects robots, extracts anchor trees, and resolves relative links.
3. **Structured Extraction**: Maps extracted attributes to a normalized universal schema (Academic eligibility, income limit, gender, domicile, quota, deadline, benefits).
4. **Authenticity Verification**: Validates primary sources vs aggregator directories via DNS and TLD analysis.
5. **Deterministic Confidence Engine**: Applies a multi-factor mathematical scoring model ($0-100\%$). Requires $\ge 95\%$ for `VERIFIED`.
6. **Differential Change Detection**: Compares incoming crawl records against current database snapshots. Detected changes trigger historical audit logging (`old_value` vs `new_value`).
7. **Storage**: Persists records into SQLite with full field-level evidence provenance.

## 2. Methodology & Scoring Rules
Confidence scoring is computed programmatically without arbitrary generation:
- **Primary Domain Authenticity (30 pts)**: Verified institutional domain (`.gov.in`, `.ac.in`, `.nic.in` or accredited CSR portal).
- **Secure Application Link (15 pts)**: Verified HTTPS endpoint directly targeting the application form.
- **Traceable Source Evidence (20 pts)**: Vital eligibility conditions directly backed by exact source quotations.
- **Deadline Currency (20 pts)**: Automated calendar calculation. Past deadlines automatically flag `EXPIRED`.
- **Schema Completeness & Integrity (15 pts)**: Complete extraction without hallucination.

## 3. Anti-Hallucination Policy
- Missing attributes are saved strictly as `"Not specified"`.
- Every primary constraint is traceable through: `Database` ➔ `Official Source URL` ➔ `Source Snippet` ➔ `Normalized Field`.

## 4. How to Run Locally
```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Run the crawler once to initialize the DB and ingest 20+ scholarships
python crawler.py

# 3. Launch the Streamlit dashboard
streamlit run app.py
```
