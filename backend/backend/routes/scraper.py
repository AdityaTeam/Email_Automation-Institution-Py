"""
LinkedIn Profile Scraper (keyword-accurate, LinkedIn-sourced contact info only)

Pipeline:
  1. Query Google / Bing / Yahoo / DuckDuckGo restricted to site:linkedin.com/in
  2. Keep ONLY profiles whose headline / indexed bio actually contains the keyword
  3. Extract email / phone ONLY from the LinkedIn result itself
  4. Provide Excel export API endpoints for User and Admin panels
"""

from flask import Blueprint, render_template, request, jsonify, session, redirect, url_for, send_file
import os
import re
import io
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import urlparse, urljoin, quote_plus, unquote

import requests
import urllib3
import pandas as pd
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
from dotenv import load_dotenv
from pymongo import MongoClient, UpdateOne
from pymongo.errors import PyMongoError

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

try:
    from ddgs import DDGS
except ImportError:
    from duckduckgo_search import DDGS

try:
    from googlesearch import search as google_search
    HAS_GOOGLESEARCH_LIB = True
except ImportError:
    HAS_GOOGLESEARCH_LIB = False

scraper_bp = Blueprint('scraper', __name__)
load_dotenv()
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB_NAME = os.getenv("MONGO_DB", "signal_scraper")

HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
}

DISQUALIFY_KEYWORDS = (
    "lead generation", "lead gen", "market lead", "marketing", "sales",
    "real estate", "recruiter", "recruitment", "talent acquisition",
    "business development", "digital marketing", "seo",
)

STOPWORDS = {"a", "an", "and", "at", "for", "in", "of", "or", "the", "to", "with", "on"}

CONCEPT_RULES = [
    (re.compile(r"\bgen(?:erative)?[\s\-]?ai\b", re.I), r"gen(?:erative)?[\s\-]?ai"),
    (re.compile(r"\b(?:ml|machine[\s\-]?learning)\b", re.I), r"(?:ml|machine[\s\-]?learning)"),
    (re.compile(r"\b(?:llms?|large[\s\-]language[\s\-]models?)\b", re.I), r"(?:llms?|large[\s\-]language[\s\-]models?)"),
    (re.compile(r"\b(?:nlp|natural[\s\-]language[\s\-]processing)\b", re.I), r"(?:nlp|natural[\s\-]language[\s\-]processing)"),
]

SEARCH_PHRASE_ALTERNATES = {
    "gen ai": ["generative ai", "genai"],
    "genai": ["generative ai", "gen ai"],
    "generative ai": ["genai", "gen ai"],
}

IGNORED_SERP_DOMAINS = (
    "google.", "gstatic.com", "youtube.com", "bing.com", "microsoft.com",
    "duckduckgo.com", "yahoo.com", "schema.org", "wikipedia.org", "w3.org",
    "facebook.com", "twitter.com", "instagram.com", "linkedin.com",
)
NON_PAGE_SCHEMES = ("mailto:", "tel:", "javascript:", "#")

_mongo_client = None


# ==============================================================================
# DATABASE
# ==============================================================================

def get_db():
    global _mongo_client
    if not MONGO_URI:
        raise RuntimeError("MONGO_URI is not set in environment.")
    if _mongo_client is None:
        _mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        _mongo_client.admin.command("ping")
    db = _mongo_client[MONGO_DB_NAME]
    db.linkedin_profiles.create_index("profile_url", unique=True)
    db.sites.create_index("domain", unique=True)
    return db


def save_to_mongo(query: str, user_id: str, profiles: list[dict], sites: list[dict], metrics: dict) -> dict:
    db = get_db()
    now = datetime.now(timezone.utc)

    profile_ops = [
        UpdateOne(
            {"profile_url": p["profile_url"]},
            {
                "$set": {**p, "last_query": query, "last_scraped_at": now, "scraped_by_user_id": user_id},
                "$addToSet": {"scraped_by_users": user_id},
                "$setOnInsert": {"first_scraped_at": now},
            },
            upsert=True,
        )
        for p in profiles if p.get("profile_url")
    ]
    site_ops = [
        UpdateOne(
            {"domain": x["domain"]},
            {
                "$set": {**x, "last_query": query, "last_scraped_at": now, "scraped_by_user_id": user_id},
                "$addToSet": {"scraped_by_users": user_id},
                "$setOnInsert": {"first_scraped_at": now},
            },
            upsert=True,
        )
        for x in sites if x.get("domain")
    ]
    if profile_ops:
        db.linkedin_profiles.bulk_write(profile_ops, ordered=False)
    if site_ops:
        db.sites.bulk_write(site_ops, ordered=False)

    run = db.scrape_runs.insert_one({
        "user_id": user_id,
        "query": query,
        "metrics": metrics,
        "linkedin_profile_urls": [p["profile_url"] for p in profiles],
        "domains": [x["domain"] for x in sites],
        "created_at": now,
    })
    return {"run_id": str(run.inserted_id), "linkedin_profiles_saved": len(profile_ops), "sites_saved": len(site_ops)}


# ==============================================================================
# SEARCH ENGINES
# ==============================================================================

def _search_duckduckgo(query: str, max_results: int = 30) -> list[dict]:
    results = []
    try:
        with DDGS() as ddgs:
            for r in list(ddgs.text(query, max_results=max_results)):
                href = r.get("href") or r.get("url") or ""
                if href.startswith("http"):
                    results.append({"url": href, "title": r.get("title", ""), "snippet": r.get("body", ""), "engine": "DuckDuckGo"})
    except Exception as e:
        print(f"[!] DuckDuckGo Engine Warning: {e}")
    return results


def _search_google(query: str, max_results: int = 30) -> list[dict]:
    results = []
    try:
        if HAS_GOOGLESEARCH_LIB:
            for hit in google_search(query, num_results=max_results, advanced=True):
                href = getattr(hit, 'url', str(hit))
                if href.startswith("http"):
                    results.append({"url": href, "title": getattr(hit, 'title', '') or '', "snippet": getattr(hit, 'description', '') or '', "engine": "Google"})
        else:
            url = f"https://www.google.com/search?q={quote_plus(query)}&num={max_results}"
            resp = requests.get(url, headers=HTTP_HEADERS, timeout=6)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "html.parser")
                for g in soup.select("div.g"):
                    link = g.select_one("a[href]")
                    title = g.select_one("h3")
                    snippet = g.select_one(".VwiC3b") or g.select_one(".st")
                    if link and link.get("href", "").startswith("http"):
                        results.append({"url": link["href"], "title": title.text if title else "", "snippet": snippet.text if snippet else "", "engine": "Google"})
    except Exception as e:
        print(f"[!] Google Engine Warning: {e}")
    return results


def _search_bing(query: str, max_results: int = 30) -> list[dict]:
    results = []
    headers = {**HTTP_HEADERS, "Referer": "https://www.bing.com/"}
    try:
        url = f"https://www.bing.com/search?q={quote_plus(query)}&count={max_results}"
        resp = requests.get(url, headers=headers, timeout=8)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            for item in soup.select("li.b_algo"):
                title_elem = item.select_one("h2 a")
                snippet_elem = item.select_one(".b_caption p") or item.select_one("p")
                if title_elem and title_elem.get("href", "").startswith("http"):
                    href = title_elem["href"]
                    if "bing.com" not in href and "msn.com" not in href:
                        results.append({"url": href, "title": title_elem.text, "snippet": snippet_elem.text if snippet_elem else "", "engine": "Bing"})
    except Exception as e:
        print(f"[!] Bing Engine Error: {e}")
    return results


def _search_yahoo(query: str, max_results: int = 30) -> list[dict]:
    results = []
    sess = requests.Session()
    sess.headers.update(HTTP_HEADERS)
    try:
        url = f"https://search.yahoo.com/search?p={quote_plus(query)}&n={max_results}"
        resp = sess.get(url, timeout=8)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            for item in soup.select("div.algo"):
                title_elem = item.select_one("h3.title a")
                snippet_elem = item.select_one("div.compText") or item.select_one("p")
                if title_elem and title_elem.get("href", "").startswith("http"):
                    raw_url = title_elem["href"]
                    if "/RU=" in raw_url:
                        m = re.search(r'/RU=([^/]+)/', raw_url)
                        if m:
                            raw_url = unquote(m.group(1))
                    if "yahoo.com" not in raw_url:
                        results.append({"url": raw_url, "title": title_elem.text, "snippet": snippet_elem.text if snippet_elem else "", "engine": "Yahoo"})
    except Exception:
        pass
    return results


# ==============================================================================
# KEYWORD MATCHING
# ==============================================================================

def _build_requirements(query: str) -> list[str]:
    text = query.lower().replace('"', ' ')
    found: list[tuple[int, str]] = []

    for rule_re, profile_pattern in CONCEPT_RULES:
        for m in rule_re.finditer(text):
            found.append((m.start(), profile_pattern))
            text = text[:m.start()] + " " * (m.end() - m.start()) + text[m.end():]

    for m in re.finditer(r"[a-z0-9+#.]+", text):
        tok = m.group(0)
        if tok in STOPWORDS or len(tok) < 2:
            continue
        esc = re.escape(tok)
        found.append((m.start(), esc + (r"\w*" if len(tok) >= 5 else r"s?")))

    found.sort(key=lambda x: x[0])
    return [p for _, p in found]


_B_L, _B_R = r"(?<![a-z0-9])", r"(?![a-z0-9])"


def _all_match(text: str, frags: list[str]) -> bool:
    return bool(frags) and bool(text) and all(re.search(_B_L + f + _B_R, text, re.I) for f in frags)


def _phrase_match(text: str, frags: list[str]) -> bool:
    if not frags or not text:
        return False
    return re.search(_B_L + r"[\s\-]+".join(frags) + _B_R, text, re.I) is not None


def _contains_word(text: str, phrase: str) -> bool:
    return re.search(r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])", text, re.I) is not None


def _relevance_score(headline: str, snippet: str, query: str) -> tuple[int, str]:
    frags = _build_requirements(query)
    headline = headline or ""
    snippet = snippet or ""
    q_low = query.lower()

    in_headline = _all_match(headline, frags)
    in_bio = _phrase_match(snippet, frags) or _phrase_match(f"{headline} {snippet}", frags)
    if not (in_headline or in_bio):
        return 0, ""

    if not in_headline:
        for bad in DISQUALIFY_KEYWORDS:
            if _contains_word(headline, bad) and bad not in q_low:
                return 0, ""

    return (3, "headline") if in_headline else (2, "bio")


# ==============================================================================
# CONTACT EXTRACTION
# ==============================================================================

EMAIL_REGEX = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)*\.[a-zA-Z]{2,}'
IGNORED_EMAIL_DOMAINS = ("linkedin.com", "licdn.com", "example.com", "sentry.io")
IMAGE_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.gif', '.svg', '.webp', '.ico', '.pdf', '.css', '.js')

PHONE_PATTERNS = [
    re.compile(r"(?<![\w+])\+\d{1,3}[\s.-]?\(?\d{1,5}\)?(?:[\s.-]?\d{2,5}){2,4}(?!\d)"),
    re.compile(r"(?<!\d)[6-9]\d{4}[\s.-]?\d{5}(?!\d)"),
    re.compile(r"(?<!\d)\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\d)"),
]


def _valid_email(email: str) -> bool:
    email = email.lower().strip()
    if any(email.endswith(ext) for ext in IMAGE_EXTENSIONS):
        return False
    domain = email.rsplit("@", 1)[-1]
    return not any(domain == d or domain.endswith("." + d) for d in IGNORED_EMAIL_DOMAINS)


def _extract_emails(*texts: str) -> set[str]:
    found = set()
    for text in texts:
        if not text:
            continue
        normalized = re.sub(r'\s*[\[(]\s*at\s*[\])]\s*', '@', text, flags=re.I)
        normalized = re.sub(r'\s*[\[(]\s*dot\s*[\])]\s*', '.', normalized, flags=re.I)
        for m in re.findall(EMAIL_REGEX, normalized):
            m = m.lower().rstrip(".")
            if _valid_email(m):
                found.add(m)
    return found


def _valid_phone(raw: str) -> str | None:
    clean = re.sub(r'\s+', ' ', raw.strip(' .-–:()'))
    digits = re.sub(r'\D', '', clean)
    if not 10 <= len(digits) <= 13:
        return None
    if len(set(digits)) <= 2:
        return None
    if digits in "01234567890123456789" or digits in "98765432109876543210":
        return None
    return clean


def _extract_phones(*texts: str) -> set[str]:
    found: dict[str, str] = {}
    for text in texts:
        if not text:
            continue
        for pat in PHONE_PATTERNS:
            for m in pat.finditer(text):
                v = _valid_phone(m.group(0))
                if v:
                    found.setdefault(re.sub(r'\D', '', v)[-10:], v)
    return set(found.values())


def _clean_profile_name(raw_name: str) -> str:
    if not raw_name:
        return "LinkedIn Profile"
    clean = re.sub(r'\(?[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\)?', '', raw_name)
    clean = clean.strip(' -–—()')
    return clean or "LinkedIn Profile"


def _normalize_profile_url(href: str) -> str | None:
    m = re.search(r"linkedin\.com/in/([^/?#\s]+)", href, re.I)
    if not m:
        return None
    return f"https://www.linkedin.com/in/{unquote(m.group(1)).lower()}"


# ==============================================================================
# LINKEDIN DISCOVERY
# ==============================================================================

def _search_phrases(query: str) -> list[str]:
    q = query.strip().replace('"', '')
    phrases = [q]
    ql = q.lower()
    for key, alts in SEARCH_PHRASE_ALTERNATES.items():
        if re.search(r"(?<![a-z0-9])" + re.escape(key) + r"(?![a-z0-9])", ql):
            for alt in alts:
                p = re.sub(re.escape(key), alt, ql, flags=re.I)
                if p not in phrases:
                    phrases.append(p)
            break
    return phrases[:3]


def _run_engines(q: str, max_results: int) -> list[dict]:
    items = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(fn, q, max_results) for fn in (_search_duckduckgo, _search_google, _search_bing, _search_yahoo)]
        for f in as_completed(futures):
            try:
                items.extend(f.result())
            except Exception:
                pass
    return items


def fetch_linkedin_profiles(query: str, max_results: int = 20) -> tuple[list[dict], int]:
    phrases = _search_phrases(query)
    primary = phrases[0]

    variants = [f'site:linkedin.com/in "{p}"' for p in phrases]
    variants.append(f'site:linkedin.com/in "{primary}" "email" OR "gmail.com" OR "@"')
    variants.append(f'site:linkedin.com/in "{primary}" "phone" OR "mobile" OR "+91"')

    candidates: dict[str, dict] = {}
    scanned = 0

    for q in variants:
        for item in _run_engines(q, max_results):
            url = _normalize_profile_url(item.get("url", ""))
            if not url:
                continue
            scanned += 1

            raw_title = (item.get("title") or "").strip()
            snippet = (item.get("snippet") or "").strip()

            parts = re.split(r"\s[-–|]\s", raw_title, maxsplit=1)
            name = _clean_profile_name(parts[0].strip())
            headline = parts[1].strip() if len(parts) > 1 else None
            if headline:
                headline = re.sub(r"\s*[|\-–]\s*LinkedIn\s*$", "", headline, flags=re.I).strip() or None

            score, matched_in = _relevance_score(headline, snippet, query)
            if score == 0:
                continue

            entry = candidates.get(url)
            if entry is None:
                candidates[url] = {
                    "name": name, "headline": headline, "profile_url": url,
                    "snippet": snippet, "_texts": {raw_title, snippet},
                    "relevance_score": score, "matched_in": matched_in,
                    "engine": item.get("engine", "Search Engine"),
                }
            else:
                entry["_texts"].update({raw_title, snippet})
                if len(snippet) > len(entry["snippet"]):
                    entry["snippet"] = snippet
                if not entry["headline"] and headline:
                    entry["headline"] = headline
                if score > entry["relevance_score"]:
                    entry["relevance_score"], entry["matched_in"] = score, matched_in
        time.sleep(0.2)

    profiles = []
    for c in candidates.values():
        texts = [t for t in c.pop("_texts") if t]
        emails = _extract_emails(*texts)
        phones = _extract_phones(*texts)
        profiles.append({
            **c,
            "emails": sorted(emails),
            "contact_number": sorted(phones)[0] if phones else None,
            "contact_numbers": sorted(phones),
            "contact_source": "linkedin_profile_text",
        })

    def sort_key(p):
        contact = (2 if p["emails"] else 0) + (1 if p["contact_numbers"] else 0)
        return (p["relevance_score"], contact)

    profiles.sort(key=sort_key, reverse=True)
    return profiles, scanned


# ==============================================================================
# WEBSITE DISCOVERY & CRAWLING
# ==============================================================================

def fetch_serp_urls(query: str, max_results: int = 30) -> list[str]:
    found = set()
    for item in _run_engines(query, max_results):
        href = item.get("url", "")
        parsed = urlparse(href)
        domain = parsed.netloc.lower()
        if not href.startswith("http") or not domain:
            continue
        if any(ign in domain for ign in IGNORED_SERP_DOMAINS):
            continue
        found.add(f"{parsed.scheme}://{domain}")
    return sorted(found)


def crawl_single_site(base_url: str, max_pages: int = 5) -> dict:
    started = time.time()
    base_url = base_url.rstrip('/')
    domain = urlparse(base_url).netloc
    visited: set[str] = set()
    queue = [base_url, f"{base_url}/contact", f"{base_url}/contact-us", f"{base_url}/about"]
    emails: set[str] = set()
    phones: set[str] = set()

    def is_internal(url: str) -> bool:
        p = urlparse(url)
        return (not p.netloc or p.netloc == domain) and not p.path.lower().endswith(IMAGE_EXTENSIONS)

    sess = requests.Session()
    sess.headers.update(HTTP_HEADERS)

    while queue and len(visited) < max_pages:
        url = queue.pop(0)
        if url.lower().startswith(NON_PAGE_SCHEMES) or url in visited:
            continue
        visited.add(url)
        try:
            resp = sess.get(url, timeout=8, verify=False)
            if resp.status_code != 200 or not resp.text:
                continue
            soup = BeautifulSoup(resp.text, 'html.parser')
            emails |= _extract_emails(resp.text)
            phones |= _extract_phones(soup.get_text(" ", strip=True))
            for a in soup.find_all('a', href=True):
                href = a['href'].strip()
                if href.lower().startswith("mailto:"):
                    addr = href[7:].split("?")[0].strip().lower()
                    if _valid_email(addr):
                        emails.add(addr)
                    continue
                if href.lower().startswith(NON_PAGE_SCHEMES):
                    continue
                full = urljoin(url, href).split('#')[0].rstrip('/')
                if full not in visited and full not in queue and is_internal(full):
                    queue.append(full)
        except Exception:
            pass

    return {
        "domain": domain, "website": base_url, "target_url": base_url,
        "execution_time_seconds": round(time.time() - started, 2),
        "emails": sorted(emails), "emails_count": len(emails),
        "contact_numbers": sorted(phones),
        "pages_visited": sorted(visited),
    }


# ==============================================================================
# EXPORT HELPERS
# ==============================================================================

def _format_profiles_for_excel(profiles: list[dict]) -> pd.DataFrame:
    rows = []
    for p in profiles:
        emails = ", ".join(p.get("emails", [])) if isinstance(p.get("emails"), list) else (p.get("emails") or "")
        phones = ", ".join(p.get("contact_numbers", [])) if isinstance(p.get("contact_numbers"), list) else (p.get("contact_number") or "")

        rows.append({
            "Email": emails if emails else "N/A",
            "Name": p.get("name", "N/A"),
            "Company": p.get("company", "LinkedIn Profile"),
            "Designation": p.get("headline", p.get("designation", "N/A")),
            "Phone": phones if phones else "N/A",
            "LinkedIn": p.get("profile_url", p.get("linkedin", "N/A")),
            "Industry": p.get("industry", "Technology"),
            "Location": p.get("location", "N/A"),
            "Source": p.get("engine", p.get("discovered_via", "Signal Scraper"))
        })

    return pd.DataFrame(rows, columns=[
        "Email", "Name", "Company", "Designation", "Phone", "LinkedIn", "Industry", "Location", "Source"
    ])


# ==============================================================================
# ORCHESTRATION & ROUTES
# ==============================================================================

def run_query_email_scraper(query: str, user_id: str, max_linkedin_results: int = 20,
                            max_serp_results: int = 30, max_pages_per_site: int = 5,
                            max_workers: int = 8, crawl_sites: bool = True):
    start = time.time()

    profiles, scanned = [], 0
    target_urls: list[str] = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        li_future = pool.submit(fetch_linkedin_profiles, query, max_linkedin_results)
        if crawl_sites:
            try:
                target_urls = fetch_serp_urls(query, max_serp_results)
            except Exception as exc:
                print(f"[X] Website discovery failed: {exc}")
        profiles, scanned = li_future.result()

    sites: list[dict] = []
    if target_urls:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futs = {pool.submit(crawl_single_site, u, max_pages_per_site): u for u in target_urls}
            for f in as_completed(futs):
                try:
                    sites.append(f.result())
                except Exception as exc:
                    print(f"[X] Error processing {futs[f]}: {exc}")
    sites.sort(key=lambda x: x["domain"])

    emails = sorted({e for p in profiles for e in p["emails"]})
    phones = sorted({n for p in profiles for n in p["contact_numbers"]})
    elapsed = round(time.time() - start, 2)

    output = {
        "query": query,
        "metrics": {
            "total_linkedin_profiles_found": len(profiles),
            "total_linkedin_results_scanned": scanned,
            "profiles_with_email": sum(1 for p in profiles if p["emails"]),
            "profiles_with_phone": sum(1 for p in profiles if p["contact_numbers"]),
            "total_unique_emails_found": len(emails),
            "total_unique_phones_found": len(phones),
            "total_domains_crawled": len(sites),
            "total_execution_time_seconds": elapsed,
            "total_execution_time_formatted": f"{int(elapsed // 60)}m {round(elapsed % 60, 2)}s",
        },
        "all_emails": emails,
        "all_contact_numbers": phones,
        "linkedin_profiles": profiles,
        "sites_data": sites,
    }

    try:
        storage = save_to_mongo(query, user_id, profiles, sites, output["metrics"])
        output["metrics"]["profiles_saved_to_mongodb"] = storage["linkedin_profiles_saved"]
        output["storage"] = storage
    except (PyMongoError, RuntimeError) as exc:
        print(f"[X] MongoDB save failed: {exc}")
        output["metrics"]["profiles_saved_to_mongodb"] = 0
        output["storage"] = {"error": f"MongoDB save failed: {exc}"}
    return output


@scraper_bp.route('/scraper')
def scraper_page():
    if 'user_id' not in session:
        return redirect(url_for('auth.login'))
    return render_template('user/scraper.html', username=session.get('username', 'User'))


@scraper_bp.route('/api/scrape', methods=['POST'])
def api_scrape():
    if 'user_id' not in session:
        return jsonify({"error": "Unauthorized access"}), 401

    payload = request.get_json(silent=True) or {}
    query = (payload.get("query") or "").strip()
    user_id = str(session['user_id'])
    if not query:
        return jsonify({"error": "A search query is required."}), 400

    def clamp(v, default, lo, hi):
        try:
            return max(lo, min(hi, int(v)))
        except (TypeError, ValueError):
            return default

    try:
        return jsonify(run_query_email_scraper(
            query=query,
            user_id=user_id,
            max_linkedin_results=clamp(payload.get("max_linkedin_results"), 20, 1, 50),
            max_serp_results=clamp(payload.get("max_results"), 30, 1, 100),
            max_pages_per_site=clamp(payload.get("max_pages_per_site"), 5, 1, 15),
            max_workers=clamp(payload.get("max_workers"), 8, 1, 16),
            crawl_sites=bool(payload.get("crawl_sites", True)),
        ))
    except Exception as exc:
        return jsonify({"error": f"Scrape failed: {exc}"}), 500


@scraper_bp.route('/api/export/excel', methods=['POST'])
def export_user_scraped_excel():
    if 'user_id' not in session:
        return jsonify({"error": "Unauthorized"}), 401

    payload = request.get_json(silent=True) or {}
    profiles = payload.get("profiles", [])

    if not profiles:
        db = get_db()
        profiles = list(db.linkedin_profiles.find({"scraped_by_user_id": str(session['user_id'])}).sort("last_scraped_at", -1).limit(500))

    df = _format_profiles_for_excel(profiles)

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Leads')
    output.seek(0)

    return send_file(
        output,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name='scraped_leads_export.xlsx'
    )


@scraper_bp.route('/api/admin/export/excel', methods=['GET', 'POST'])
def export_admin_global_scraped_excel():
    if session.get('role') != 'admin' and not session.get('is_admin'):
        return jsonify({"error": "Admin unauthorized"}), 403

    db = get_db()
    query_filter = {}

    search_term = request.args.get('search', '').strip() or request.args.get('query', '').strip()
    user_filter = request.args.get('user_id', '').strip()
    has_phone = request.args.get('has_phone', '').strip()
    has_email = request.args.get('has_email', '').strip()

    if search_term:
        query_filter["$or"] = [
            {"name": {"$regex": search_term, "$options": "i"}},
            {"headline": {"$regex": search_term, "$options": "i"}},
            {"last_query": {"$regex": search_term, "$options": "i"}}
        ]
    if user_filter and user_filter != "all":
        query_filter["scraped_by_user_id"] = user_filter
    if has_phone == 'true':
        query_filter["contact_numbers.0"] = {"$exists": True}
    elif has_phone == 'false':
        query_filter["contact_numbers"] = {"$size": 0}
    if has_email == 'true':
        query_filter["emails.0"] = {"$exists": True}
    elif has_email == 'false':
        query_filter["emails"] = {"$size": 0}

    profiles = list(db.linkedin_profiles.find(query_filter).sort("last_scraped_at", -1))
    df = _format_profiles_for_excel(profiles)

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Global Leads Repository')
    output.seek(0)

    return send_file(
        output,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name='global_scraped_leads_repository.xlsx'
    )