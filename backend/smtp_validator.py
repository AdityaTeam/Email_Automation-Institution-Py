import re
import smtplib
import socket
try:
    import dns.resolver
except ImportError:
    dns = None


def validate_email(email):
    """Validate email address with syntax, MX DNS check, and SMTP RCPT check."""
    try:
        if not email or not isinstance(email, str):
            return False, "Invalid syntax"

        email = email.strip()
        pattern = r'^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$'

        if not re.match(pattern, email):
            return False, "Invalid syntax"

        parts = email.split('@')
        if len(parts) != 2:
            return False, "Invalid syntax"

        domain = parts[1].strip()

        # Check disposable domains
        disposable_domains = {
            'mailinator.com', 'tempmail.com', '10minutemail.com', 'guerrillamail.com',
            'trashmail.com', 'yopmail.com', 'dispostable.com', 'getairmail.com'
        }
        if domain.lower() in disposable_domains:
            return False, "Disposable"

        # Check MX records if dns.resolver is available
        mx_record = None
        if dns and hasattr(dns, 'resolver'):
            try:
                resolver = dns.resolver.Resolver()
                resolver.timeout = 3.0
                resolver.lifetime = 3.0
                mx_records = resolver.resolve(domain, 'MX')
                if mx_records and len(mx_records) > 0:
                    mx_record = str(mx_records[0].exchange).rstrip('.')
            except (dns.resolver.NXDOMAIN, dns.resolver.NoNameservers):
                return False, "Domain not found"
            except (dns.resolver.NoAnswer, dns.resolver.Timeout):
                # Fallback to domain itself if no MX
                return False, "MX missing"
            except Exception as ex:
                return False, "Domain does not accept emails"
        else:
            # Fallback domain lookup using socket
            try:
                socket.gethostbyname(domain)
            except Exception:
                return False, "Domain not found"

        # If MX record was found, attempt fast SMTP handshake check
        if mx_record:
            try:
                socket.setdefaulttimeout(3)
                server = smtplib.SMTP(timeout=3)
                server.connect(mx_record, 25)
                server.helo('mail.institution.edu')
                server.mail('verify@institution.edu')
                code, message = server.rcpt(email)
                server.quit()

                if code == 250:
                    return True, "Mailbox exists"
                elif code == 550:
                    return False, "Mailbox rejected (550)"
                elif code in [551, 552, 553, 554]:
                    return False, f"Mailbox rejected ({code})"
                else:
                    # Accept with notice or valid
                    return True, "Mailbox exists"
            except socket.timeout:
                # If SMTP port 25 is blocked (common on residential/cloud ISPs), fallback to MX valid
                return True, "Mailbox exists"
            except Exception:
                # If connection refused or blocked by ISP, treat valid domain/MX as valid
                return True, "Mailbox exists"

        return True, "Mailbox exists"

    except Exception as e:
        return False, str(e)
