import re
import smtplib
import dns.resolver
import socket


def validate_email(email):

    try:

        email = email.strip()

        pattern = r'^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$'

        if not re.match(pattern, email):
            return False, "Invalid email format"

        domain = email.split('@')[1]

        try:
            mx_records = dns.resolver.resolve(domain, 'MX')

        except:
            return False, "Domain does not accept emails"

        mx_record = str(mx_records[0].exchange)

        try:

            socket.setdefaulttimeout(5)

            server = smtplib.SMTP(timeout=5)

            server.connect(mx_record)

            server.helo(server.local_hostname)

            server.mail('test@example.com')

            code, message = server.rcpt(email)

            server.quit()

            if code == 250:
                return True, "Mailbox exists"

            return False, f"Mailbox rejected ({code})"

        except:
            return False, "SMTP verification failed"

    except Exception as e:

        return False, str(e)