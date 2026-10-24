"""
Multi-Platform People Finder (job role + location -> person profiles)

Pipeline:
  1. LinkedIn: public profiles found through search engines.
  2. GitHub (official API) and Y Combinator founders: person records
     with name, bio, company, location, links.
  3. Strict Policy: Return ONLY profiles that have a verified email or contact phone number.
     Profiles without one are dropped at every stage (discovery, live stream, results, MongoDB).
  4. LinkedIn-first parallel search: LinkedIn decides when the search ends. It stops as soon as
     TARGET_MAX_PROFILES (20) LinkedIn profiles with contact info are found, or after the soft time
     limit (5 min) if at least TARGET_MIN_PROFILES (10) were found. More is better, 10 is the minimum:
     the 10-min ceiling (SEARCH_HARD_LIMIT_SECONDS, 0 disables it) only applies once 10 are found.
     Other platforms only add extras.
  5. Live Streaming: New profiles are immediately pushed to MongoDB and streamed to the UI in real time.
  6. Results stored in MongoDB; Excel export endpoints & Admin Repository filtering.
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
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED
from concurrent.futures import TimeoutError as FuturesTimeout
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


@scraper_bp.errorhandler(Exception)
def _api_errors_as_json(exc):
    """
    The frontend calls /api/... with fetch(...).json(). Any unhandled server error used to come back as
    an HTML error page, which the browser shows as: Unexpected token '<', "<!doctype"... is not valid JSON.
    API routes now always answer with JSON that contains the real error message.
    """
    from werkzeug.exceptions import HTTPException
    is_api = request.path.startswith("/api/")
    if isinstance(exc, HTTPException):
        if is_api:
            return jsonify({"error": f"{exc.name}: {exc.description}"}), (exc.code or 500)
        return exc
    import traceback
    traceback.print_exc()
    if is_api:
        return jsonify({"error": f"Server error - {type(exc).__name__}: {exc}"}), 500
    raise exc
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

# ------------------------------------------------------------------------------
# SEARCH LIMITS (all overridable through environment variables)
# ------------------------------------------------------------------------------
TARGET_MIN_PROFILES = int(os.getenv("SEARCH_TARGET_MIN", "10"))   # every search returns at least this many
TARGET_MAX_PROFILES = int(os.getenv("SEARCH_TARGET_MAX", "20"))
SEARCH_TIME_BUDGET_SECONDS = int(os.getenv("SEARCH_BUDGET_SECONDS", "300"))    # 5 min: stop here if MIN reached
SEARCH_HARD_LIMIT_SECONDS = int(os.getenv("SEARCH_HARD_LIMIT_SECONDS", "600"))  # 10 min ceiling; 0 = no ceiling at all
ENGINE_TIMEOUT_SECONDS = float(os.getenv("ENGINE_TIMEOUT_SECONDS", "20"))       # max wait per search-engine call
LINKEDIN_PARALLEL_SEARCHES = int(os.getenv("LINKEDIN_PARALLEL_SEARCHES", "2"))  # concurrent queries (x4 engines each)


def _has_contact(p: dict) -> bool:
    """True only if the profile carries at least one email or phone number."""
    return bool(p.get("emails") or p.get("contact_numbers") or p.get("contact_number"))


class _SearchBudget:
    """
    Stop rules shared by every source of ONE search, so the whole search (not each source)
    ends at 10-20 contact-rich profiles:
      - total >= max_results                         -> stop (target reached)
      - elapsed >= soft limit and total >= min       -> stop (good enough, save time)
      - elapsed >= hard limit and total >= min       -> stop (ceiling; the minimum is always honoured)
    """

    def __init__(self, min_results: int, max_results: int, soft_seconds: float,
                 hard_seconds: float, cancel=None):
        self.min_results = max(1, int(min_results))
        self.max_results = max(self.min_results, int(max_results))
        self.soft_seconds = float(soft_seconds)
        # hard_seconds <= 0 means "no ceiling"
        self.hard_seconds = max(float(hard_seconds), self.soft_seconds) if hard_seconds and hard_seconds > 0 else float("inf")
        self.primary: str | None = None  # when set, only this source's count decides when the search is done
        self.cancel = cancel
        self.started = time.time()
        self.last_reason = ""
        self._counts: dict[str, int] = {}
        self._lock = threading.Lock()

    def set_count(self, key: str, n: int):
        with self._lock:
            self._counts[key] = n

    @property
    def total(self) -> int:
        with self._lock:
            if self.primary:
                return self._counts.get(self.primary, 0)
            return sum(self._counts.values())

    @property
    def elapsed(self) -> float:
        return time.time() - self.started

    def fraction(self) -> float:
        by_count = self.total / self.max_results
        by_time = self.elapsed / self.soft_seconds if self.soft_seconds else 0.0
        return min(1.0, max(by_count, by_time))

    def stop_reason(self) -> str:
        total, elapsed = self.total, self.elapsed
        if self.cancel is not None and self.cancel.is_set():
            reason = "cancelled"
        elif total >= self.max_results:
            reason = "target reached"
        elif elapsed >= self.hard_seconds and total >= self.min_results:
            reason = "time ceiling reached"
        elif elapsed >= self.soft_seconds and total >= self.min_results:
            reason = "time budget reached"
        else:
            reason = ""
        if reason and not self.last_reason:
            self.last_reason = reason
        return reason


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
    db.scrape_jobs.create_index("job_id", unique=True)
    db.scrape_jobs.create_index([("user_id", 1), ("status", 1)])
    return db


def save_to_mongo(query: str, user_id: str, profiles: list[dict], sites: list[dict], metrics: dict) -> dict:
    db = get_db()
    now = datetime.now(timezone.utc)
    profiles = [p for p in profiles if _has_contact(p)]  # strict policy: never store profiles without email/phone

    profile_ops = [
        UpdateOne(
            {"profile_url": p["profile_url"]},
            {
                "$set": {
                    **p,
                    "platform": _canonical_platform_label(p.get("platform") or "LinkedIn"),
                    "last_query": query,
                    "last_scraped_at": now,
                    "scraped_by_user_id": user_id
                },
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
# SEARCH ENGINES
# ==============================================================================

def _search_duckduckgo(query: str, max_results: int = 30, page: int = 0) -> list[dict]:
    results = []
    try:
        with DDGS() as ddgs:
            try:
                raw = ddgs.text(query, max_results=max_results, page=page + 1)
            except TypeError:
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


_ENGINES = {
    "DuckDuckGo": _search_duckduckgo,
    "Google": _search_google,
    "Bing": _search_bing,
    "Yahoo": _search_yahoo,
}
# One shared pool: no thread start-up cost per query, and a slow engine never blocks a search.
_ENGINE_POOL = ThreadPoolExecutor(max_workers=32, thread_name_prefix="engine")


_ENGINE_STATS: dict[str, dict[str, int]] = {n: {"calls": 0, "results": 0, "timeouts": 0} for n in _ENGINES}
_STATS_LOCK = threading.Lock()


def _engine_stats_text() -> str:
    with _STATS_LOCK:
        return ", ".join(f"{n}: {s['results']} results/{s['calls']} calls" + (f"/{s['timeouts']} timeouts" if s["timeouts"] else "")
                         for n, s in _ENGINE_STATS.items())


def _run_engines(q: str, max_results: int, page: int = 0) -> list[dict]:
    """Query all engines in parallel; stop waiting after ENGINE_TIMEOUT_SECONDS and keep what arrived."""
    items: list[dict] = []
    counts: dict[str, int] = {}
    futures = {_ENGINE_POOL.submit(fn, q, max_results, page): name for name, fn in _ENGINES.items()}
    try:
        for f in as_completed(futures, timeout=ENGINE_TIMEOUT_SECONDS):
            name = futures[f]
            try:
                res = f.result()
            except Exception as e:
                print(f"[!] {name} crashed: {e}")
                res = []
            counts[name] = len(res)
            items.extend(res)
    except FuturesTimeout:
        slow = [name for f, name in futures.items() if not f.done()]
        print(f"[engines] not waiting any longer for: {slow}")
        with _STATS_LOCK:
            for n in slow:
                _ENGINE_STATS[n]["timeouts"] += 1
        for f in futures:
            f.cancel()
    with _STATS_LOCK:
        for n, c in counts.items():
            _ENGINE_STATS[n]["calls"] += 1
            _ENGINE_STATS[n]["results"] += c
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


def _region_pattern(text: str) -> str | None:
    tokens = [re.escape(t) for t in re.findall(r"[a-zA-Z0-9]+", text) if len(t) >= 2]
    if not tokens:
        return None
    return r"(?<![a-z0-9])" + r"[\s,-]+".join(tokens) + r"(?![a-z0-9])"


def _verify_region_match(full_text: str, region: str) -> bool:
    if not region or not region.strip():
        return True

    full = _region_pattern(region)
    if full is None:
        return True
    if re.search(full, full_text, re.I):
        return True

    # Search snippets normally show only the city ("Siliguri Area, India"), so the first part
    # of a "City, State, Country" region is enough. Without this, valid local profiles were dropped.
    city = _region_pattern(re.split(r"[,/|]", region)[0])
    return bool(city and city != full and re.search(city, full_text, re.I))


def _relevance_score(headline: str, snippet: str, query: str, raw_title: str,
                     require_500: bool = True, region: str = "", loose: bool = False) -> tuple[int, str]:
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
        if frac < 0.5:
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
    r = (region or "").strip()
    if not r:
        return [""]
    first = re.split(r"[,/|]", r)[0].strip()
    out = [f'"{first} Area"', f'"Greater {first} Area"', f'"{first} Metropolitan Area"']
    out.append(f'"{r}"' if "," in r or "india" in r.lower() else f'"{first}, India"')
    return list(dict.fromkeys(out))


ANCHOR_ANY = '("email" OR "gmail.com" OR "@" OR "phone" OR "contact")'
ANCHOR_EMAIL = '("email" OR "e-mail" OR "@")'
ANCHOR_GMAIL = '("@gmail.com" OR "@outlook.com" OR "@yahoo.com" OR "@hotmail.com")'
ANCHOR_PHONE = '("phone" OR "mobile" OR "+91" OR "WhatsApp" OR "call")'


def _linkedin_rounds(query: str, region: str, require_500: bool) -> list[list[str]]:
    """
    Round 0 holds the queries most likely to return a snippet that contains an email/phone
    for the requested role + location; later rounds only widen the net. Queries run in this
    order and the search stops as soon as the target is reached.
    """
    phrases = _search_phrases(query)
    primary = phrases[0]
    region = (region or "").strip()
    loc = _loc_pattern(region)
    conn = ' "500+ connections"' if require_500 else ""
    tokens = [t for t in re.findall(r"[A-Za-z0-9+#.]+", query) if t.lower() not in STOPWORDS]

    def q(phrase: str, anchor: str, *, location: str | None = None, extra: str = "", with_conn: bool = False) -> str:
        where = loc if location is None else location
        return _tidy(f'site:linkedin.com/in "{phrase}" {where}{conn if with_conn else ""} {extra} {anchor}')

    # Round 0: role + location + a contact anchor (email word, mail provider, phone/WhatsApp)
    base = [q(p, ANCHOR_ANY) for p in phrases]
    base += [
        q(primary, ANCHOR_GMAIL),
        q(primary, ANCHOR_PHONE),
        q(primary, ANCHOR_EMAIL, with_conn=True),
        q(primary, ANCHOR_PHONE, with_conn=True),
    ]

    pool: list[str] = []
    # Other spellings of the location (most useful widening for "role + location" searches)
    for place in _location_variants(region):
        for p in phrases:
            pool.append(q(p, ANCHOR_ANY, location=place))
    # Other word orders of the role
    for w in _query_windows(tokens)[1:]:
        pool.append(q(w, ANCHOR_ANY))
        pool.append(_tidy(f'site:linkedin.com/in {w} {loc} {ANCHOR_ANY}'))
    # Seniority and company hints (lowest yield, only reached if time remains)
    for mod in DEEP_ROLE_MODIFIERS:
        pool.append(q(primary, ANCHOR_ANY, extra=mod))
    for comp in DEEP_COMPANY_HINTS:
        pool.append(q(primary, ANCHOR_ANY, extra=f'"{comp}"'))
    return _chunk_rounds(base, pool)


def _format_current_profiles(candidates_dict: dict) -> list[dict]:
    profiles = []
    for c in list(candidates_dict.values()):
        emails = c.get("_emails", set())
        phones = c.get("_phones", set())
        if not (emails or phones):
            continue  # strict policy: no email and no phone -> never shown
        profiles.append({
            "name": c.get("name"),
            "headline": c.get("headline"),
            "profile_url": c.get("profile_url"),
            "snippet": c.get("snippet"),
            "emails": sorted(emails),
            "contact_number": sorted(phones)[0] if phones else None,
            "contact_numbers": sorted(phones),
            "contact_source": "linkedin_profile_text" if (emails or phones) else None,
            "relevance_score": c.get("relevance_score", 1),
            "engine": c.get("engine", "Search Engine"),
            "platform": "LinkedIn",
            "connections": c.get("connections", "500+ connections"),
            "location": c.get("location", "Global / Unspecified")
        })

    def sort_key(p):
        contact = (2 if p["emails"] else 0) + (1 if p["contact_numbers"] else 0)
        return (p["relevance_score"], contact)

    profiles.sort(key=sort_key, reverse=True)
    return profiles


MAX_DYNAMIC_PASSES = 50


def fetch_linkedin_profiles(query: str, max_results: int = 20, require_500: bool = True, region: str = "",
                            *, deep: bool = False, round_no: int = 0, exclude=None,
                            budget: "_SearchBudget | None" = None, cancel=None, report=None,
                            diag: dict | None = None) -> tuple[list[dict], int]:
    """
    Finds LinkedIn profiles whose search-result text shows the role, the location AND an
    email or phone number. Queries run in parallel; the search ends as soon as `budget` says so.
    `max_results` is the number of results requested per search-engine page.
    """
    diag = diag if diag is not None else {}
    exclude = {u.lower().rstrip("/") for u in (exclude or [])}
    if budget is None:  # standalone use
        budget = _SearchBudget(TARGET_MIN_PROFILES, TARGET_MAX_PROFILES,
                               SEARCH_TIME_BUDGET_SECONDS, SEARCH_HARD_LIMIT_SECONDS, cancel)
    rounds = _linkedin_rounds(query, region, require_500)

    candidates: dict[str, dict] = {}
    scanned = 0
    searches = 0
    stop_reason = ""
    rej = {"not_linkedin_profile": 0, "role_or_location": 0, "name_not_matching_url": 0, "no_email_or_phone": 0}

    def handle(item: dict) -> bool:
        nonlocal scanned
        parsed_url = _parse_profile_url(item.get("url", ""))
        if not parsed_url:
            rej["not_linkedin_profile"] += 1
            return False
        url, slug, is_root = parsed_url
        scanned += 1
        if not is_root or url.lower() in exclude:
            return False

        raw_title = (item.get("title") or "").strip()
        snippet = (item.get("snippet") or "").strip()

        parts = re.split(r"\s[-–|]\s", raw_title, maxsplit=1)
        name = _clean_profile_name(parts[0].strip())
        headline = parts[1].strip() if len(parts) > 1 else None
        if headline:
            headline = re.sub(r"\s*[|\-–]\s*LinkedIn\s*$", "", headline, flags=re.I).strip() or None

        # role + location check (cheap, so it runs before the contact extraction)
        score, matched_in = _relevance_score(headline, snippet, query, raw_title, require_500, region=region, loose=True)
        if score == 0:
            rej["role_or_location"] += 1
            return False

        owned = _name_matches_slug(name, slug)
        item_emails = _extract_profile_emails(raw_title, snippet, owner_name=name, slug=slug) if owned else set()
        item_phones = _extract_phones(raw_title, snippet) if owned else set()

        # STRICT FILTER: block the profile immediately if there is no email AND no phone number
        if not (item_emails or item_phones):
            rej["name_not_matching_url" if not owned else "no_email_or_phone"] += 1
            return False

        conn_text = "500+ connections" if require_500 else (_detect_connections(raw_title, snippet) or "Not shown")

        is_new = url not in candidates
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

        return is_new

    # Query stream, best queries first: static rounds, then endless provider/phone-anchor variations.
    pages = (0, 1, 2) if deep else (0, 1)
    email_providers = ["@gmail.com", "@yahoo.com", "@outlook.com", "@hotmail.com", "@icloud.com"]
    phone_anchors = ['"+91"', '"mobile"', '"WhatsApp"', '"contact"', '"phone"']
    loc_str = f'"{region}"' if region else ""

    def query_stream():
        for rnd in rounds:
            for q in rnd:
                for pg in pages:
                    yield q, max_results, pg
        for n in range(MAX_DYNAMIC_PASSES):
            provider = email_providers[n % len(email_providers)]
            anchor = phone_anchors[n % len(phone_anchors)]
            pg = (n // len(email_providers)) % 5
            yield _tidy(f'site:linkedin.com/in "{query}" {loc_str} "{provider}"'), 30, pg
            yield _tidy(f'site:linkedin.com/in "{query}" {loc_str} {anchor}'), 30, pg
            yield _tidy(f'linkedin.com/in "{query}" {loc_str} "{provider}" OR "{anchor.strip(chr(34))}"'), 30, pg

    stream = query_stream()
    inflight: dict = {}
    pool = ThreadPoolExecutor(max_workers=max(1, LINKEDIN_PARALLEL_SEARCHES), thread_name_prefix="li-search")

    def submit_next() -> bool:
        try:
            q, n, pg = next(stream)
        except StopIteration:
            return False
        inflight[pool.submit(_run_engines, q, n, pg)] = q
        return True

    try:
        for _ in range(max(1, LINKEDIN_PARALLEL_SEARCHES)):
            if not submit_next():
                break
        stop_reason = budget.stop_reason()
        while inflight and not stop_reason:
            done, _pending = wait(list(inflight), timeout=1.0, return_when=FIRST_COMPLETED)
            for fut in done:
                inflight.pop(fut, None)
                searches += 1
                try:
                    items = fut.result()
                except Exception as exc:
                    print(f"[!] LinkedIn search failed: {exc}")
                    items = []
                added = False
                for item in items:
                    if handle(item):
                        added = True
                budget.set_count("linkedin", len(candidates))
                if added and report:
                    report(count=len(candidates), fraction=budget.fraction(),
                           message=f"LinkedIn: {len(candidates)} contact-rich profiles "
                                   f"({budget.total}/{budget.max_results} overall, {int(budget.elapsed)}s)...",
                           live_profiles=_format_current_profiles(candidates))
                stop_reason = budget.stop_reason()
                if stop_reason:
                    break
                submit_next()
            if not stop_reason:
                stop_reason = budget.stop_reason()
    finally:
        for fut in inflight:
            fut.cancel()
        pool.shutdown(wait=False, cancel_futures=True)  # do not wait for searches that are still running

    diag["next_round"] = round_no + 1
    diag["exhausted"] = len(candidates) >= budget.max_results
    diag["summary"] = (f"{len(candidates)} LinkedIn profiles with verified contact info kept from {scanned} results over "
                       f"{searches} searches" + (f" (stopped: {stop_reason})" if stop_reason else "") + ". "
                       f"Rejected: {rej['role_or_location']} role/location mismatch, {rej['no_email_or_phone']} no email/phone "
                       f"in the result text, {rej['name_not_matching_url']} name does not match the profile URL, "
                       f"{rej['not_linkedin_profile']} not LinkedIn profile pages. Engines: {_engine_stats_text()}.")

    final_profiles = _format_current_profiles(candidates)
    return final_profiles[:budget.max_results], scanned


# ==============================================================================
# PERSON-PROFILE SOURCES (trusted platforms only)
# ==============================================================================

ALL_SOURCES = ("linkedin", "github", "yc")
SOURCE_LABELS = {
    "linkedin": "LinkedIn", "github": "GitHub",
    "yc": "Y Combinator",
}
GITHUB_API = "https://api.github.com"
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")


def _new_person(**kw) -> dict:
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
    if not frags or not text:
        return 0.0
    return sum(1 for f in frags if re.search(_B_L + f + _B_R, text, re.I)) / len(frags)


def _query_windows(tokens: list[str]) -> list[str]:
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
    if not _name_matches_slug(name, slug):
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


def _enrich_person_from_linkedin(person: dict) -> bool:
    """Look for an email/phone in the person's linked LinkedIn search snippet; update the dict in place."""
    try:
        emails, phones = _linkedin_contact_lookup(person)
    except Exception:
        return False
    if not (emails or phones):
        return False
    person["emails"] = sorted(set(person.get("emails", [])) | emails)
    person["contact_numbers"] = sorted(set(person.get("contact_numbers", [])) | phones)
    person["contact_number"] = person["contact_numbers"][0] if person["contact_numbers"] else None
    person["contact_source"] = "linkedin_profile_text"
    return True


def enrich_contacts_from_linkedin(people: list[dict], limit: int = 30, cancel=None) -> int:
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


def fetch_github_people(query: str, max_results: int = 15, region: str = "",
                        warn: list | None = None, diag: dict | None = None, *,
                        round_no: int = 0, exclude=None, deep: bool = False,
                        cancel=None, report=None, budget: "_SearchBudget | None" = None) -> list[dict]:
    diag = diag if diag is not None else {}
    if budget is None:
        budget = _SearchBudget(1, max_results, SEARCH_TIME_BUDGET_SECONDS, SEARCH_HARD_LIMIT_SECONDS, cancel)
    exclude = {u.lower().rstrip("/") for u in (exclude or [])}
    seg = region.split(",")[0].strip() if region else ""
    loc_q = f' location:"{seg}"' if seg else ""
    tokens = [t for t in re.findall(r"[A-Za-z0-9+#.]+", query) if t.lower() not in STOPWORDS]

    queries = [f'"{p}" in:bio type:user{loc_q}' for p in _search_phrases(query)]
    queries += [f"{w} in:bio type:user{loc_q}" for w in _query_windows(tokens)]
    queries = list(dict.fromkeys(queries))
    if not GITHUB_TOKEN:
        queries = queries[:3]

    pages = list(range(round_no, round_no + 3)) if deep else [round_no]
    pause = 0.2 if GITHUB_TOKEN else 1.2

    # Only users with a public email/phone are kept, so look at more users than we need.
    if GITHUB_TOKEN:
        cap = max(max_results * 4, 40)
    else:
        cap = 25 if deep else 15
        if deep:
            _warn(warn, "GitHub: without GITHUB_TOKEN only ~25 profiles per hour can be checked. "
                        "Add GITHUB_TOKEN to .env to get many more GitHub results in Deep Search.")

    logins: list[str] = []
    for q in queries:
        if len(logins) >= cap or budget.stop_reason():
            break
        for pg in pages:
            if len(logins) >= cap or budget.stop_reason():
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
    logins = logins[:cap]
    frags = _build_requirements(query)
    progress_done = 0

    def build(login: str):
        if budget.stop_reason():
            return None, "skipped"
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
        li = _find_linkedin_url(d.get("blog"), bio)
        if li:
            links["linkedin"] = li

        # Contact info the user published themselves: public profile email, plus email/phone written in the bio.
        emails = {e for e in _extract_emails(bio) if not e.endswith("noreply.github.com")}
        public_email = (d.get("email") or "").strip().lower()
        if public_email and _valid_email(public_email) and not public_email.endswith("noreply.github.com"):
            emails.add(public_email)
        phones = _extract_phones(bio)

        person = _new_person(
            name=name, headline=bio[:140] or None, bio=bio, company=company, location=location,
            emails=sorted(emails), contact_numbers=sorted(phones), profile_url=d["html_url"], website=website,
            links=links,
            extra={"followers": d.get("followers", 0), "public_repos": d.get("public_repos", 0),
                   "hireable": d.get("hireable"), "relevance": rel},
            platform="GitHub", engine="GitHub REST API", source_type="official_api",
            contact_source="github_public_profile" if (emails or phones) else None, snippet=bio,
        )
        if not _has_contact(person) and links.get("linkedin") and not budget.stop_reason():
            _enrich_person_from_linkedin(person)  # last resort, only for users with a LinkedIn link
        if not _has_contact(person):
            return None, "no_contact"  # STRICT FILTER: no email and no phone -> blocked
        return person, "ok"

    people: list[dict] = []
    reasons = {"ok": 0, "role": 0, "location": 0, "unavailable": 0, "no_contact": 0, "skipped": 0}
    pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="github")
    try:
        futs = [pool.submit(build, lg) for lg in logins]
        for fut in as_completed(futs):
            try:
                person, why = fut.result()
            except Exception as exc:
                print(f"[!] GitHub profile error: {exc}")
                continue
            reasons[why] += 1
            progress_done += 1
            if person:
                people.append(person)
                budget.set_count("github", len(people))
            if report:
                report(count=len(people), fraction=budget.fraction(),
                       message=f"GitHub: {len(people)} users with contact info ({progress_done}/{len(logins)} checked)",
                       live_profiles=list(people))
            if budget.stop_reason():
                break
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    people.sort(key=lambda p: (p["extra"]["relevance"], p["extra"].get("followers", 0)), reverse=True)
    diag["next_round"] = round_no + (3 if deep else 1)
    diag["exhausted"] = found_total == 0
    diag["summary"] = (f"{found_total} new users matched on GitHub search; checked {progress_done} of {len(logins)}: "
                       f"{reasons['ok']} kept, {reasons['no_contact']} blocked (no email/phone), "
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
                emails=[], contact_numbers=[],
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


def _yc_founders_with_contacts(url: str, region: str, budget: "_SearchBudget") -> list[dict]:
    """Founders of one YC company that have an email/phone (found via their LinkedIn link). Others are blocked."""
    kept = []
    for p in _yc_company_people(url, region):
        if budget.stop_reason():
            break
        if not _has_contact(p) and (p.get("links") or {}).get("linkedin"):
            _enrich_person_from_linkedin(p)
        if _has_contact(p):
            kept.append(p)
    return kept


def fetch_yc_people(query: str, max_results: int = 15, region: str = "", *,
                    round_no: int = 0, exclude=None, deep: bool = False,
                    diag: dict | None = None, cancel=None, report=None,
                    budget: "_SearchBudget | None" = None) -> list[dict]:
    diag = diag if diag is not None else {}
    if budget is None:
        budget = _SearchBudget(1, max_results, SEARCH_TIME_BUDGET_SECONDS, SEARCH_HARD_LIMIT_SECONDS, cancel)
    exclude = set(exclude or [])
    rounds = _yc_rounds(query, region)
    run_rounds = list(range(len(rounds))) if deep else [round_no]

    pages: list[str] = []
    for r in run_rounds:
        if r >= len(rounds):
            break
        for q in rounds[r]:
            if budget.stop_reason():
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
            time.sleep(0.2)
            if report:
                report(count=0, fraction=budget.fraction(), message=f"Y Combinator: {len(pages)} company pages found")

    cap = 60 if deep else max(max_results, 20)
    people: list[dict] = []
    done = 0
    pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="yc")
    try:
        futs = [pool.submit(_yc_founders_with_contacts, u, region, budget) for u in pages[:cap]]
        for f in as_completed(futs):
            done += 1
            try:
                people.extend(p for p in f.result() if p["profile_url"] not in exclude)
            except Exception as exc:
                print(f"[!] YC page error: {exc}")
            budget.set_count("yc", len(people))
            if report:
                report(count=len(people), fraction=budget.fraction(),
                       message=f"Y Combinator: {len(people)} founders with contact info ({done}/{len(futs)} companies read)",
                       live_profiles=list(people))
            if budget.stop_reason():
                break
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    next_round = (len(rounds) if deep else round_no + 1)
    diag["next_round"] = next_round
    diag["exhausted"] = next_round >= len(rounds)
    diag["summary"] = (f"{len(pages)} YC company pages found; {len(people)} founders with email/phone kept "
                       f"(founders without contact info are blocked).")
    return people[: max_results * 3]


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
DEEP_TIME_BUDGET_SECONDS = int(os.getenv("DEEP_SEARCH_BUDGET_SECONDS", "180"))  # was 600


def _norm_relevance(p: dict) -> float:
    """LinkedIn scores 1-3, other sources store a 0-1 fraction; bring both to 0-1."""
    if p.get("relevance_score"):
        return float(p["relevance_score"]) / 3.0
    return float((p.get("extra") or {}).get("relevance", 0.5))


def _finalize_profiles(found: dict[str, list[dict]], limit: int) -> list[dict]:
    """
    Final gate for a search: keep only profiles with an email/phone, remove the same person found
    on two platforms (via the LinkedIn link), rank best first and cut to `limit`.
    """
    linkedin_urls = {(p.get("profile_url") or "").lower().rstrip("/") for p in found.get("linkedin", [])}
    seen: set[str] = set()
    merged: list[dict] = []
    for key in ALL_SOURCES:
        for p in found.get(key, []):
            if not _has_contact(p):
                continue
            url = (p.get("profile_url") or "").lower().rstrip("/")
            if not url or url in seen:
                continue
            if key != "linkedin":
                li = _parse_profile_url((p.get("links") or {}).get("linkedin") or "")
                if li and li[0].lower() in linkedin_urls:
                    continue
            seen.add(url)
            merged.append(p)
    # LinkedIn profiles first, then best contact info, then best role match; other platforms only fill the rest
    merged.sort(key=lambda p: (_platform_key(p.get("platform")) == "linkedin", _contact_score(p), _norm_relevance(p)),
                reverse=True)
    return merged[:limit]


def run_query_email_scraper(query: str, user_id: str, region: str = "",
                            max_linkedin_results: int = 20, require_500: bool = True,
                            sources: list | None = None, deep: bool = False,
                            target: int = DEEP_TARGET_DEFAULT, more: str | None = None,
                            round_no: int = 0, exclude: list | None = None,
                            progress=None, cancel=None):
    start = time.time()
    selected = [s for s in (sources or ALL_SOURCES) if s in ALL_SOURCES] or list(ALL_SOURCES)
    if more in ALL_SOURCES:
        selected = [more]
        deep = False
    rnd = round_no if more else 0
    exclude_set = set(exclude or [])
    full_search_query = f"{query} {region}".strip() if region else query

    # One budget for the WHOLE search: standard searches return 10-20 contact-rich profiles (10 minimum).
    if deep:
        budget = _SearchBudget(max(TARGET_MIN_PROFILES, target // 2), target,
                               DEEP_TIME_BUDGET_SECONDS, int(DEEP_TIME_BUDGET_SECONDS * 1.5), cancel)
    else:
        budget = _SearchBudget(TARGET_MIN_PROFILES, TARGET_MAX_PROFILES,
                               SEARCH_TIME_BUDGET_SECONDS, SEARCH_HARD_LIMIT_SECONDS, cancel)

    if "linkedin" in selected:
        budget.primary = "linkedin"  # LinkedIn is the main result; other sources never end the search early

    found: dict[str, list[dict]] = {k: [] for k in ALL_SOURCES}
    scanned = 0
    warnings: list[str] = []
    diag: dict[str, dict] = {k: {} for k in ALL_SOURCES}

    state = {k: {"count": 0} for k in selected}
    live_by_source: dict[str, list[dict]] = {}
    last_percent = [0]
    lock = threading.Lock()

    def emit(message: str):
        """Push progress + ALL sources' current profiles (contact-rich only) to the UI/MongoDB."""
        if not progress:
            return
        with lock:
            last_percent[0] = max(last_percent[0], int(min(budget.fraction(), 1.0) * 92))
            live = _finalize_profiles(live_by_source, budget.max_results)
            progress(last_percent[0], message, {k: s["count"] for k, s in state.items()}, live_profiles=live)

    def reporter(key: str):
        def _report(count=None, fraction=None, message="", live_profiles=None):
            with lock:
                if count is not None:
                    state[key]["count"] = count
                if live_profiles is not None:
                    live_by_source[key] = [p for p in live_profiles if _has_contact(p)]
            emit(message)
        return _report

    emit("Starting search...")
    li_limit = max_linkedin_results
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = {}
        if "linkedin" in selected:
            jobs["linkedin"] = pool.submit(
                fetch_linkedin_profiles, query, li_limit, require_500, region,
                deep=deep, round_no=rnd, exclude=exclude_set, budget=budget,
                cancel=cancel, report=reporter("linkedin"), diag=diag["linkedin"])
        if "github" in selected:
            jobs["github"] = pool.submit(
                fetch_github_people, query, budget.max_results, region, warnings, diag["github"],
                round_no=rnd, exclude=exclude_set, deep=deep, cancel=cancel, report=reporter("github"),
                budget=budget)
        if "yc" in selected:
            jobs["yc"] = pool.submit(
                fetch_yc_people, query, budget.max_results, region,
                round_no=rnd, exclude=exclude_set, deep=deep, diag=diag["yc"], cancel=cancel,
                report=reporter("yc"), budget=budget)

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

    for p in found["linkedin"]:
        p.setdefault("bio", p.get("snippet", ""))
        p.setdefault("company", None)
        p.setdefault("website", None)
        p.setdefault("links", {})
        p.setdefault("extra", {})
        p.setdefault("source_type", "search_index")

    # STRICT FILTERING + de-duplication + cap: ONLY profiles with a valid email or phone number are returned
    all_profiles = _finalize_profiles(found, budget.max_results)
    found = {k: [p for p in all_profiles if _platform_key(p.get("platform")) == k] for k in ALL_SOURCES}

    emails = sorted({e for p in all_profiles for e in p.get("emails", [])})
    phones = sorted({n for p in all_profiles for n in p.get("contact_numbers", [])})
    elapsed = round(time.time() - start, 2)

    if not cancelled and not deep and len(all_profiles) < TARGET_MIN_PROFILES:
        _warn(warnings, f"Only {len(all_profiles)} profile(s) with a public email or phone number were found "
                        f"in {int(budget.elapsed)}s (target {TARGET_MIN_PROFILES}-{TARGET_MAX_PROFILES}). "
                        f"Try a broader role or location, or use Load More. See diagnostics for why results were rejected.")

    output = {
        "query": query,
        "region": region if region else "All / Unspecified",
        "mode": "more" if more else ("deep" if deep else "standard"),
        "cancelled": cancelled,
        "sources_searched": [SOURCE_LABELS[s] for s in selected],
        "contact_policy": ("Only profiles with an email or phone number are returned. Contacts come from LinkedIn "
                           "profile text, public GitHub profile fields and profile bios."),
        "metrics": {
            "require_500_connections": require_500,
            "total_linkedin_profiles_found": len(found["linkedin"]),
            "total_linkedin_results_scanned": scanned,
            "total_github_profiles_found": len(found["github"]),
            "total_yc_profiles_found": len(found["yc"]),
            "total_profiles_returned": len(all_profiles),
            "target_profiles": f"{budget.min_results}-{budget.max_results}",
            "stopped_because": budget.last_reason or "all sources finished",
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
        progress(100, "Done", {k: s["count"] for k, s in state.items()}, live_profiles=all_profiles)
    return output


# ==============================================================================
# BACKGROUND JOBS (MONGODB PERSISTED TO PREVENT PM2 RESTART LOSS)
# ==============================================================================

_ACTIVE_CANCEL_EVENTS: dict[str, threading.Event] = {}
_ACTIVE_LOCK = threading.Lock()


JOB_HEARTBEAT_SECONDS = 15   # a running search touches its job document this often
JOB_STALE_SECONDS = 90       # no heartbeat for this long -> the search died (crash / restart) and is released


def _as_utc(dt):
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _user_has_running_job(user_id: str) -> bool:
    """
    True only if the user has a search that is really alive. Jobs live in MongoDB, so a search killed by a
    crash or server restart used to stay "running" forever and block every new search for that user.
    """
    try:
        db = get_db()
        now = datetime.now(timezone.utc)
        alive = False
        for job in db.scrape_jobs.find({"user_id": user_id, "status": "running"}):
            beat = _as_utc(job.get("updated_at") or job.get("created_at"))
            with _ACTIVE_LOCK:
                in_this_process = job.get("job_id") in _ACTIVE_CANCEL_EVENTS
            if in_this_process or (beat and (now - beat).total_seconds() < JOB_STALE_SECONDS):
                alive = True
                continue
            db.scrape_jobs.update_one(
                {"job_id": job.get("job_id"), "status": "running"},
                {"$set": {"status": "error", "error": "This search was interrupted (server stopped or restarted).",
                          "message": "Interrupted", "updated_at": now}})
            print(f"[jobs] released stale search {job.get('job_id')} (no heartbeat since {beat})")
        return alive
    except Exception:
        return False


def _start_job(user_id: str, kwargs: dict) -> str:
    db = get_db()
    job_id = uuid.uuid4().hex
    cancel_event = threading.Event()

    with _ACTIVE_LOCK:
        _ACTIVE_CANCEL_EVENTS[job_id] = cancel_event

    job_doc = {
        "job_id": job_id,
        "user_id": user_id,
        "status": "running",
        "percent": 0,
        "message": "Queued...",
        "counts": {},
        "live_profiles": [],
        "result": None,
        "error": None,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc)
    }
    db.scrape_jobs.insert_one(job_doc)

    def progress(percent, message, counts, live_profiles=None):
        try:
            update_data = {
                "percent": percent,
                "message": message,
                "counts": dict(counts),
                "updated_at": datetime.now(timezone.utc)
            }
            if live_profiles is not None:
                update_data["live_profiles"] = live_profiles
            db.scrape_jobs.update_one(
                {"job_id": job_id},
                {"$set": update_data}
            )
        except Exception as e:
            print(f"[!] Mongo job progress update warning: {e}")

    stop_heartbeat = threading.Event()

    def heartbeat():
        while not stop_heartbeat.wait(JOB_HEARTBEAT_SECONDS):
            try:
                db.scrape_jobs.update_one({"job_id": job_id, "status": "running"},
                                          {"$set": {"updated_at": datetime.now(timezone.utc)}})
            except Exception as e:
                print(f"[!] job heartbeat warning: {e}")

    threading.Thread(target=heartbeat, daemon=True, name=f"beat-{job_id[:6]}").start()

    def worker():
        try:
            res = run_query_email_scraper(
                user_id=user_id,
                progress=progress,
                cancel=cancel_event,
                **kwargs
            )
            db.scrape_jobs.update_one(
                {"job_id": job_id},
                {"$set": {
                    "status": "done",
                    "percent": 100,
                    "message": "Done",
                    "result": res,
                    "live_profiles": res.get("all_profiles", []),
                    "updated_at": datetime.now(timezone.utc)
                }}
            )
        except Exception as exc:
            print(f"[X] Scrape Job {job_id} failed: {exc}")
            db.scrape_jobs.update_one(
                {"job_id": job_id},
                {"$set": {
                    "status": "error",
                    "error": f"Scrape failed: {exc}",
                    "updated_at": datetime.now(timezone.utc)
                }}
            )
        finally:
            stop_heartbeat.set()
            with _ACTIVE_LOCK:
                _ACTIVE_CANCEL_EVENTS.pop(job_id, None)

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

    try:
        db = get_db()
        job = db.scrape_jobs.find_one({"job_id": job_id})
    except Exception as exc:
        return jsonify({"error": f"Database error checking status: {exc}"}), 500

    if not job or str(job.get("user_id")) != str(session['user_id']):
        return jsonify({"error": "Search job not found or expired."}), 404

    created_at = job.get("created_at")
    elapsed = 0.0
    if created_at:
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        elapsed = round((datetime.now(timezone.utc) - created_at).total_seconds(), 1)

    body = {
        "status": job.get("status", "running"),
        "percent": job.get("percent", 0),
        "message": job.get("message", "Processing..."),
        "counts": job.get("counts", {}),
        "elapsed": elapsed,
        "live_profiles": [p for p in job.get("live_profiles", []) if _has_contact(p)]
    }
    if job.get("status") == "done":
        body["result"] = job.get("result")
    elif job.get("status") == "error":
        body["error"] = job.get("error")

    return jsonify(body)


@scraper_bp.route('/api/scrape/cancel/<job_id>', methods=['POST'])
def api_scrape_cancel(job_id):
    if 'user_id' not in session:
        return jsonify({"error": "Unauthorized access"}), 401

    db = get_db()
    job = db.scrape_jobs.find_one({"job_id": job_id})
    if not job or str(job.get("user_id")) != str(session['user_id']):
        return jsonify({"error": "Search job not found."}), 404

    with _ACTIVE_LOCK:
        event = _ACTIVE_CANCEL_EVENTS.get(job_id)
        if event:
            event.set()

    if event is None and job.get("status") == "running":
        # Nothing in this process is running it (crash / restart): close it so it stops blocking new searches.
        db.scrape_jobs.update_one(
            {"job_id": job_id},
            {"$set": {"status": "error", "error": "Search cancelled.", "message": "Cancelled",
                      "updated_at": datetime.now(timezone.utc)}})
        return jsonify({"ok": True, "released": True})

    db.scrape_jobs.update_one(
        {"job_id": job_id},
        {"$set": {"message": "Cancelling - finishing current step...", "updated_at": datetime.now(timezone.utc)}}
    )
    return jsonify({"ok": True})


@scraper_bp.route('/api/scrape', methods=['POST'])
def api_scrape():
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
        profiles = list(db.linkedin_profiles.find({
            "scraped_by_user_id": str(session['user_id']),
            "$or": [
                {"emails.0": {"$exists": True}},
                {"contact_numbers.0": {"$exists": True}},
                {"contact_number": {"$nin": [None, ""]}},
            ],
        }).sort("last_scraped_at", -1).limit(500))

    profiles = [p for p in profiles if _has_contact(p)]
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


# ==============================================================================
# ADMIN REPOSITORY ENDPOINTS (ROBUST MULTI-PARAM & WORD BOUNDARY FILTERS)
# ==============================================================================

_PLATFORM_FILTERS = {
    "linkedin":  ("LinkedIn",     r"^\s*linkedin\s*$"),
    "github":    ("GitHub",       r"^\s*git\s*hub\s*$"),
    "yc":        ("Y Combinator", r"^\s*(y\s*combinator|yc|ycombinator)\s*$"),
    "wellfound": ("Wellfound",    r"^\s*(wellfound|angel\s*list|angellist)(\s*\(.*\))?\s*$"),
}


def _platform_key(value: str) -> str:
    v = re.sub(r"[^a-z0-9]", "", (value or "").lower())
    if not v:
        return ""
    if v.startswith("linkedin"):
        return "linkedin"
    if v.startswith("github"):
        return "github"
    if v in ("yc", "ycombinator") or v.startswith("ycombinator"):
        return "yc"
    if v.startswith("wellfound") or v.startswith("angellist") or v == "angel":
        return "wellfound"
    return v


def _canonical_platform_label(value: str) -> str:
    key = _platform_key(value)
    return _PLATFORM_FILTERS[key][0] if key in _PLATFORM_FILTERS else (value or "")


def _build_admin_query_filter(search_term: str, platform_filter: str, user_filter: str, has_phone: str, has_email: str) -> dict:
    and_conditions = []

    # 1. Search Query Filter (Word boundaries for short terms like HR, AI, ML)
    if search_term:
        term = search_term.strip()
        escaped_term = re.escape(term)
        pattern = f"\\b{escaped_term}\\b" if (len(term) <= 3 and term.isalnum()) else escaped_term

        and_conditions.append({
            "$or": [
                {"name": {"$regex": pattern, "$options": "i"}},
                {"headline": {"$regex": pattern, "$options": "i"}},
                {"bio": {"$regex": pattern, "$options": "i"}},
                {"snippet": {"$regex": pattern, "$options": "i"}},
                {"company": {"$regex": pattern, "$options": "i"}},
                {"last_query": {"$regex": pattern, "$options": "i"}}
            ]
        })

    # 2. Platform Filter
    pf_key = _platform_key(platform_filter) if platform_filter and platform_filter.strip().lower() not in ('all', 'all platforms', '', 'none') else ""
    if pf_key:
        if pf_key in _PLATFORM_FILTERS:
            pf_regex = _PLATFORM_FILTERS[pf_key][1]
        else:
            clean_pf = re.escape(platform_filter.strip())
            pf_regex = f"^{clean_pf}$"

        if pf_key == "linkedin":
            and_conditions.append({
                "$or": [
                    {"platform": {"$regex": pf_regex, "$options": "i"}},
                    {"platform": {"$exists": False}},
                    {"platform": None},
                    {"platform": ""}
                ]
            })
        else:
            and_conditions.append({"platform": {"$regex": pf_regex, "$options": "i"}})

    # 3. User ID Filter
    if user_filter and user_filter.lower() not in ('all', 'all users', '', 'none'):
        and_conditions.append({"scraped_by_user_id": user_filter.strip()})

    # 4. Phone Filter
    if str(has_phone).lower() == 'true':
        and_conditions.append({
            "$or": [
                {"contact_numbers.0": {"$exists": True}},
                {"contact_number": {"$nin": [None, ""]}}
            ]
        })
    elif str(has_phone).lower() == 'false':
        and_conditions.append({
            "$and": [
                {"$or": [{"contact_numbers": {"$exists": False}}, {"contact_numbers": {"$size": 0}}, {"contact_numbers": None}]},
                {"$or": [{"contact_number": {"$exists": False}}, {"contact_number": None}, {"contact_number": ""}]}
            ]
        })

    # 5. Email Filter
    if str(has_email).lower() == 'true':
        and_conditions.append({"emails.0": {"$exists": True}})
    elif str(has_email).lower() == 'false':
        and_conditions.append({
            "$or": [
                {"emails": {"$exists": False}},
                {"emails": {"$size": 0}},
                {"emails": None}
            ]
        })

    return {"$and": and_conditions} if and_conditions else {}


@scraper_bp.route('/api/admin/scraped-data', methods=['GET'])
def get_admin_scraped_data():
    if session.get('role') != 'admin' and not session.get('is_admin'):
        return jsonify({"success": False, "error": "Admin unauthorized"}), 403

    try:
        db = get_db()
        page = max(1, int(request.args.get('page', 1)))
        limit = max(1, int(request.args.get('limit', 20)))
        skip = (page - 1) * limit

        search_term = (
            request.args.get('query', '') or 
            request.args.get('search', '') or 
            request.args.get('q', '') or 
            request.args.get('search_term', '')
        ).strip()

        platform_filter = (
            request.args.get('platform', '') or 
            request.args.get('platform_filter', '')
        ).strip()

        user_filter = (
            request.args.get('user_id', '') or 
            request.args.get('user', '') or 
            request.args.get('scraped_by_user_id', '')
        ).strip()

        has_phone = (
            request.args.get('has_phone', '') or 
            request.args.get('phone', '')
        ).strip()

        has_email = (
            request.args.get('has_email', '') or 
            request.args.get('email', '')
        ).strip()

        query_filter = _build_admin_query_filter(search_term, platform_filter, user_filter, has_phone, has_email)

        total = db.linkedin_profiles.count_documents(query_filter)
        profiles = list(
            db.linkedin_profiles.find(query_filter)
            .sort("last_scraped_at", -1)
            .skip(skip)
            .limit(limit)
        )

        for p in profiles:
            p.pop('_id', None)

        available_users = db.linkedin_profiles.distinct("scraped_by_user_id")
        total_pages = (total + limit - 1) // limit if total > 0 else 1

        return jsonify({
            "success": True,
            "profiles": profiles,
            "total": total,
            "page": page,
            "total_pages": total_pages,
            "available_users": available_users
        })

    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


@scraper_bp.route('/api/admin/export/excel', methods=['GET', 'POST'])
def export_admin_global_scraped_excel():
    if session.get('role') != 'admin' and not session.get('is_admin'):
        return jsonify({"error": "Admin unauthorized"}), 403

    db = get_db()

    search_term = (
        request.args.get('query', '') or 
        request.args.get('search', '') or 
        request.args.get('q', '') or 
        request.args.get('search_term', '')
    ).strip()

    platform_filter = (
        request.args.get('platform', '') or 
        request.args.get('platform_filter', '')
    ).strip()

    user_filter = (
        request.args.get('user_id', '') or 
        request.args.get('user', '') or 
        request.args.get('scraped_by_user_id', '')
    ).strip()

    has_phone = (
        request.args.get('has_phone', '') or 
        request.args.get('phone', '')
    ).strip()

    has_email = (
        request.args.get('has_email', '') or 
        request.args.get('email', '')
    ).strip()

    query_filter = _build_admin_query_filter(search_term, platform_filter, user_filter, has_phone, has_email)

    profiles = list(db.linkedin_profiles.find(query_filter).sort("last_scraped_at", -1))
    df = _format_profiles_for_excel(profiles)

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Global Repository')
    output.seek(0)

    return send_file(
        output,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name='global_scraped_leads_repository.xlsx'
    )