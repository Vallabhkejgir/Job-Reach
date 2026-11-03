# Job-Reach: Automated Entry-Level Job Scouting, Dual-API Lead Enrichment & Cold Outreach Pipeline

**Job-Reach** is a modular Python automation system that scouts entry-level engineering and data science openings on **Naukri.com** and **LinkedIn**, discovers the appropriate hiring manager or technical recruiter using a **Dual Free-Tier API Failover Router (`Apollo.io` <-> `Hunter.io` + DNS MX Pattern Fallback)**, and dispatches tailored cold outreach emails with strict anti-spam safeguards.

---

## 1. Recommended Technical Stack

| Layer | Technology | Purpose |
| :--- | :--- | :--- |
| **Browser Automation & DOM Parsing** | `Playwright` (async Chromium) + `BeautifulSoup4` / `lxml` | Renders dynamic SPA job boards (Naukri.com & LinkedIn) with stealth headers, rotating user agents, and human-like scroll jitter. |
| **Experience & Seniority Gatekeeper** | Custom Regex & NLP Gate (`src/scrapers/exp_filter.py`) | Enforces strict `0–2 years` (`PRIMARY`) and `1–3 years` (`EDGE_CASE`) bounds while rejecting `>3 years` and any `Senior / Sr. / Lead / Principal / Staff / Manager / Director` roles. |
| **Lead & Email Enrichment** | `Apollo.io API v1` + `Hunter.io API v2` + `dnspython` | Dual free-tier API router with automatic credit exhaustion failover, cross-provider `Name + Domain` handoff, and DNS MX pattern verification. |
| **Outreach Personalization** | `Jinja2` + Optional LLM Hook | Deterministic role-specific email templates for **AI Engineer**, **Data Scientist**, **Software Developer**, and **Backend Engineer**, with optional LLM customization. |
| **Mailing Pipeline** | `smtplib` (TLS) + `google-api-python-client` (Gmail OAuth2) | Supports both Gmail App-Password SMTP (`smtp.gmail.com:587`) and official Gmail REST API (`users.messages.send`) with PDF resume attachment. |
| **State & Quota Ledger** | `SQLite3` (WAL mode) |Zero-credit domain contact caching, persistent `api_quota_ledger` for free-tier credit tracking, and 30-day outreach deduplication. |

---

## 2. Modular Architecture

```text
job-reach/
├── main.py                         # CLI orchestrator (--scrape, --enrich, --draft, --send, --dry-run, --demo)
├── config.yaml                     # Target roles, experience bounds, API priority, and rate-limit settings
├── .env.example                    # Environment template for APOLLO_API_KEY, HUNTER_API_KEY, and SMTP/Gmail OAuth
├── requirements.txt                # Project dependencies
├── src/
│   ├── models.py                   # Typed domain models (JobPosting, DecisionMaker, EnrichmentResult, OutreachDraft)
│   ├── storage.py                  # SQLite WAL persistence, zero-credit cache, and api_quota_ledger
│   ├── scrapers/
│   │   ├── exp_filter.py           # Strict 0-2 yr PRIMARY, 1-3 yr EDGE_CASE, >3 yr & Senior title gatekeeper
│   │   ├── base.py                 # Playwright stealth context, UA rotation, and human scroll/delay jitter
│   │   ├── naukri_scraper.py       # Naukri.com job search & card parser
│   │   └── linkedin_scraper.py     # LinkedIn Jobs search (f_E=1,2) & card/JD parser
│   ├── enrichment/
│   │   ├── apollo_client.py        # Apollo.io v1 (/mixed_people/search & /people/match) + QuotaExhaustedError
│   │   ├── hunter_client.py        # Hunter.io v2 (/domain-search, /email-finder, /email-verifier)
│   │   ├── pattern_fallback.py     # Corporate domain inference, email permutations, and DNS MX verification
│   │   └── router.py               # EnrichmentFailoverRouter (Cache -> Primary API -> Secondary API -> MX Fallback)
│   └── outreach/
│       ├── composer.py             # Hybrid Jinja2 role-specific composer + optional LLM hook
│       └── dispatcher.py           # Dual-transport mail sender (SMTP TLS + Gmail OAuth2) with safety gates
└── tests/
    └── test_job_reach.py           # Comprehensive unit test suite (experience filter, API failover, outreach)
```

---

## 3. Core Capabilities

### 3.1 Experience & Seniority Gatekeeper (`src/scrapers/exp_filter.py`)
1. **Immediate Seniority Rejection**: Any job posting whose title or seniority badge contains `Senior`, `Sr.`, `Sr`, `Lead`, `Principal`, `Staff`, `Architect`, `Manager`, `Director`, `Head`, `VP`, `SDE-III`, `SDE 3`, or LinkedIn `Mid-Senior level / Director / Executive` is immediately classified as `EXCLUDED`—even if the experience field claims `0-2 years` or is blank.
2. **Primary Target (`0–2 years`)**: Listings requiring `0–2 years`, `0–1 year`, `1–2 years`, or `Fresher / Entry-Level` are classified as `PRIMARY` (`is_accepted=True`, `is_edge_case=False`).
3. **Edge Cases (`1–3 years` / `0–3 years`)**: Listings requiring `1–3 years`, `0–3 years`, or `2–3 years` (`min_years <= 2` and `max_years == 3`) are classified as `EDGE_CASE` (`is_accepted=True`, `is_edge_case=True`). The outreach composer automatically includes a proactive ramp-up note for edge-case listings.
4. **Hard Exclusion (`>3 years`)**: Listings requiring `>3 years` (e.g., `2–4 yrs`, `3–5 yrs`, `3+ yrs`, `5+ yrs`) are rejected (`EXCLUDED`).

### 3.2 Dual Free-Tier API Failover (`src/enrichment/router.py`)
Both `APOLLO_API_KEY` and `HUNTER_API_KEY` remain active in `.env`:
- **Tier 0 — Local SQLite Cache (0 Credits)**: Checks `contacts` by `company_domain` and `company` before making any external HTTP call.
- **Tier 1 — Primary API (`Apollo.io` by default)**: Searches for decision makers (`Technical Recruiter`, `Talent Acquisition`, `Engineering Manager`, `Hiring Manager`) and unlocks verified business emails.
- **Tier 2 — Automatic Secondary Failover (`Hunter.io`) & Cross-Provider Handoff**:
  - If the primary API hits its free-tier credit limit (HTTP `429`, HTTP `401/402/403`, or remaining credit counter reaches `0`), `EnrichmentFailoverRouter` marks the provider exhausted in SQLite's `api_quota_ledger` and switches automatically to the secondary API for the current and subsequent jobs.
  - **Cross-Provider Synergy**: If Apollo's search discovers a decision maker's **First Name + Last Name + Company Domain** but cannot unlock the email due to exhausted Apollo email credits, the router hands that exact `Name + Domain` directly to Hunter's `/v2/email-finder` endpoint before falling back to `/v2/domain-search`.
- **Tier 3 — Pattern Permutation + DNS MX Verification**: When both APIs are exhausted, `PatternFallbackEngine` generates standard permutations (`first.last@domain`, `first@domain`, `careers@domain`) and validates domain mail servers via DNS `MX` lookups.

### 3.3 Anti-Scraping & Anti-Spam Best Practices
- **Anti-Scraping**: Playwright context masks `navigator.webdriver`, rotates desktop User-Agents/viewports, scrolls progressively, and sleeps with randomized human-like jitter (`2.0–5.0s`) between page loads.
- **Anti-Spam & Deliverability**:
  - **`--dry-run` by Default**: Never dispatches live emails unless `--send` is explicitly passed without `--dry-run`.
  - **30-Day Recipient Deduplication**: Checks `outreach_log` in SQLite so the same recipient email is never contacted twice within 30 days.
  - **Daily Send Cap & Jitter**: Enforces a configurable daily ceiling (`daily_send_cap: 25`) and sleeps `45–120s` randomly between live sends.

---

## 4. Quick Start & CLI Usage

### 4.1 Installation
```bash
pip install -r requirements.txt
playwright install chromium
cp .env.example .env
```

### 4.2 Run the Offline End-to-End Demo
```bash
python main.py --demo --dry-run
```

### 4.3 Run Live Pipeline Stages
```bash
# 1. Scrape Naukri & LinkedIn, enrich contacts, and preview outreach drafts (Dry-Run)
python main.py --scrape --enrich --draft --dry-run

# 2. Dispatch outreach emails via configured transport (SMTP TLS or Gmail OAuth2)
python main.py --send
```

### 4.4 Run the Unit Test Suite
```bash
pytest -v
```
