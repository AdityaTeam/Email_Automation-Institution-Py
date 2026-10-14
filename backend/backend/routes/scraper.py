"""
Multi-Platform People Finder (job role + location -> person profiles)

Pipeline:
  1. LinkedIn: public profiles found through search engines.
  2. GitHub (official API), Y Combinator founders, Wellfound profiles: person records
     with name, bio, company, location, links.
  3. Emails / phone numbers come from LinkedIn profiles ONLY (no other source is read).
  4. Standard search (fast), "Load more" (next batch per platform) and Deep Search
     (background job, many query rounds, 100+ profiles).
  5. Results stored in MongoDB; Excel export endpoints.
"""

from flask import Blueprint, render_template, request, jsonify, session, redirect, url_for, send_file
import os
import re
import json
import io
import base64
import time
import threading
import uuid
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import urlparse, quote_plus, unquote

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


CONNECTIONS_500_PLUS_REGEX = re.compile(
    r"(?:500\+\s*(?:connections?|followers?|contacts?)|[1-9]\d{0,2}k\+\s*(?:connections?|followers?)|over\s*500\s*connections?)",
    re.I
)

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
        "linkedin_profile_urls": [p["profile_url"] for p in profiles if p.get("profile_url")],
        "domains": [x["domain"] for x in sites],
        "created_at": now,
    })
    return {"run_id": str(run.inserted_id), "linkedin_profiles_saved": len(profile_ops), "sites_saved": len(site_ops)}



# ==============================================================================
# SEARCH ENGINES (all engines accept `page` so deeper result pages can be read)
# ==============================================================================

def _search_duckduckgo(query: str, max_results: int = 30, page: int = 0) -> list[dict]:
    results = []
    try:
        with DDGS() as ddgs:
            try:
                raw = ddgs.text(query, max_results=max_results, page=page + 1)
            except TypeError:
                # very old duckduckgo_search releases have no paging
                if page:
                    return results
                raw = ddgs.text(query, max_results=max_results)
            for r in list(raw):
                href = r.get("href") or r.get("url") or ""
                if href.startswith("http"):
                    results.append({"url": href, "title": r.get("title", ""), "snippet": r.get("body", ""), "engine": "DuckDuckGo"})
    except Exception as e:
        print(f"[!] DuckDuckGo Engine Warning: {e}")
    return results


def _search_google(query: str, max_results: int = 30, page: int = 0) -> list[dict]:
    results = []
    try:
        if HAS_GOOGLESEARCH_LIB:
            kwargs = {"num_results": max_results, "advanced": True}
            if page:
                kwargs["start_num"] = page * max_results
            try:
                hits = google_search(query, **kwargs)
            except TypeError:
                # installed googlesearch version has no start_num
                if page:
                    return results
                hits = google_search(query, num_results=max_results, advanced=True)
            for hit in hits:
                href = getattr(hit, 'url', str(hit))
                if href.startswith("http"):
                    results.append({"url": href, "title": getattr(hit, 'title', '') or '', "snippet": getattr(hit, 'description', '') or '', "engine": "Google"})
        else:
            url = f"https://www.google.com/search?q={quote_plus(query)}&num={max_results}&start={page * max_results}&hl=en"
            resp = requests.get(url, headers=HTTP_HEADERS, timeout=8)
            if resp.status_code != 200 or "/sorry/" in resp.url or "unusual traffic" in resp.text:
                return results
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


def _unwrap_bing_url(href: str) -> str:
    if "bing.com/ck/a" not in href:
        return href
    m = re.search(r"[?&]u=([^&]+)", href)
    if not m:
        return ""
    token = unquote(m.group(1))
    if token.startswith("a1"):
        token = token[2:]
    token += "=" * (-len(token) % 4)
    try:
        return base64.urlsafe_b64decode(token).decode("utf-8", "ignore")
    except Exception:
        return ""


def _search_bing(query: str, max_results: int = 30, page: int = 0) -> list[dict]:
    results = []
    headers = {**HTTP_HEADERS, "Referer": "https://www.bing.com/"}
    try:
        count = min(max_results, 50)
        url = (f"https://www.bing.com/search?q={quote_plus(query)}&count={count}"
               f"&first={page * count + 1}&setmkt=en-IN&setlang=en")
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code != 200:
            return results
        soup = BeautifulSoup(resp.text, "html.parser")
        for item in soup.select("li.b_algo"):
            title_elem = item.select_one("h2 a")
            snippet_elem = item.select_one(".b_caption p") or item.select_one("p")
            if not title_elem:
                continue
            href = _unwrap_bing_url(title_elem.get("href", ""))
            host = urlparse(href).netloc.lower()
            if href.startswith("http") and "bing.com" not in host and "msn.com" not in host:
                results.append({"url": href, "title": title_elem.get_text(" ", strip=True), "snippet": snippet_elem.get_text(" ", strip=True) if snippet_elem else "", "engine": "Bing"})
    except Exception as e:
        print(f"[!] Bing Engine Error: {e}")
    return results


def _search_yahoo(query: str, max_results: int = 30, page: int = 0) -> list[dict]:
    results = []
    sess = requests.Session()
    sess.headers.update(HTTP_HEADERS)
    try:
        url = f"https://search.yahoo.com/search?p={quote_plus(query)}&n={max_results}&b={page * max_results + 1}&ei=UTF-8"
        resp = sess.get(url, timeout=10)
        if resp.status_code != 200 or "consent" in resp.url:
            return results
        soup = BeautifulSoup(resp.text, "html.parser")
        for item in soup.select("div.algo, div.algo-sr"):
            title_elem = item.select_one("h3.title a") or item.select_one("h3 a") or item.select_one("a[href]")
            snippet_elem = item.select_one("div.compText") or item.select_one("p")
            if not title_elem or not title_elem.get("href", "").startswith("http"):
                continue
            raw_url = title_elem["href"]
            if "/RU=" in raw_url:
                m = re.search(r'/RU=([^/]+)/', raw_url)
                if m:
                    raw_url = unquote(m.group(1))
            if "yahoo.com" not in urlparse(raw_url).netloc:
                results.append({"url": raw_url, "title": title_elem.get_text(" ", strip=True), "snippet": snippet_elem.get_text(" ", strip=True) if snippet_elem else "", "engine": "Yahoo"})
    except Exception as e:
        print(f"[!] Yahoo Engine Error: {e}")
    return results


def _run_engines(q: str, max_results: int, page: int = 0) -> list[dict]:
    items = []
    engines = {
        "DuckDuckGo": _search_duckduckgo,
        "Google": _search_google,
        "Bing": _search_bing,
        "Yahoo": _search_yahoo,
    }
    counts = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(fn, q, max_results, page): name for name, fn in engines.items()}
        for f in as_completed(futures):
            name = futures[f]
            try:
                res = f.result()
            except Exception as e:
                print(f"[!] {name} crashed: {e}")
                res = []
            counts[name] = len(res)
            items.extend(res)
    print(f"[engines] {q[:70]!r} p{page} -> {counts}")
    return items


# ==============================================================================
# KEYWORD & LOCATION RELEVANCE VERIFICATION
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


def _has_500_plus_connections(title: str, snippet: str) -> bool:
    full_text = f"{title} {snippet}"
    if CONNECTIONS_500_PLUS_REGEX.search(full_text):
        return True
    
    low_conn_match = re.search(r"(\d+)\s*connections?", full_text, re.I)
    if low_conn_match and int(low_conn_match.group(1)) < 500:
        return False

    return True


CONNECTIONS_SHOWN_REGEX = re.compile(r"(\d[\d,]*k?\+?)\s*(?:connections?|followers?)", re.I)


def _detect_connections(*texts: str) -> str | None:
    full_text = " ".join(t for t in texts if t)
    m = CONNECTIONS_SHOWN_REGEX.search(full_text)
    if not m:
        return None
    kind = "followers" if re.search(r"followers?", m.group(0), re.I) else "connections"
    return f"{m.group(1)} {kind}"


def _verify_region_match(full_text: str, region: str) -> bool:
    if not region or not region.strip():
        return True

    region_tokens = [re.escape(t.strip()) for t in re.findall(r"[a-zA-Z0-9]+", region) if len(t.strip()) >= 2]
    if not region_tokens:
        return True

    pattern = r"(?<![a-z0-9])" + r"[\s,-]+".join(region_tokens) + r"(?![a-z0-9])"
    return bool(re.search(pattern, full_text, re.I))


def _relevance_score(headline: str, snippet: str, query: str, raw_title: str,
                     require_500: bool = True, region: str = "", loose: bool = False) -> tuple[int, str]:
    """
    Strict mode (default, original behaviour): all role keywords in the headline, or the
    role phrase inside the bio snippet.
    Loose mode (Deep Search / Load more): additionally accepts profiles where at least 60% of
    the role keywords appear somewhere in headline+snippet. These get score 1 / "partial"
    so they always sort below exact matches.
    """
    if require_500 and not _has_500_plus_connections(raw_title, snippet):
        return 0, ""

    full_text = f"{raw_title} {headline or ''} {snippet or ''}"

    if region and not _verify_region_match(full_text, region):
        return 0, ""

    frags = _build_requirements(query)
    headline = headline or ""
    snippet = snippet or ""
    q_low = query.lower()

    in_headline = _all_match(headline, frags)
    in_bio = _phrase_match(snippet, frags) or _phrase_match(f"{headline} {snippet}", frags)
    partial = False
    if not (in_headline or in_bio):
        if not loose:
            return 0, ""
        frac = max(_match_fraction(headline, frags), _match_fraction(f"{headline} {snippet}", frags))
        if frac < 0.6:
            return 0, ""
        partial = True

    if not in_headline:
        for bad in DISQUALIFY_KEYWORDS:
            if _contains_word(headline, bad) and bad not in q_low:
                return 0, ""

    if partial:
        return 1, "partial"
    return (3, "headline") if in_headline else (2, "bio")


# ==============================================================================
# CONTACT EXTRACTION & OWNER VALIDATION
# ==============================================================================

EMAIL_REGEX = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)*\.[a-zA-Z]{2,}'
IGNORED_EMAIL_DOMAINS = ("linkedin.com", "licdn.com", "example.com", "sentry.io", "naukri.com", "indeed.com")
IMAGE_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.gif', '.svg', '.webp', '.ico', '.pdf', '.css', '.js')

PHONE_PATTERNS = [
    re.compile(r"(?<![\w+])\+\d{1,3}[\s.-]?\(?\d{1,5}\)?(?:[\s.-]?\d{2,5}){2,4}(?!\d)"),
    re.compile(r"(?<!\d)[6-9]\d{4}[\s.-]?\d{5}(?!\d)"),
    re.compile(r"(?<!\d)\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\d)"),
]

STRICT_EMAIL_REGEX = re.compile(
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"
)
COMMON_TLDS = {
    "com", "org", "net", "edu", "gov", "mil", "int", "info", "biz", "io", "co", "ai", "app", "dev",
    "me", "us", "uk", "in", "ac", "tech", "xyz", "cloud", "online", "site", "store", "pro", "agency",
    "digital", "solutions", "consulting", "systems", "services", "global", "group", "email",
}


def _valid_email(email: str) -> bool:
    email = email.lower().strip()
    if any(email.endswith(ext) for ext in IMAGE_EXTENSIONS):
        return False
    domain = email.rsplit("@", 1)[-1]
    return not any(domain == d or domain.endswith("." + d) for d in IGNORED_EMAIL_DOMAINS)


def _clean_glued_tld(raw_email: str) -> str | None:
    local, _, domain = raw_email.rpartition("@")
    labels = domain.split(".")
    tld = labels[-1]
    if not (tld.islower() or tld.isupper()):
        m = re.match(r"[A-Za-z][a-z]*", tld)
        tld = m.group(0) if m else tld
    tld = tld.lower()
    if tld not in COMMON_TLDS and len(tld) != 2:
        return None
    return f"{local}@{'.'.join(labels[:-1] + [tld])}".lower()


def _is_email_owner_match(email: str, name: str, slug: str) -> bool:
    if not email or not (name or slug):
        return True

    local_part = email.split('@')[0].lower()
    local_words = [w for w in re.findall(r'[a-zA-Z]{3,}', local_part) if w not in {'gmail', 'yahoo', 'hotmail', 'outlook', 'mail'}]

    name_tokens = [t.lower() for t in re.findall(r'[a-zA-Z]{2,}', name)] if name else []
    slug_tokens = _slug_tokens(slug) if slug else []

    all_owner_tokens = set(name_tokens + slug_tokens)
    if not all_owner_tokens:
        return True

    for token in all_owner_tokens:
        if len(token) >= 3 and (token in local_part or local_part in token):
            return True

    if local_words and not any(w in all_owner_tokens for w in local_words):
        return False

    return True


def _extract_profile_emails(raw_title: str, snippet: str, owner_name: str = "", slug: str = "") -> set[str]:
    found = set()
    for text in [raw_title, snippet]:
        if not text:
            continue
        normalized = re.sub(r'\s*[\[(]\s*at\s*[\])]\s*', '@', text, flags=re.I)
        normalized = re.sub(r'\s*[\[(]\s*dot\s*[\])]\s*', '.', normalized, flags=re.I)

        for m in STRICT_EMAIL_REGEX.finditer(normalized):
            tail = normalized[m.end():m.end() + 2]
            if tail.startswith("…") or tail.startswith(".."):
                continue

            cleaned = _clean_glued_tld(m.group(0).rstrip("."))
            if cleaned and _valid_email(cleaned):
                if _is_email_owner_match(cleaned, owner_name, slug):
                    found.add(cleaned)

    return found


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


def _slug_tokens(slug: str) -> list[str]:
    return [p for p in re.split(r"[-_.]+", slug.lower()) if p.isalpha() and len(p) >= 2]


def _name_matches_slug(name: str, slug: str) -> bool:
    if not name or name == "LinkedIn Profile":
        return False
    name_tokens = {t for t in re.findall(r"[a-z]+", name.lower()) if len(t) >= 2}
    slug_tokens = _slug_tokens(slug)
    if not name_tokens or not slug_tokens:
        return False
    if any(st in name_tokens for st in slug_tokens):
        return True
    return any(nt in st for nt in name_tokens if len(nt) >= 3 for st in slug_tokens)


def _clean_profile_name(raw_name: str) -> str:
    if not raw_name:
        return "Candidate Profile"
    clean = re.sub(r'\(?[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\)?', '', raw_name)
    clean = clean.strip(' -–—()')
    return clean or "Candidate Profile"


def _parse_profile_url(href: str) -> tuple[str, str, bool] | None:
    parsed = urlparse(href)
    host = parsed.netloc.lower()
    if not (host == "linkedin.com" or host.endswith(".linkedin.com")):
        return None
    m = re.match(r"^/in/([^/?#\s]+)((?:/[^?#\s]*)?)$", parsed.path)
    if not m:
        return None
    slug = unquote(m.group(1)).lower()
    rest = [seg for seg in m.group(2).split("/") if seg]
    is_root = not rest or (len(rest) == 1 and re.fullmatch(r"[a-z]{2}(?:-[a-z]{2})?", rest[0], re.I) is not None)
    return f"https://www.linkedin.com/in/{slug}", slug, is_root



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


# ------------------------------------------------------------------------------
# Search-variant "rounds": round 0 is the original query set (fast, most precise).
# Rounds 1..N widen the net (location spellings, seniority words, employers, looser
# keyword windows). Standard search runs round 0, "Load more" runs the next round,
# Deep Search runs every round until the target is reached or the time budget ends.
# ------------------------------------------------------------------------------

VARIANTS_PER_ROUND = 8

DEEP_ROLE_MODIFIERS = (
    "senior", "lead", "junior", "intern", "associate", "principal", "manager", "consultant",
    "freelance", "founder", "architect", "specialist", "head", "director", "trainee", "fresher",
)
DEEP_COMPANY_HINTS = (
    "TCS", "Infosys", "Wipro", "Accenture", "Cognizant", "Capgemini", "IBM", "Microsoft", "Google",
    "Amazon", "Deloitte", "HCL", "Tech Mahindra", "Oracle", "SAP", "Adobe", "Flipkart", "Paytm",
    "Zoho", "Freshworks", "LTIMindtree", "EY", "KPMG", "PwC", "Startup", "Freelancer", "Stealth",
)
YC_TOPIC_MODIFIERS = (
    "AI", "startup", "SaaS", "B2B", "fintech", "healthcare", "developer tools", "machine learning",
    "LLM", "automation", "enterprise", "marketplace", "infrastructure", "security",
)


def _tidy(q: str) -> str:
    return " ".join(q.split())


def _chunk_rounds(base: list[str], pool: list[str], per_round: int = VARIANTS_PER_ROUND) -> list[list[str]]:
    """[[round 0 = base], [round 1], [round 2], ...] with duplicates removed."""
    base = list(dict.fromkeys(_tidy(q) for q in base))
    seen = set(base)
    extra = []
    for q in pool:
        q = _tidy(q)
        if q not in seen:
            seen.add(q)
            extra.append(q)
    return [base] + [extra[i:i + per_round] for i in range(0, len(extra), per_round)]


def _loc_pattern(region: str) -> str:
    r = (region or "").strip()
    return f'("{r} Area" OR "Location * {r}" OR "{r}, India" OR "{r}")' if r else ""


def _location_variants(region: str) -> list[str]:
    """Individual location spellings LinkedIn uses, tried one at a time in deeper rounds."""
    r = (region or "").strip()
    if not r:
        return [""]
    first = re.split(r"[,/|]", r)[0].strip()
    out = [f'"{first} Area"', f'"Greater {first} Area"', f'"{first} Metropolitan Area"']
    out.append(f'"{r}"' if "," in r or "india" in r.lower() else f'"{first}, India"')
    return list(dict.fromkeys(out))


def _linkedin_rounds(query: str, region: str, require_500: bool) -> list[list[str]]:
    phrases = _search_phrases(query)
    primary = phrases[0]
    region = (region or "").strip()
    loc_pattern = _loc_pattern(region)
    conn = ' "500+ connections"' if require_500 else ""
    tokens = [t for t in re.findall(r"[A-Za-z0-9+#.]+", query) if t.lower() not in STOPWORDS]

    # ---- round 0: the original variants, unchanged ----
    base = [f'site:linkedin.com/in "{p}" {loc_pattern}{conn}' for p in phrases]
    base.append(f'site:linkedin.com/in "{primary}" {loc_pattern}{conn} ("email" OR "gmail.com" OR "@")')
    base.append(f'site:linkedin.com/in "{primary}" {loc_pattern}{conn} ("phone" OR "mobile" OR "+91")')
    base.append(f'site:linkedin.com/in "{primary}" {loc_pattern}{conn} ("@gmail.com" OR "@outlook.com" OR "@yahoo.com")')

    # ---- rounds 1..N ----
    pool: list[str] = []
    for loc in _location_variants(region):                       # one location spelling at a time
        for p in phrases:
            pool.append(f'site:linkedin.com/in "{p}" {loc}{conn}')
    if require_500:                                              # same query without the 500+ phrase
        for p in phrases:
            pool.append(f'site:linkedin.com/in "{p}" {loc_pattern}')
    for mod in DEEP_ROLE_MODIFIERS:                              # seniority / flavour words
        pool.append(f'site:linkedin.com/in "{primary}" {loc_pattern}{conn} {mod}')
    for comp in DEEP_COMPANY_HINTS:                              # employers
        pool.append(f'site:linkedin.com/in "{primary}" {loc_pattern}{conn} "{comp}"')
    for w in _query_windows(tokens)[1:]:                         # looser keyword sets
        pool.append(f'site:linkedin.com/in "{w}" {loc_pattern}{conn}')
        pool.append(f'site:linkedin.com/in {w} {loc_pattern}')
    return _chunk_rounds(base, pool)


def fetch_linkedin_profiles(query: str, max_results: int = 20, require_500: bool = True, region: str = "",
                            *, deep: bool = False, round_no: int = 0, exclude=None, target: int = 120,
                            deadline: float | None = None, cancel=None, report=None,
                            diag: dict | None = None) -> tuple[list[dict], int]:
    """
    Returns (profiles, results_scanned).  `diag` receives: summary, next_round, exhausted.
    Contact info (emails / phones) is read ONLY from the LinkedIn search-result title/snippet
    of the profile itself and only when the name matches the profile slug.
    """
    diag = diag if diag is not None else {}
    exclude = {u.lower().rstrip("/") for u in (exclude or [])}
    rounds = _linkedin_rounds(query, region, require_500)
    loose = deep or round_no > 0
    run_rounds = list(range(len(rounds))) if deep else [round_no]
    pages = (0, 1) if deep else (0,)
    per_query = max(max_results, 30) if deep else max_results
    pause = 1.2 if deep else 0.6

    candidates: dict[str, dict] = {}
    scanned = 0
    total_variants = sum(len(rounds[r]) for r in run_rounds if r < len(rounds)) or 1
    done_variants = 0
    completed_rounds = round_no if not deep else 0
    stop_reason = ""

    def should_stop() -> str:
        if cancel is not None and cancel.is_set():
            return "cancelled"
        if deadline is not None and time.time() > deadline:
            return "time budget reached"
        if deep and len(candidates) >= target:
            return "target reached"
        return ""

    def handle(item: dict):
        nonlocal scanned
        parsed_url = _parse_profile_url(item.get("url", ""))
        if not parsed_url:
            return
        url, slug, is_root = parsed_url
        scanned += 1
        if not is_root or url.lower() in exclude:
            return

        raw_title = (item.get("title") or "").strip()
        snippet = (item.get("snippet") or "").strip()

        parts = re.split(r"\s[-–|]\s", raw_title, maxsplit=1)
        name = _clean_profile_name(parts[0].strip())
        headline = parts[1].strip() if len(parts) > 1 else None
        if headline:
            headline = re.sub(r"\s*[|\-–]\s*LinkedIn\s*$", "", headline, flags=re.I).strip() or None

        score, matched_in = _relevance_score(headline, snippet, query, raw_title, require_500, region=region, loose=loose)
        if score == 0:
            return

        owned = _name_matches_slug(name, slug)
        item_emails = _extract_profile_emails(raw_title, snippet, owner_name=name, slug=slug) if owned else set()
        item_phones = _extract_phones(raw_title, snippet) if owned else set()
        conn_text = "500+ connections" if require_500 else (_detect_connections(raw_title, snippet) or "Not shown")

        entry = candidates.get(url)
        if entry is None:
            candidates[url] = {
                "name": name,
                "headline": headline,
                "profile_url": url,
                "snippet": snippet,
                "_emails": set(item_emails),
                "_phones": set(item_phones),
                "relevance_score": score,
                "matched_in": matched_in,
                "engine": item.get("engine", "Search Engine"),
                "platform": "LinkedIn",
                "connections": conn_text,
                "location": region.strip().title() if region else "Global / Unspecified",
            }
        else:
            entry["_emails"].update(item_emails)
            entry["_phones"].update(item_phones)
            if entry.get("connections") == "Not shown" and conn_text != "Not shown":
                entry["connections"] = conn_text
            if len(snippet) > len(entry["snippet"]):
                entry["snippet"] = snippet
            if not entry["headline"] and headline:
                entry["headline"] = headline
            if score > entry["relevance_score"]:
                entry["relevance_score"], entry["matched_in"] = score, matched_in

    for r in run_rounds:
        if r >= len(rounds):
            break
        for q in rounds[r]:
            stop_reason = should_stop()
            if stop_reason:
                break
            for pg in pages:
                for item in _run_engines(q, per_query, pg):
                    handle(item)
                time.sleep(pause)
            done_variants += 1
            if report:
                report(count=len(candidates), fraction=max(done_variants / total_variants,
                                                          (len(candidates) / target) if deep else 0),
                       message=f"LinkedIn: {len(candidates)} profiles found ({done_variants}/{total_variants} searches)")
        if stop_reason:
            break
        completed_rounds = r + 1

    next_round = completed_rounds if deep else round_no + 1
    diag["next_round"] = next_round
    diag["exhausted"] = next_round >= len(rounds)
    diag["summary"] = (f"{len(candidates)} LinkedIn profiles kept from {scanned} results over "
                       f"{done_variants} searches" + (f" (stopped: {stop_reason})" if stop_reason else "") + ".")

    profiles = []
    for c in candidates.values():
        emails = c.pop("_emails")
        phones = c.pop("_phones")
        profiles.append({
            **c,
            "emails": sorted(emails),
            "contact_number": sorted(phones)[0] if phones else None,
            "contact_numbers": sorted(phones),
            "contact_source": "linkedin_profile_text" if (emails or phones) else None,
        })

    def sort_key(p):
        contact = (2 if p["emails"] else 0) + (1 if p["contact_numbers"] else 0)
        return (p["relevance_score"], contact)

    profiles.sort(key=sort_key, reverse=True)
    return profiles, scanned


# ==============================================================================
# PERSON-PROFILE SOURCES (trusted platforms only)
# ------------------------------------------------------------------------------
#   LinkedIn      : public profiles via search index (fetch_linkedin_profiles)
#   GitHub        : official REST API - profile fields only (no contact info read)
#   Y Combinator  : founders listed on official ycombinator.com/companies pages
#   Wellfound     : public candidate profiles (wellfound.com/u/...) via search index
# Naukri / Indeed were dropped: their public pages are job listings and their
# resume databases are login/employer-only, so they expose no person data.
# ==============================================================================

ALL_SOURCES = ("linkedin", "github", "yc", "wellfound")
SOURCE_LABELS = {
    "linkedin": "LinkedIn", "github": "GitHub",
    "yc": "Y Combinator", "wellfound": "Wellfound",
}
GITHUB_API = "https://api.github.com"
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")  # optional, raises API limit from 60 to 5000 req/hour


def _new_person(**kw) -> dict:
    """Common person schema shared by every platform."""
    p = {
        "name": "", "headline": None, "bio": "", "company": None, "location": "",
        "emails": [], "contact_numbers": [], "profile_url": "", "website": None,
        "links": {}, "extra": {}, "platform": "", "engine": "", "source_type": "",
        "contact_source": None, "snippet": "",
    }
    p.update(kw)
    p["emails"] = sorted(set(p["emails"]))
    p["contact_numbers"] = sorted(set(p["contact_numbers"]))
    p["contact_number"] = p["contact_numbers"][0] if p["contact_numbers"] else None
    return p


def _location_matches(text: str, region: str) -> bool:
    """Most specific part of the region (text before the first comma) must appear in text."""
    if not region or not region.strip():
        return True
    if not text:
        return False
    first = re.split(r"[,/|]", region)[0].strip()
    if len(first) < 2:
        return True
    return re.search(r"(?<![a-z0-9])" + re.escape(first) + r"(?![a-z0-9])", text, re.I) is not None


def _contact_score(p: dict) -> int:
    return (2 if p.get("emails") else 0) + (1 if p.get("contact_numbers") else 0)


def _warn(warn, msg: str):
    if warn is not None and msg not in warn:
        warn.append(msg)


def _match_fraction(text: str, frags: list[str]) -> float:
    """Share of required role keywords found in text (1.0 = all)."""
    if not frags or not text:
        return 0.0
    return sum(1 for f in frags if re.search(_B_L + f + _B_R, text, re.I)) / len(frags)


def _query_windows(tokens: list[str]) -> list[str]:
    """Progressively looser keyword sets: full, then n-1 contiguous words, then single words (2-word roles)."""
    out = [" ".join(tokens)] if tokens else []
    n = len(tokens)
    if n >= 3:
        out += [" ".join(tokens[:-1]), " ".join(tokens[1:])]
    elif n == 2:
        out += tokens
    return out


def _walk_for_key(obj, key):
    if isinstance(obj, dict):
        if key in obj:
            yield obj
        for v in obj.values():
            yield from _walk_for_key(v, key)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_for_key(v, key)


# ---------------------------------------------------------------- GitHub ------

def _github_get(path: str, params: dict | None = None, warn: list | None = None):
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "people-finder"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    try:
        resp = requests.get(f"{GITHUB_API}{path}", headers=headers, params=params, timeout=10)
    except requests.RequestException as exc:
        _warn(warn, f"GitHub could not be reached: {exc}")
        return None
    if resp.status_code == 200:
        return resp.json()
    if resp.status_code == 401:
        _warn(warn, "GitHub rejected GITHUB_TOKEN (401) - check the token in .env.")
    elif resp.status_code in (403, 429):
        hint = "" if GITHUB_TOKEN else " Add GITHUB_TOKEN to .env (without it the limit is 60 requests/hour)."
        _warn(warn, "GitHub API rate limit reached." + hint)
    elif resp.status_code == 422:
        _warn(warn, f"GitHub rejected the search query: {resp.text[:120]}")
    elif resp.status_code != 404:
        _warn(warn, f"GitHub API returned HTTP {resp.status_code}.")
    return None




# ------------------------------------------------------------------------------
# Contact details: LinkedIn ONLY.
# GitHub / Y Combinator / Wellfound records never carry an email or phone of their own.
# If such a person links to their LinkedIn profile, the contact info shown on that
# LinkedIn profile (as exposed in LinkedIn's public search snippet) is attached.
# ------------------------------------------------------------------------------

_LINKEDIN_IN_TEXT_RE = re.compile(r"(?:https?://)?(?:[a-z]{2,3}\.)?linkedin\.com/in/[^\s/?#,;)\]]+", re.I)


def _find_linkedin_url(*texts: str) -> str | None:
    for t in texts:
        m = _LINKEDIN_IN_TEXT_RE.search(t or "")
        if m:
            u = m.group(0)
            return u if u.lower().startswith("http") else "https://" + u
    return None


def _linkedin_contact_lookup(person: dict) -> tuple[set[str], set[str]]:
    li = (person.get("links") or {}).get("linkedin") or ""
    parsed = _parse_profile_url(li) if li else None
    if not parsed:
        return set(), set()
    _, slug, _ = parsed
    name = person.get("name") or ""
    if not _name_matches_slug(name, slug):      # never attach contacts to a possibly different person
        return set(), set()
    emails: set[str] = set()
    phones: set[str] = set()
    for engine in (_search_bing, _search_duckduckgo):
        try:
            hits = engine(f"site:linkedin.com/in/{slug}", 10)
        except Exception:
            hits = []
        for hit in hits:
            p = _parse_profile_url(hit.get("url", ""))
            if not p or p[1] != slug:
                continue
            title, snippet = hit.get("title") or "", hit.get("snippet") or ""
            emails |= _extract_profile_emails(title, snippet, owner_name=name, slug=slug)
            phones |= _extract_phones(title, snippet)
        if emails or phones:
            break
    return emails, phones


def enrich_contacts_from_linkedin(people: list[dict], limit: int = 30, cancel=None) -> int:
    """Attach LinkedIn-profile contact info to non-LinkedIn people that link to a LinkedIn profile."""
    targets = [p for p in people if (p.get("links") or {}).get("linkedin")][:limit]
    hit_count = 0
    if not targets:
        return 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = {pool.submit(_linkedin_contact_lookup, p): p for p in targets}
        for f in as_completed(futs):
            if cancel is not None and cancel.is_set():
                break
            p = futs[f]
            try:
                emails, phones = f.result()
            except Exception:
                continue
            if emails or phones:
                p["emails"] = sorted(set(p.get("emails", [])) | emails)
                p["contact_numbers"] = sorted(set(p.get("contact_numbers", [])) | phones)
                p["contact_number"] = p["contact_numbers"][0] if p["contact_numbers"] else None
                p["contact_source"] = "linkedin_profile_text"
                hit_count += 1
    return hit_count


# ---------------------------------------------------------------- GitHub ------

def fetch_github_people(query: str, max_results: int = 15, region: str = "",
                        warn: list | None = None, diag: dict | None = None, *,
                        round_no: int = 0, exclude=None, deep: bool = False,
                        cancel=None, report=None) -> list[dict]:
    """`round_no` is the zero-based GitHub search page. Deep Search reads 3 pages per query."""
    diag = diag if diag is not None else {}
    exclude = {u.lower().rstrip("/") for u in (exclude or [])}
    seg = region.split(",")[0].strip() if region else ""
    loc_q = f' location:"{seg}"' if seg else ""
    tokens = [t for t in re.findall(r"[A-Za-z0-9+#.]+", query) if t.lower() not in STOPWORDS]

    queries = [f'"{p}" in:bio type:user{loc_q}' for p in _search_phrases(query)]
    queries += [f"{w} in:bio type:user{loc_q}" for w in _query_windows(tokens)]
    queries = list(dict.fromkeys(queries))
    if not GITHUB_TOKEN:
        queries = queries[:3]          # unauthenticated search API allows only ~10 calls/minute

    pages = list(range(round_no, round_no + 3)) if deep else [round_no]
    pause = 0.3 if GITHUB_TOKEN else 1.2

    logins: list[str] = []
    for q in queries:
        for pg in pages:
            if cancel is not None and cancel.is_set():
                break
            data = _github_get("/search/users", {"q": q, "per_page": 50 if deep else 30, "page": pg + 1}, warn)
            items = (data or {}).get("items", [])
            for item in items:
                login = item.get("login")
                if item.get("type") == "User" and login and login not in logins \
                        and f"https://github.com/{login}".lower() not in exclude:
                    logins.append(login)
            time.sleep(pause)
            if not items:
                break
    found_total = len(logins)

    if GITHUB_TOKEN:
        cap = max_results * 2
    else:
        cap = 25 if deep else 15
        if deep:
            _warn(warn, "GitHub: without GITHUB_TOKEN only ~25 profiles per hour can be checked. "
                        "Add GITHUB_TOKEN to .env to get many more GitHub results in Deep Search.")
    logins = logins[:cap]
    frags = _build_requirements(query)
    progress_done = 0

    def build(login: str):
        d = _github_get(f"/users/{login}", None, warn)
        if not d:
            return None, "unavailable"
        bio = (d.get("bio") or "").strip()
        company = (d.get("company") or "").strip().lstrip("@") or None
        location = (d.get("location") or "").strip()
        rel = _match_fraction(f"{bio} {company or ''}", frags)
        if rel < 0.5:
            return None, "role"
        if region and not _location_matches(location, region):
            return None, "location"

        name = (d.get("name") or "").strip() or d["login"]
        website = (d.get("blog") or "").strip() or None
        if website and not re.match(r"^https?://", website, re.I):
            website = "https://" + website

        links = {}
        if d.get("twitter_username"):
            links["twitter"] = f"https://x.com/{d['twitter_username']}"
        li = _find_linkedin_url(d.get("blog"), bio)       # only used to look up LinkedIn contact info
        if li:
            links["linkedin"] = li
        return _new_person(
            name=name, headline=bio[:140] or None, bio=bio, company=company, location=location,
            emails=[], contact_numbers=[], profile_url=d["html_url"], website=website, links=links,
            extra={"followers": d.get("followers", 0), "public_repos": d.get("public_repos", 0),
                   "hireable": d.get("hireable"), "relevance": rel},
            platform="GitHub", engine="GitHub REST API", source_type="official_api",
            contact_source=None, snippet=bio,
        ), "ok"

    people: list[dict] = []
    reasons = {"ok": 0, "role": 0, "location": 0, "unavailable": 0}
    with ThreadPoolExecutor(max_workers=5) as pool:
        for fut in as_completed([pool.submit(build, lg) for lg in logins]):
            if cancel is not None and cancel.is_set():
                break
            try:
                person, why = fut.result()
            except Exception as exc:
                print(f"[!] GitHub profile error: {exc}")
                continue
            reasons[why] += 1
            progress_done += 1
            if person:
                people.append(person)
            if report:
                report(count=len(people), fraction=progress_done / max(len(logins), 1),
                       message=f"GitHub: {len(people)} matching users ({progress_done}/{len(logins)} checked)")
    people.sort(key=lambda p: (p["extra"]["relevance"], p["extra"].get("followers", 0)), reverse=True)
    diag["next_round"] = round_no + (3 if deep else 1)
    diag["exhausted"] = found_total == 0
    diag["summary"] = (f"{found_total} new users matched on GitHub search; checked {len(logins)}: {reasons['ok']} kept, "
                       f"{reasons['role']} role mismatch, {reasons['location']} other location, "
                       f"{reasons['unavailable']} could not be loaded.")
    return people[:max_results]


# ---------------------------------------------------------- Y Combinator ------

_YC_RESERVED = {"founders", "industry", "location", "batch", "tags", "directory"}


def _yc_company_people(url: str, region: str) -> list[dict]:
    resp = requests.get(url, headers=HTTP_HEADERS, timeout=10)
    if resp.status_code != 200 or not resp.text:
        return []
    node = BeautifulSoup(resp.text, "html.parser").find(attrs={"data-page": True})
    if not node:
        return []
    try:
        data = json.loads(node["data-page"])
    except ValueError:
        return []

    people = []
    for holder in _walk_for_key(data, "founders"):
        founders = holder.get("founders")
        if not (isinstance(founders, list) and founders and isinstance(founders[0], dict)):
            continue
        company = (holder.get("name") or "").strip()
        location = f"{holder.get('location') or ''} {holder.get('all_locations') or ''}".strip()
        if region and not _location_matches(location, region):
            return []
        one_liner = (holder.get("one_liner") or "").strip()
        for f in founders:
            fname = (f.get("full_name") or "").strip()
            if not fname:
                continue
            title = (f.get("title") or "Founder").strip()
            bio = (f.get("founder_bio") or f.get("bio") or "").strip()
            links = {}
            if f.get("linkedin_url"):
                links["linkedin"] = f["linkedin_url"]
            if f.get("twitter_url"):
                links["twitter"] = f["twitter_url"]
            slug = re.sub(r"[^a-z0-9]+", "-", fname.lower()).strip("-")
            people.append(_new_person(
                name=fname, headline=f"{title} at {company}" if company else title,
                bio=bio or one_liner, company=company or None, location=location,
                emails=[], contact_numbers=[],            # contact info comes from LinkedIn only
                profile_url=f"{url}#{slug}", website=None, links=links,
                platform="Y Combinator", engine="ycombinator.com company page",
                source_type="official_site", contact_source=None,
                snippet=one_liner,
            ))
        break
    return people


def _yc_rounds(query: str, region: str) -> list[list[str]]:
    loc_str = f'"{region.strip()}"' if region and region.strip() else ""
    tokens = " ".join(t for t in re.findall(r"[A-Za-z0-9+#.]+", query) if t.lower() not in STOPWORDS)
    phrases = _search_phrases(query)
    base = [f'site:ycombinator.com/companies "{p}" {loc_str}' for p in phrases[:2]]
    base.append(f'site:ycombinator.com/companies {tokens} {loc_str}')
    pool = [f'site:ycombinator.com/companies {tokens} {m} {loc_str}' for m in YC_TOPIC_MODIFIERS]
    pool.append(f'site:ycombinator.com/companies "{phrases[0]}"')
    pool.append(f'site:ycombinator.com/companies {tokens}')
    return _chunk_rounds(base, pool, 6)


def fetch_yc_people(query: str, max_results: int = 15, region: str = "", *,
                    round_no: int = 0, exclude=None, deep: bool = False,
                    diag: dict | None = None, cancel=None, report=None) -> list[dict]:
    diag = diag if diag is not None else {}
    exclude = set(exclude or [])
    rounds = _yc_rounds(query, region)
    run_rounds = list(range(len(rounds))) if deep else [round_no]

    pages: list[str] = []
    for r in run_rounds:
        if r >= len(rounds):
            break
        for q in rounds[r]:
            if cancel is not None and cancel.is_set():
                break
            for item in _run_engines(q, max_results):
                parsed = urlparse(item.get("url", ""))
                if not (parsed.netloc.lower() in ("ycombinator.com", "www.ycombinator.com")):
                    continue
                m = re.match(r"^/companies/([a-z0-9._-]+)/?$", parsed.path, re.I)
                if not m or m.group(1).lower() in _YC_RESERVED:
                    continue
                page = f"https://www.ycombinator.com/companies/{m.group(1).lower()}"
                if page not in pages:
                    pages.append(page)
            time.sleep(0.6)
            if report:
                report(count=len(pages), fraction=0.5, message=f"Y Combinator: {len(pages)} company pages found")

    cap = 60 if deep else max_results
    people: list[dict] = []
    done = 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = [pool.submit(_yc_company_people, u, region) for u in pages[:cap]]
        for f in as_completed(futs):
            done += 1
            try:
                people.extend(p for p in f.result() if p["profile_url"] not in exclude)
            except Exception as exc:
                print(f"[!] YC page error: {exc}")
            if report:
                report(count=len(people), fraction=0.5 + 0.5 * done / max(len(futs), 1),
                       message=f"Y Combinator: {len(people)} founders ({done}/{len(futs)} companies read)")
    next_round = (len(rounds) if deep else round_no + 1)
    diag["next_round"] = next_round
    diag["exhausted"] = next_round >= len(rounds)
    diag["summary"] = f"{len(pages)} YC company pages found; {len(people)} new founders kept."
    return people[: max_results * 3]


# ------------------------------------------------------------ Wellfound -------
# Wellfound keeps most candidate profiles behind a login, so search engines index only
# the few profiles people made public. Strategy: run the precise queries first; if they
# find nothing, automatically fall back to broader queries (no location in the query, no
# site:/path operator, angel.co legacy URLs) and a relaxed role filter.

_WELLFOUND_PATH_RE = re.compile(r"^/(u|p)/([^/?#\s]+)(?:/.*)?$")


def _wellfound_rounds(query: str, region: str) -> tuple[list[list[str]], list[str]]:
    region = (region or "").strip()
    phrases = _search_phrases(query)
    primary = phrases[0]
    tokens = [t for t in re.findall(r"[A-Za-z0-9+#.]+", query) if t.lower() not in STOPWORDS]
    tok = " ".join(tokens)
    loc_q = f'"{region}"' if region else ""

    base = [f'site:wellfound.com/u "{p}" {loc_q}' for p in phrases]
    for w in _query_windows(tokens)[:3]:
        base.append(f'site:wellfound.com/u {w} {region}')
    base.append(f'site:wellfound.com {tok} {region} profile')

    fallback = [
        f'site:wellfound.com/u {tok}',
        f'site:wellfound.com/u "{primary}"',
        f'site:angel.co/u {tok} {region}',
        f'inurl:wellfound.com/u "{primary}"',
        f'wellfound.com/u "{primary}" {region}',
        f'"{primary}" wellfound profile {region}',
        f'"{primary}" "wellfound" candidate {region}',
    ]
    pool = [f'site:wellfound.com/u {tok} {m} {region}' for m in DEEP_ROLE_MODIFIERS]
    pool += [f'site:wellfound.com/u "{primary}" "{c}"' for c in DEEP_COMPANY_HINTS[:12]]
    return _chunk_rounds(base, pool), [_tidy(q) for q in fallback]


def fetch_wellfound_people(query: str, max_results: int = 15, region: str = "",
                           warn: list | None = None, diag: dict | None = None, *,
                           round_no: int = 0, exclude=None, deep: bool = False,
                           cancel=None, report=None) -> list[dict]:
    diag = diag if diag is not None else {}
    exclude = {u.lower().rstrip("/") for u in (exclude or [])}
    region = (region or "").strip()
    rounds, fallback = _wellfound_rounds(query, region)
    run_rounds = list(range(len(rounds))) if deep else [round_no]
    frags = _build_requirements(query)

    candidates: dict[str, dict] = {}
    stats = {"results": 0, "not_profile": 0, "role": 0, "unverified_loc": 0, "queries": 0}

    def run_variants(variants: list[str], relaxed: bool):
        for q in variants:
            if cancel is not None and cancel.is_set():
                return
            stats["queries"] += 1
            for item in _run_engines(q, max(max_results, 20)):
                stats["results"] += 1
                parsed = urlparse(item.get("url", ""))
                host = parsed.netloc.lower()
                m = _WELLFOUND_PATH_RE.match(parsed.path)
                if not (host == "wellfound.com" or host.endswith(".wellfound.com") or host == "angel.co" or host.endswith(".angel.co")) or not m:
                    stats["not_profile"] += 1
                    continue
                kind, slug = m.group(1), unquote(m.group(2)).lower()
                url = f"https://wellfound.com/{kind}/{slug}"
                if url in candidates or url.lower() in exclude:
                    continue

                raw_title = (item.get("title") or "").strip()
                snippet = (item.get("snippet") or "").strip()
                clean_title = re.sub(r"\s*[|\-–]\s*(?:Wellfound|AngelList)[^|]*$", "", raw_title, flags=re.I)
                parts = re.split(r"\s[-–|]\s", clean_title, maxsplit=1)
                name = _clean_profile_name(re.sub(r"['’]s (?:profile|resume)$", "", parts[0].strip(), flags=re.I))
                headline = parts[1].strip() if len(parts) > 1 else None

                text = f"{raw_title} {snippet}"
                rel = _match_fraction(text, frags)
                if rel < (0.01 if relaxed else 0.5):
                    stats["role"] += 1
                    continue
                loc_ok = _location_matches(text, region) if region else True
                if not loc_ok:
                    stats["unverified_loc"] += 1

                candidates[url] = _new_person(
                    name=name, headline=headline, bio=snippet,
                    location=(region.title() if (region and loc_ok) else ""),
                    emails=[], contact_numbers=[], profile_url=url,    # contact info comes from LinkedIn only
                    extra={"location_verified": bool(loc_ok), "relevance": rel},
                    platform="Wellfound", engine=item.get("engine", "Search Engine"),
                    source_type="search_index", contact_source=None, snippet=snippet,
                )
            time.sleep(1.0 if deep else 0.6)
            if report:
                report(count=len(candidates), fraction=min(0.95, stats["queries"] / 20),
                       message=f"Wellfound: {len(candidates)} profiles found ({stats['queries']} searches)")

    for r in run_rounds:
        if r >= len(rounds):
            break
        run_variants(rounds[r], relaxed=(round_no > 0 or deep))
    used_fallback = False
    if not candidates and not (cancel is not None and cancel.is_set()):
        used_fallback = True
        run_variants(fallback, relaxed=True)

    people = list(candidates.values())
    people.sort(key=lambda p: (p["extra"]["location_verified"], p["extra"]["relevance"]), reverse=True)
    next_round = len(rounds) if deep else round_no + 1
    diag["next_round"] = next_round
    diag["exhausted"] = next_round >= len(rounds)
    diag["summary"] = (f"{stats['queries']} searches{' (incl. broader fallback queries)' if used_fallback else ''}, "
                       f"{stats['results']} results; {len(people)} Wellfound profiles kept "
                       f"({stats['unverified_loc']} without a location in the snippet), "
                       f"{stats['not_profile']} were not profile pages, {stats['role']} role mismatch.")
    if stats["results"] == 0:
        _warn(warn, "Wellfound: the search engines returned nothing (they may be blocking automated queries).")
    elif not people and round_no == 0:
        _warn(warn, "Wellfound: search engines returned pages, but none were public candidate profiles for this role. "
                    "Wellfound hides most profiles behind login, so few or none are indexed.")
    return people[: max(max_results, 100) if deep else max_results]


# ==============================================================================
# EXPORT HELPERS
# ==============================================================================

def _format_profiles_for_excel(profiles: list[dict]) -> pd.DataFrame:
    def join(v):
        return ", ".join(v) if isinstance(v, list) else (v or "")

    rows = []
    for p in profiles:
        emails = join(p.get("emails"))
        phones = join(p.get("contact_numbers")) or (p.get("contact_number") or "")
        rows.append({
            "Platform": p.get("platform", "LinkedIn"),
            "Name": p.get("name", "N/A"),
            "Role / Headline": p.get("headline") or p.get("designation") or "N/A",
            "Company": p.get("company") or "N/A",
            "Bio": p.get("bio") or p.get("snippet") or "N/A",
            "Email": emails or "N/A",
            "Phone": phones or "N/A",
            "Location": p.get("location") or "N/A",
            "Profile Link": p.get("profile_url", p.get("linkedin", "N/A")),
            "Website": p.get("website") or "N/A",
            "Source": p.get("engine", p.get("discovered_via", "Signal Scraper")),
            "Contact Source": "LinkedIn profile" if (emails or phones) else "N/A",
        })

    return pd.DataFrame(rows, columns=[
        "Platform", "Name", "Role / Headline", "Company", "Bio", "Email", "Phone",
        "Location", "Profile Link", "Website", "Source", "Contact Source",
    ])




# ==============================================================================
# ORCHESTRATION
# ==============================================================================

DEEP_TARGET_DEFAULT = 120
DEEP_TIME_BUDGET_SECONDS = int(os.getenv("DEEP_SEARCH_BUDGET_SECONDS", "600"))
JOB_TTL_SECONDS = 3600


def run_query_email_scraper(query: str, user_id: str, region: str = "",
                            max_linkedin_results: int = 20, require_500: bool = True,
                            sources: list | None = None, deep: bool = False,
                            target: int = DEEP_TARGET_DEFAULT, more: str | None = None,
                            round_no: int = 0, exclude: list | None = None,
                            progress=None, cancel=None):
    """
    Modes
      standard : round 0 of every selected platform (fast).
      deep     : every search round, 2 result pages per query, loose matching; keeps going until
                 `target` LinkedIn profiles are found or DEEP_TIME_BUDGET_SECONDS is used up.
      more     : `more="<platform>"` + `round_no` -> next batch for ONE platform, skipping `exclude` URLs.
    Emails / phone numbers are taken from LinkedIn profiles only.
    """
    start = time.time()
    selected = [s for s in (sources or ALL_SOURCES) if s in ALL_SOURCES] or list(ALL_SOURCES)
    if more in ALL_SOURCES:
        selected = [more]
        deep = False
    rnd = round_no if more else 0
    exclude_set = set(exclude or [])
    deadline = start + DEEP_TIME_BUDGET_SECONDS if deep else None
    full_search_query = f"{query} {region}".strip() if region else query

    found: dict[str, list[dict]] = {k: [] for k in ALL_SOURCES}
    scanned = 0
    warnings: list[str] = []
    diag: dict[str, dict] = {k: {} for k in ALL_SOURCES}

    state = {k: {"count": 0, "fraction": 0.0} for k in selected}
    lock = threading.Lock()

    def emit(message: str):
        if not progress:
            return
        with lock:
            frac = sum(s["fraction"] for s in state.values()) / max(len(state), 1)
            progress(int(min(frac, 1.0) * 92), message, {k: s["count"] for k, s in state.items()})

    def reporter(key: str):
        def _report(count=None, fraction=None, message=""):
            with lock:
                if count is not None:
                    state[key]["count"] = count
                if fraction is not None:
                    state[key]["fraction"] = max(state[key]["fraction"], min(float(fraction), 1.0))
            emit(message)
        return _report

    emit("Starting search...")
    li_limit = max_linkedin_results
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = {}
        if "linkedin" in selected:
            jobs["linkedin"] = pool.submit(
                fetch_linkedin_profiles, query, li_limit, require_500, region,
                deep=deep, round_no=rnd, exclude=exclude_set, target=target, deadline=deadline,
                cancel=cancel, report=reporter("linkedin"), diag=diag["linkedin"])
        if "github" in selected:
            jobs["github"] = pool.submit(
                fetch_github_people, query, 100 if deep else 15, region, warnings, diag["github"],
                round_no=rnd, exclude=exclude_set, deep=deep, cancel=cancel, report=reporter("github"))
        if "yc" in selected:
            jobs["yc"] = pool.submit(
                fetch_yc_people, query, 50 if deep else 15, region,
                round_no=rnd, exclude=exclude_set, deep=deep, diag=diag["yc"], cancel=cancel, report=reporter("yc"))
        if "wellfound" in selected:
            jobs["wellfound"] = pool.submit(
                fetch_wellfound_people, query, 100 if deep else 15, region, warnings, diag["wellfound"],
                round_no=rnd, exclude=exclude_set, deep=deep, cancel=cancel, report=reporter("wellfound"))

        for key, fut in jobs.items():
            try:
                res = fut.result()
            except Exception as exc:
                print(f"[X] {SOURCE_LABELS[key]} source failed: {exc}")
                _warn(warnings, f"{SOURCE_LABELS[key]} search failed: {exc}")
                continue
            if key == "linkedin":
                found[key], scanned = res
            else:
                found[key] = res

    cancelled = bool(cancel is not None and cancel.is_set())

    # LinkedIn is the only source of contact details: look up the LinkedIn profile of
    # people from other platforms (only when they link to one).
    others = [p for k in ALL_SOURCES if k != "linkedin" for p in found[k]]
    if others and not cancelled:
        if progress:
            progress(94, "Looking up contact info on linked LinkedIn profiles...", {k: s["count"] for k, s in state.items()})
        enrich_contacts_from_linkedin(others, limit=100 if deep else 30, cancel=cancel)

    for p in found["linkedin"]:
        p.setdefault("bio", p.get("snippet", ""))
        p.setdefault("company", None)
        p.setdefault("website", None)
        p.setdefault("links", {})
        p.setdefault("extra", {})
        p.setdefault("source_type", "search_index")

    all_profiles = [p for k in ALL_SOURCES for p in found[k]]
    emails = sorted({e for p in all_profiles for e in p.get("emails", [])})
    phones = sorted({n for p in all_profiles for n in p.get("contact_numbers", [])})
    elapsed = round(time.time() - start, 2)

    output = {
        "query": query,
        "region": region if region else "All / Unspecified",
        "mode": "more" if more else ("deep" if deep else "standard"),
        "cancelled": cancelled,
        "sources_searched": [SOURCE_LABELS[s] for s in selected],
        "contact_policy": "Emails and phone numbers are taken from LinkedIn profiles only.",
        "metrics": {
            "require_500_connections": require_500,
            "total_linkedin_profiles_found": len(found["linkedin"]),
            "total_linkedin_results_scanned": scanned,
            "total_github_profiles_found": len(found["github"]),
            "total_yc_profiles_found": len(found["yc"]),
            "total_wellfound_profiles_found": len(found["wellfound"]),
            "profiles_with_email": sum(1 for p in all_profiles if p.get("emails")),
            "profiles_with_phone": sum(1 for p in all_profiles if p.get("contact_numbers")),
            "total_unique_emails_found": len(emails),
            "total_unique_phones_found": len(phones),
            "total_execution_time_seconds": elapsed,
            "total_execution_time_formatted": f"{int(elapsed // 60)}m {round(elapsed % 60, 2)}s",
        },
        "all_emails": emails,
        "all_contact_numbers": phones,
        "linkedin_profiles": found["linkedin"],
        "github_profiles": found["github"],
        "yc_profiles": found["yc"],
        "wellfound_profiles": found["wellfound"],
        "all_profiles": all_profiles,
        "paging": {k: {"next_round": diag[k].get("next_round", rnd + 1), "exhausted": bool(diag[k].get("exhausted", False))}
                   for k in selected},
        "warnings": warnings,
        "diagnostics": {k: v.get("summary") for k, v in diag.items() if v.get("summary")},
    }

    try:
        storage = save_to_mongo(full_search_query, user_id, all_profiles, [], output["metrics"])
        output["metrics"]["profiles_saved_to_mongodb"] = storage["linkedin_profiles_saved"]
        output["storage"] = storage
    except (PyMongoError, RuntimeError) as exc:
        print(f"[X] MongoDB save failed: {exc}")
        output["metrics"]["profiles_saved_to_mongodb"] = 0
        output["storage"] = {"error": f"MongoDB save failed: {exc}"}
    if progress:
        progress(100, "Done", {k: s["count"] for k, s in state.items()})
    return output


# ==============================================================================
# BACKGROUND JOBS (deep search can take minutes - far longer than an HTTP request should live)
# NOTE: jobs live in this process's memory. Run the app as a single worker process
# (threads are fine) or move this dict to Redis/MongoDB if you scale to several workers.
# ==============================================================================

_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()


def _prune_jobs():
    now = time.time()
    with _JOBS_LOCK:
        for jid in [j for j, v in _JOBS.items() if now - v["created"] > JOB_TTL_SECONDS]:
            _JOBS.pop(jid, None)


def _user_has_running_job(user_id: str) -> bool:
    with _JOBS_LOCK:
        return any(v["user_id"] == user_id and v["status"] == "running" for v in _JOBS.values())


def _start_job(user_id: str, kwargs: dict) -> str:
    _prune_jobs()
    job_id = uuid.uuid4().hex
    cancel = threading.Event()
    job = {"id": job_id, "user_id": user_id, "status": "running", "percent": 0, "message": "Queued...",
           "counts": {}, "result": None, "error": None, "created": time.time(), "cancel": cancel}
    with _JOBS_LOCK:
        _JOBS[job_id] = job

    def progress(percent, message, counts):
        job["percent"], job["message"], job["counts"] = percent, message, dict(counts)

    def worker():
        try:
            job["result"] = run_query_email_scraper(user_id=user_id, progress=progress, cancel=cancel, **kwargs)
            job["status"] = "done"
        except Exception as exc:
            print(f"[X] Job {job_id} failed: {exc}")
            job["error"], job["status"] = f"Scrape failed: {exc}", "error"

    threading.Thread(target=worker, daemon=True, name=f"scrape-{job_id[:6]}").start()
    return job_id


def _scrape_kwargs(payload: dict) -> dict:
    def clamp(v, default, lo, hi):
        try:
            return max(lo, min(hi, int(v)))
        except (TypeError, ValueError):
            return default

    more = payload.get("more")
    return dict(
        query=(payload.get("query") or "").strip(),
        region=(payload.get("region") or "").strip(),
        max_linkedin_results=clamp(payload.get("max_linkedin_results"), 20, 1, 50),
        require_500=bool(payload.get("require_500_connections", True)),
        sources=payload.get("sources") if isinstance(payload.get("sources"), list) else None,
        deep=bool(payload.get("deep", False)),
        target=clamp(payload.get("target"), DEEP_TARGET_DEFAULT, 50, 300),
        more=more if more in ALL_SOURCES else None,
        round_no=clamp(payload.get("round"), 0, 0, 500),
        exclude=[str(u) for u in (payload.get("exclude") or [])][:3000] if isinstance(payload.get("exclude"), list) else [],
    )


# ==============================================================================
# ROUTES
# ==============================================================================

@scraper_bp.route('/scraper')
def scraper_page():
    if 'user_id' not in session:
        return redirect(url_for('auth.login'))
    return render_template('user/scraper.html', username=session.get('username', 'User'))


@scraper_bp.route('/api/scrape/start', methods=['POST'])
def api_scrape_start():
    if 'user_id' not in session:
        return jsonify({"error": "Unauthorized access"}), 401
    payload = request.get_json(silent=True) or {}
    kwargs = _scrape_kwargs(payload)
    if not kwargs["query"]:
        return jsonify({"error": "A search query is required."}), 400
    user_id = str(session['user_id'])
    if _user_has_running_job(user_id):
        return jsonify({"error": "A search is already running. Wait for it to finish or cancel it."}), 409
    return jsonify({"job_id": _start_job(user_id, kwargs)})


@scraper_bp.route('/api/scrape/status/<job_id>')
def api_scrape_status(job_id):
    if 'user_id' not in session:
        return jsonify({"error": "Unauthorized access"}), 401
    job = _JOBS.get(job_id)
    if not job or job["user_id"] != str(session['user_id']):
        return jsonify({"error": "Search job not found (it may have expired or the server restarted)."}), 404
    body = {"status": job["status"], "percent": job["percent"], "message": job["message"],
            "counts": job["counts"], "elapsed": round(time.time() - job["created"], 1)}
    if job["status"] == "done":
        body["result"] = job["result"]
    elif job["status"] == "error":
        body["error"] = job["error"]
    return jsonify(body)


@scraper_bp.route('/api/scrape/cancel/<job_id>', methods=['POST'])
def api_scrape_cancel(job_id):
    if 'user_id' not in session:
        return jsonify({"error": "Unauthorized access"}), 401
    job = _JOBS.get(job_id)
    if not job or job["user_id"] != str(session['user_id']):
        return jsonify({"error": "Search job not found."}), 404
    job["cancel"].set()
    job["message"] = "Cancelling - finishing the current step..."
    return jsonify({"ok": True})


@scraper_bp.route('/api/scrape', methods=['POST'])
def api_scrape():
    """Synchronous endpoint kept for compatibility (standard / load-more only). Use /api/scrape/start for Deep Search."""
    if 'user_id' not in session:
        return jsonify({"error": "Unauthorized access"}), 401
    payload = request.get_json(silent=True) or {}
    kwargs = _scrape_kwargs(payload)
    if not kwargs["query"]:
        return jsonify({"error": "A search query is required."}), 400
    if kwargs["deep"]:
        return jsonify({"error": "Deep Search runs as a background job - use /api/scrape/start."}), 400
    try:
        return jsonify(run_query_email_scraper(user_id=str(session['user_id']), **kwargs))
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