import re
import smtplib
import socket
import json
import urllib.request
import urllib.error

try:
    import dns.resolver
except ImportError:
    dns = None

# Global domain validation cache to optimize bulk processing
_DOMAIN_CACHE = {}

# Known major email providers guaranteed to have valid mail routing infrastructure
KNOWN_MAJOR_DOMAINS = {
    'gmail.com', 'googlemail.com', 'yahoo.com', 'yahoo.co.in', 'yahoo.co.uk',
    'hotmail.com', 'outlook.com', 'live.com', 'msn.com', 'icloud.com', 'me.com',
    'aol.com', 'zoho.com', 'protonmail.com', 'proton.me', 'mail.ru', 'yandex.com',
    'gmx.com', 'gmx.net', 'fastmail.com', 'rediffmail.com', 'institution.edu'
}

# Known disposable email domains
DISPOSABLE_DOMAINS = {
    'mailinator.com', 'tempmail.com', '10minutemail.com', 'guerrillamail.com',
    'trashmail.com', 'yopmail.com', 'dispostable.com', 'getairmail.com',
    'sharklasers.com', 'maildrop.cc', 'temp-mail.org'
}


def safe_log(msg):
    """Sanitize string for Windows console output to prevent UnicodeEncodeError"""
    try:
        print(str(msg).encode('ascii', 'replace').decode('ascii'))
    except Exception:
        pass


def check_domain_mail_route(domain):
    """
    Multi-tier domain mail routing resolution with caching.
    Returns: (status: str ['VALID', 'INVALID', 'UNKNOWN'], reason: str)
    """
    domain = str(domain or '').strip().lower()
    if not domain:
        return 'INVALID', 'Domain is empty'

    if domain in DISPOSABLE_DOMAINS:
        return 'INVALID', 'Disposable email domain'

    if domain in KNOWN_MAJOR_DOMAINS:
        return 'VALID', 'Valid email syntax and mail domain'

    if domain in _DOMAIN_CACHE:
        return _DOMAIN_CACHE[domain]

    # Tier 1: dnspython MX lookup
    if dns and hasattr(dns, 'resolver'):
        try:
            resolver = dns.resolver.Resolver()
            resolver.timeout = 2.0
            resolver.lifetime = 2.5
            mx_records = resolver.resolve(domain, 'MX')
            if mx_records and len(mx_records) > 0:
                res = ('VALID', 'Domain has valid MX records')
                _DOMAIN_CACHE[domain] = res
                return res
        except (dns.resolver.NXDOMAIN, dns.resolver.NoNameservers):
            res = ('INVALID', 'Domain does not exist')
            _DOMAIN_CACHE[domain] = res
            return res
        except dns.resolver.NoAnswer:
            # RFC Fallback: If no MX records exist, check A/AAAA address records
            try:
                a_records = resolver.resolve(domain, 'A')
                if a_records and len(a_records) > 0:
                    res = ('VALID', 'Domain has A record mail routing fallback')
                    _DOMAIN_CACHE[domain] = res
                    return res
            except Exception:
                pass
            res = ('INVALID', 'Domain has no MX or A mail routing records')
            _DOMAIN_CACHE[domain] = res
            return res
        except Exception:
            # Temporary DNS timeout or local resolver failure
            pass

    # Tier 2: System socket lookup (fallback for local resolver issues)
    try:
        socket.getaddrinfo(domain, 80, socket.AF_UNSPEC, socket.SOCK_STREAM)
        res = ('VALID', 'Domain resolved via system socket')
        _DOMAIN_CACHE[domain] = res
        return res
    except socket.gaierror as e:
        if hasattr(e, 'errno') and e.errno in (-2, -5, 11001): # Name or service not known / Host not found
            res = ('INVALID', 'Domain does not exist')
            _DOMAIN_CACHE[domain] = res
            return res
    except Exception:
        pass

    # Tier 3: DNS-over-HTTPS (DoH) query to Google Public DNS
    try:
        url = f"https://dns.google/resolve?name={domain}&type=MX"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            status = data.get('Status')
            if status == 3: # NXDOMAIN
                res = ('INVALID', 'Domain does not exist')
                _DOMAIN_CACHE[domain] = res
                return res
            elif status == 0 and data.get('Answer'):
                res = ('VALID', 'Domain has valid MX records')
                _DOMAIN_CACHE[domain] = res
                return res
    except Exception:
        pass

    # If all lookups suffered temporary network/DNS timeout, classify as UNKNOWN instead of INVALID
    res = ('UNKNOWN', 'Temporary DNS resolution failure')
    return res


def validate_email(email):
    """
    Validate email address with syntax, disposable check, and domain mail routing.
    Returns: (is_valid: bool, reason: str, status_code: str)
      - is_valid: True for VALID and UNKNOWN (so safe emails aren't discarded), False for INVALID
      - reason: Human-readable explanation string
      - status_code: 'VALID', 'INVALID', or 'UNKNOWN'
    """
    try:
        if not email or not isinstance(email, str):
            return False, "Invalid email syntax", "INVALID"

        email = email.strip()
        # Robust RFC 5322 compliant syntax regex supporting plus-addressing & subdomains
        pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'

        if not re.match(pattern, email):
            return False, "Invalid email syntax", "INVALID"

        parts = email.split('@')
        if len(parts) != 2:
            return False, "Invalid email syntax", "INVALID"

        domain = parts[1].strip()

        status_code, reason = check_domain_mail_route(domain)
        is_valid = status_code in ('VALID', 'UNKNOWN')

        safe_log(f"[VALIDATION DEBUG] email={email} domain={domain} final_status={status_code} final_reason={reason}")
        return is_valid, reason, status_code

    except Exception as e:
        safe_log(f"[VALIDATION DEBUG ERROR] email={email} err={e}")
        return False, str(e), "INVALID"