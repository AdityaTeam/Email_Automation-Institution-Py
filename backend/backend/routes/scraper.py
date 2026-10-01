"""
Scraper Routes & Extraction Engine
Handles public website crawling, DuckDuckGo SERP lookups, and LinkedIn profile extraction.
Now stores user association for all scraped profiles and runs.
"""

from flask import Blueprint, render_template, request, jsonify, session, redirect, url_for
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import requests
import urllib3
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from pymongo import MongoClient, UpdateOne
from pymongo.errors import PyMongoError

try:
    from ddgs import DDGS
except ImportError:
    from duckduckgo_search import DDGS

scraper_bp = Blueprint('scraper', __name__)
load_dotenv()
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB_NAME = os.getenv("MONGO_DB", "signal_scraper")

EMAIL_REGEX = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
PHONE_REGEX = r'(?:\+?\d{1,3}[-.\s]?)?\(?\d{3,5}\)?[-.\s]?\d{3,5}[-.\s]?\d{3,5}'

IGNORED_SERP_DOMAINS = (
    "google.", "gstatic.com", "youtube.com", "bing.com", "microsoft.com",
    "duckduckgo.com", "yahoo.com", "schema.org", "wikipedia.org", "w3.org",
    "facebook.com", "twitter.com", "instagram.com", "linkedin.com"
)

NON_PAGE_SCHEMES = ("mailto:", "tel:", "javascript:", "#")
IGNORED_EMAIL_DOMAINS = ("linkedin.com", "licdn.com", "example.com", "sentry.io")
IMAGE_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.gif', '.svg', '.webp', '.ico', '.pdf', '.css', '.js')

HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

_mongo_client = None

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
    """
    Store all profiles, domain data, and scrape run history bound to the executing user_id.
    """
    db = get_db()
    now = datetime.now(timezone.utc)

    profile_ops = [
        UpdateOne(
            {"profile_url": p["profile_url"]},
            {
                "$set": {
                    **p,
                    "last_query": query,
                    "last_scraped_at": now,
                    "scraped_by_user_id": user_id
                },
                "$addToSet": {"scraped_by_users": user_id},
                "$setOnInsert": {"first_scraped_at": now}
            },
            upsert=True,
        )
        for p in profiles if p.get("profile_url")
    ]
    
    site_ops = [
        UpdateOne(
            {"domain": s["domain"]},
            {
                "$set": {
                    **s,
                    "last_query": query,
                    "last_scraped_at": now,
                    "scraped_by_user_id": user_id
                },
                "$addToSet": {"scraped_by_users": user_id},
                "$setOnInsert": {"first_scraped_at": now}
            },
            upsert=True,
        )
        for s in sites if s.get("domain")
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
        "domains": [s["domain"] for s in sites],
        "created_at": now,
    })

    return {
        "run_id": str(run.inserted_id),
        "linkedin_profiles_saved": len(profile_ops),
        "sites_saved": len(site_ops),
    }


def fetch_serp_urls(query: str, max_results: int = 50) -> list[str]:
    discovered_urls = set()

    def parse_results(results_list):
        for result in results_list:
            href = result.get("href") or result.get("url") or ""
            if not href.startswith("http"):
                continue
            parsed = urlparse(href)
            domain = parsed.netloc.lower()

            if any(ignored in domain for ignored in IGNORED_SERP_DOMAINS):
                continue

            if parsed.scheme in ("http", "https") and domain:
                base_domain_url = f"{parsed.scheme}://{domain}"
                discovered_urls.add(base_domain_url)

    try:
        with DDGS() as ddgs:
            raw_results = list(ddgs.text(query, max_results=max_results))
            parse_results(raw_results)

            if not discovered_urls and '"' in query:
                clean_query = query.replace('"', '')
                raw_results = list(ddgs.text(clean_query, max_results=max_results))
                parse_results(raw_results)
    except Exception as e:
        print(f"[!] Warning during DDGS search execution: {e}")

    return sorted(list(discovered_urls))


def _valid_phone(raw: str) -> str | None:
    if not raw:
        return None
    raw_clean = re.sub(r'\s+', ' ', raw.strip(' .-–:()'))
    digits = re.sub(r'\D', '', raw_clean)
    
    if not 10 <= len(digits) <= 13:
        return None
    if len(set(digits)) <= 2:
        return None
    if digits in "01234567890123456789" or digits in "98765432109876543210":
        return None
    return raw_clean


def _extract_phones(*texts: str) -> set[str]:
    found = set()
    for text in texts:
        if not text:
            continue
        for m in re.finditer(PHONE_REGEX, text):
            v = _valid_phone(m.group(0))
            if v:
                found.add(v)
    return found


def _valid_email(email: str) -> bool:
    email = email.lower().strip()
    if any(email.endswith(ext) for ext in IMAGE_EXTENSIONS):
        return False
    domain = email.rsplit("@", 1)[-1].lower()
    return not any(domain == d or domain.endswith("." + d) for d in IGNORED_EMAIL_DOMAINS)


def _extract_emails_strict(*texts: str) -> set[str]:
    found = set()
    for text in texts:
        for m in re.findall(EMAIL_REGEX, text or "", re.IGNORECASE):
            m = m.lower().rstrip(".")
            if _valid_email(m):
                found.add(m)
    return found


def _clean_profile_name(raw_name: str) -> str:
    if not raw_name:
        return "LinkedIn Profile"
    clean = re.sub(r'\(?[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\)?', '', raw_name)
    clean = clean.strip(' -–—()')
    return clean if clean else "LinkedIn Profile"


def fetch_linkedin_profiles(query: str, max_results: int = 20) -> tuple[list[dict], int]:
    variants = [
        f"site:linkedin.com/in {query}",
        f'site:linkedin.com/in {query} "email" OR "gmail.com" OR "contact"',
        f'site:linkedin.com/in {query} "phone" OR "mobile" OR "+91"',
    ]

    candidates: dict[str, dict] = {}
    try:
        with DDGS() as ddgs:
            for q in variants:
                try:
                    results = list(ddgs.text(q, max_results=max_results))
                except Exception:
                    continue
                for result in results:
                    href = result.get("href") or result.get("url") or ""
                    if "linkedin.com/in/" not in href.lower():
                        continue
                    clean_url = href.split("?")[0].rstrip("/")
                    if clean_url in candidates:
                        continue
                    raw_title = (result.get("title") or "").strip()
                    title_parts = re.split(r"\s[-|]\s", raw_title, maxsplit=1)
                    headline = title_parts[1].strip() if len(title_parts) > 1 else None
                    snippet = (result.get("body") or "").strip()
                    
                    parsed_name = _clean_profile_name(title_parts[0].strip())
                    candidates[clean_url] = {
                        "name": parsed_name,
                        "headline": headline,
                        "profile_url": clean_url,
                        "snippet": snippet,
                        "_raw_title": raw_title,
                    }
                time.sleep(0.3)
    except Exception as e:
        print(f"[!] Warning during LinkedIn search execution: {e}")

    profiles = []
    for c in candidates.values():
        emails = _extract_emails_strict(c["snippet"], c["_raw_title"])
        phones = _extract_phones(c["snippet"], c["_raw_title"])
        profiles.append({
            "name": c["name"],
            "headline": c["headline"],
            "profile_url": c["profile_url"],
            "snippet": c["snippet"],
            "emails": sorted(emails),
            "contact_number": sorted(phones)[0] if phones else None,
            "contact_numbers": sorted(phones),
        })

    def priority_score(p):
        has_email = len(p["emails"]) > 0
        has_phone = len(p["contact_numbers"]) > 0
        if has_email and has_phone:
            return 3
        elif has_phone:
            return 2
        elif has_email:
            return 1
        return 0

    profiles.sort(key=priority_score, reverse=True)
    return profiles, len(candidates)


def crawl_single_site(base_url: str, max_pages: int = 5) -> dict:
    site_start_time = time.time()
    base_url = base_url.rstrip('/')
    domain = urlparse(base_url).netloc

    visited_urls = set()
    urls_to_visit = [
        base_url,
        f"{base_url}/contact",
        f"{base_url}/contact-us",
        f"{base_url}/about",
    ]
    found_emails = set()
    found_phones = set()

    def is_internal_link(url: str) -> bool:
        parsed = urlparse(url)
        if parsed.netloc and parsed.netloc != domain:
            return False
        return not parsed.path.lower().endswith(IMAGE_EXTENSIONS)

    session_req = requests.Session()
    session_req.headers.update(HTTP_HEADERS)

    while urls_to_visit and len(visited_urls) < max_pages:
        current_url = urls_to_visit.pop(0)

        if current_url.lower().startswith(NON_PAGE_SCHEMES) or current_url in visited_urls:
            continue

        visited_urls.add(current_url)

        try:
            resp = session_req.get(current_url, timeout=8, verify=False)
            if resp.status_code != 200 or not resp.text:
                continue

            raw_html = resp.text

            for match in re.findall(EMAIL_REGEX, raw_html, re.IGNORECASE):
                cleaned_match = match.lower().rstrip('.')
                if _valid_email(cleaned_match):
                    found_emails.add(cleaned_match)

            visible_text = BeautifulSoup(raw_html, 'html.parser').get_text(" ", strip=True)
            for m in _extract_phones(visible_text):
                found_phones.add(m)

            soup = BeautifulSoup(raw_html, 'html.parser')
            for anchor in soup.find_all('a', href=True):
                href = anchor['href'].strip()

                if href.lower().startswith("mailto:"):
                    addr = href[7:].split("?")[0].strip()
                    if _valid_email(addr):
                        found_emails.add(addr.lower())
                    continue

                if href.lower().startswith(("tel:", "javascript:", "#")):
                    continue

                full_url = urljoin(current_url, href).split('#')[0].rstrip('/')
                if full_url not in visited_urls and is_internal_link(full_url):
                    if full_url not in urls_to_visit:
                        urls_to_visit.append(full_url)
        except Exception:
            pass

    site_execution_time = round(time.time() - site_start_time, 2)
    sorted_emails = sorted(list(found_emails))

    return {
        "domain": domain,
        "target_url": base_url,
        "execution_time_seconds": site_execution_time,
        "emails_count": len(sorted_emails),
        "emails": sorted_emails,
        "contact_numbers": sorted(found_phones),
        "website": base_url,
        "pages_visited": sorted(list(visited_urls))
    }


def _build_output(query, results, total_execution_time, linkedin_profiles, linkedin_scanned):
    site_emails = {e for site in results for e in site["emails"]}
    linkedin_emails = {e for p in linkedin_profiles for e in p.get("emails", [])}
    combined_unique_emails = sorted(site_emails | linkedin_emails)
    all_numbers = sorted(
        {n for p in linkedin_profiles for n in p.get("contact_numbers", [])}
        | {n for site in results for n in site.get("contact_numbers", [])}
    )
    return {
        "query": query,
        "metrics": {
            "total_domains_crawled": len(results),
            "total_unique_emails_found": len(combined_unique_emails),
            "total_linkedin_profiles_found": len(linkedin_profiles),
            "total_linkedin_profiles_scanned": linkedin_scanned,
            "total_execution_time_seconds": total_execution_time,
            "total_execution_time_formatted": f"{int(total_execution_time // 60)}m {round(total_execution_time % 60, 2)}s",
        },
        "all_emails": combined_unique_emails,
        "all_contact_numbers": all_numbers,
        "linkedin_profiles": linkedin_profiles,
        "sites_data": results,
    }


def run_query_email_scraper(query: str, user_id: str, max_serp_results: int = 50, max_pages_per_site: int = 5,
                            max_workers: int = 8, max_linkedin_results: int = 20):
    total_start_time = time.time()

    linkedin_profiles, linkedin_scanned = [], 0
    with ThreadPoolExecutor(max_workers=2) as discovery_pool:
        linkedin_future = discovery_pool.submit(fetch_linkedin_profiles, query, max_linkedin_results)
        target_urls = fetch_serp_urls(query, max_results=max_serp_results)
        try:
            linkedin_profiles, linkedin_scanned = linkedin_future.result()
        except Exception as exc:
            print(f"[X] LinkedIn discovery failed: {exc}")

    results = []
    if target_urls:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_url = {executor.submit(crawl_single_site, url, max_pages_per_site): url for url in target_urls}
            for future in as_completed(future_to_url):
                url = future_to_url[future]
                try:
                    results.append(future.result())
                except Exception as exc:
                    print(f"[X] Error processing {url}: {exc}")

    total_execution_time = round(time.time() - total_start_time, 2)
    output = _build_output(query, results, total_execution_time, linkedin_profiles, linkedin_scanned)

    try:
        storage = save_to_mongo(query, user_id, linkedin_profiles, results, output["metrics"])
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

    def clamp(value, default, lo, hi):
        try:
            return max(lo, min(hi, int(value)))
        except (TypeError, ValueError):
            return default

    try:
        result = run_query_email_scraper(
            query=query,
            user_id=user_id,
            max_serp_results=clamp(payload.get("max_results"), 30, 1, 100),
            max_pages_per_site=clamp(payload.get("max_pages_per_site"), 5, 1, 15),
            max_workers=clamp(payload.get("max_workers"), 8, 1, 16),
            max_linkedin_results=clamp(payload.get("max_linkedin_results"), 20, 1, 50),
        )
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": f"Scrape failed: {exc}"}), 500