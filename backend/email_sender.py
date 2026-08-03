"""
Email Sender Service
Handles email sending with rotation logic
Support for both TLS and SSL connections
"""

import os
import ssl
import smtplib
import mimetypes
import time
import socket
import re
import traceback
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
from email.mime.base import MIMEBase
from email import encoders
from email.utils import formatdate, make_msgid


class EmailSender:
    """Email sender with SMTP pooling & Gmail-safe rotation"""
    
    def __init__(self, email_accounts, batch_size=25, user_id=None):
        """
        Initialize with configurable batch_size (default 25 emails per account)
        """
        self.email_accounts = email_accounts or []
        self.batch_size = batch_size
        self.user_id = user_id
        self.current_account_index = 0
        self.total_sent = 0
        self.failed = []
        self.sent_entries = []
        self.server = None  # SMTP Connection
        self.connected_account_email = None
        self.current_account = None
        self.last_rotation = 0
        
    def get_current_account(self):
        """Get the current email account to use"""
        if not self.email_accounts:
            self.current_account = None
            return None
        if self.current_account_index >= len(self.email_accounts):
            self.current_account_index = 0
        account = self.email_accounts[self.current_account_index]
        self.current_account = account
        return account
    
    def get_account_sent_count(self):
        """Get sent count for current account from DB-synced data"""
        current = self.get_current_account()
        if current and isinstance(current, dict):
            return current.get('emails_sent', 0)
        return float('inf')
    
    def increment_current_account(self):
        """Increment sent count for current account (DB-synced)"""
        current = self.get_current_account()
        if current and isinstance(current, dict):
            current['emails_sent'] = current.get('emails_sent', 0) + 1
            self.total_sent += 1
            account_id = current.get('_id')
            if account_id:
                try:
                    from models import EmailID
                    EmailID.increment_sent_count(account_id)
                except Exception as db_error:
                    print(f"Failed to persist sent count for {current.get('email')}: {db_error}")
            print(f"📊 {current.get('email')}: {current.get('emails_sent', 0)}/{self.batch_size}")
    
    def needs_rotation(self):
        """Check if current account needs rotation (DB-driven)"""
        current = self.get_current_account()
        if not current:
            return True
        count = self.get_account_sent_count()
        print(f"🔍 {current.get('email')}: {count}/{self.batch_size}")
        if count >= self.batch_size:
            print(f"🚫 LIMIT REACHED for {current.get('email')}")
            return True
        return False
    
    def find_next_available_account(self):
        """Find next account with sent_count < batch_size"""
        if not self.email_accounts:
            return False
        total_accounts = len(self.email_accounts)
        start_index = self.current_account_index
        
        for i in range(total_accounts):
            self.current_account_index = (start_index + i) % total_accounts
            if not self.needs_rotation():
                current = self.get_current_account()
                if current:
                    print(f"✅ Selected: {current.get('email')} ({self.get_account_sent_count()}/{self.batch_size})")
                    return True
        
        # All accounts exhausted
        print("🔄 ALL ACCOUNTS EXHAUSTED - Need reset!")
        return False
    
    def switch_account(self):
        print("🔁 Rotating to next available account...")
        if not self.find_next_available_account():
            print("⚠️  No available accounts - reset required")
        else:
            current = self.get_current_account()
            if current:
                print(f"🔄 Now using: {current.get('email')}")

    def _html_to_plain_text(self, html):
        """Create readable plain-text fallback from HTML body."""
        if not html:
            return ""
        text = re.sub(r'(?is)<(script|style).*?>.*?</\1>', '', html)
        text = re.sub(r'(?i)<br\s*/?>', '\n', text)
        text = re.sub(r'(?i)</(p|div|li|h1|h2|h3|h4|h5|h6)>', '\n', text)
        text = re.sub(r'(?is)<[^>]+>', '', text)
        text = text.replace('&nbsp;', ' ').replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text.strip()

    def _build_logo_part(self, custom_logo=None):
        """Build MIME inline image part for custom or default logo."""
        # 1. Custom uploaded logo
        if custom_logo and isinstance(custom_logo, dict):
            try:
                c_data = custom_logo.get('data')
                c_filename = custom_logo.get('filename') or 'company_logo.png'
                c_mime = custom_logo.get('mimetype') or mimetypes.guess_type(c_filename)[0] or 'image/png'
                if c_data:
                    main_type, sub_type = c_mime.split('/', 1) if '/' in c_mime else ('image', 'png')
                    sub_type = sub_type.lower()
                    if sub_type in ['jpg', 'pjpeg']:
                        sub_type = 'jpeg'
                    elif sub_type in ['svg+xml', 'svg']:
                        sub_type = 'svg'

                    try:
                        part = MIMEImage(c_data, _subtype=sub_type)
                    except Exception:
                        part = MIMEBase(main_type, sub_type)
                        part.set_payload(c_data)
                        encoders.encode_base64(part)

                    part.add_header('Content-ID', '<company_logo>')
                    part.add_header('Content-Disposition', 'inline', filename=c_filename)
                    part.add_header('Content-Location', c_filename)
                    part.add_header('X-Attachment-Id', 'company_logo')
                    part.set_param('name', c_filename)
                    print(f"✅ Custom inline logo attached ({len(c_data)} bytes, {c_mime}) as CID <company_logo>")
                    return part
            except Exception as c_err:
                print(f"⚠️ Custom logo attach error: {c_err}")

        # 2. Fallback to default company logo on disk
        possible_logo_paths = [
            os.path.join(os.getcwd(), "backend", "uploads", "logo", "company_logo.jpeg"),
            os.path.join(os.getcwd(), "uploads", "logo", "company_logo.jpeg"),
            os.path.join(os.path.dirname(__file__), "uploads", "logo", "company_logo.jpeg"),
            os.path.join(os.path.dirname(__file__), "..", "uploads", "logo", "company_logo.jpeg"),
            os.path.join(os.path.dirname(__file__), "backend", "uploads", "logo", "company_logo.jpeg")
        ]
        for logo_path in possible_logo_paths:
            if os.path.exists(logo_path):
                try:
                    with open(logo_path, 'rb') as f:
                        img_data = f.read()
                    if img_data:
                        mime_type, _ = mimetypes.guess_type(logo_path)
                        sub_type = mime_type.split('/', 1)[1] if mime_type and '/' in mime_type else 'jpeg'
                        if sub_type.lower() in ['jpg', 'pjpeg']:
                            sub_type = 'jpeg'
                        try:
                            part = MIMEImage(img_data, _subtype=sub_type)
                        except Exception:
                            part = MIMEBase('image', sub_type)
                            part.set_payload(img_data)
                            encoders.encode_base64(part)

                        filename = os.path.basename(logo_path)
                        part.add_header('Content-ID', '<company_logo>')
                        part.add_header('Content-Disposition', 'inline', filename=filename)
                        part.add_header('Content-Location', filename)
                        part.add_header('X-Attachment-Id', 'company_logo')
                        part.set_param('name', filename)
                        print(f"✅ Default inline logo attached ({len(img_data)} bytes) from: {logo_path}")
                        return part
                except Exception as e:
                    print(f"⚠️ Default logo attach warning: {e}")

        return None

    def create_email_message(self, to_email, subject, body, from_name="Sender", cc_emails=None, bcc_emails=None, attachments=None, is_html=False, custom_logo=None):
        """Create MIME-safe email with plain or HTML body + optional attachments and inline CID logo."""
        account = self.get_current_account()
        if not account:
            return None

        if attachments is None:
            attachments = []

        sender_email = str(account.get('email') or '').strip()
        from_header = f"{from_name} <{sender_email}>" if from_name else sender_email

        sanitized_cc = []
        if cc_emails:
            if isinstance(cc_emails, str):
                sanitized_cc = [c.strip() for c in cc_emails.split(',') if c.strip()]
            elif isinstance(cc_emails, list):
                sanitized_cc = [str(c).strip() for c in cc_emails if str(c).strip()]

        sanitized_bcc = []
        if bcc_emails:
            if isinstance(bcc_emails, str):
                sanitized_bcc = [b.strip() for b in bcc_emails.split(',') if b.strip()]
            elif isinstance(bcc_emails, list):
                sanitized_bcc = [str(b).strip() for b in bcc_emails if str(b).strip()]

        body_str = str(body or '')

        if is_html:
            # 1. Create multipart/alternative with plain-text fallback and HTML
            alt_part = MIMEMultipart('alternative')
            plain_fallback = self._html_to_plain_text(body_str)
            alt_part.attach(MIMEText(plain_fallback or body_str, 'plain', 'utf-8'))
            alt_part.attach(MIMEText(body_str, 'html', 'utf-8'))

            # 2. If there is an inline logo, wrap in multipart/related (RFC 2387)
            logo_part = self._build_logo_part(custom_logo)
            if logo_part is not None:
                related_part = MIMEMultipart('related')
                related_part.attach(alt_part)
                related_part.attach(logo_part)
                body_part = related_part
            else:
                body_part = alt_part
        else:
            body_part = MIMEText(body_str, 'plain', 'utf-8')

        # 3. If file attachments exist, use multipart/mixed at root
        if attachments and len(attachments) > 0:
            msg = MIMEMultipart('mixed')
            msg.attach(body_part)

            for att_path in attachments:
                try:
                    if not att_path or not os.path.exists(att_path):
                        print(f"⚠️ Attachment not found: {att_path}")
                        continue

                    file_size = os.path.getsize(att_path)
                    with open(att_path, "rb") as f:
                        file_data = f.read()

                    mime_type, _ = mimetypes.guess_type(att_path)
                    if mime_type and "/" in mime_type:
                        main_type, sub_type = mime_type.split("/", 1)
                    else:
                        main_type, sub_type = "application", "octet-stream"

                    part = MIMEBase(main_type, sub_type)
                    part.set_payload(file_data)
                    encoders.encode_base64(part)

                    filename = os.path.basename(att_path)
                    part.add_header(
                        "Content-Disposition",
                        f'attachment; filename="{filename}"'
                    )
                    part.set_param('name', filename)
                    msg.attach(part)
                    print(f"✅ Attached file: {filename} ({file_size} bytes, MIME: {main_type}/{sub_type})")

                except Exception as e:
                    print(f"❌ Attachment error for {att_path}: {e}")
        else:
            msg = body_part

        # Set standard email headers on root message
        msg['From'] = from_header
        msg['To'] = str(to_email or '').strip()
        if sanitized_cc:
            msg['Cc'] = ", ".join(sanitized_cc)
        if sanitized_bcc:
            msg['Bcc'] = ", ".join(sanitized_bcc)
        msg['Subject'] = str(subject or '')
        msg['Date'] = formatdate(localtime=True)
        msg['Message-ID'] = make_msgid()

        return msg
    
    def test_smtp_connection(self):
        """Test SMTP connection for current account"""
        conn_ok, conn_error = self.ensure_connection()
        if not conn_ok:
            return False, conn_error
        return True, "SMTP connection successful"

    def ensure_connection(self):
        """Ensure active connection for current account"""
        account = self.get_current_account()
        if not account:
            return False, "No sender accounts available"

        email_addr = account.get('email')
        if not email_addr:
            return False, "Current account missing email"

        # Check if already connected with this account
        if self.server and self.connected_account_email == email_addr:
            try:
                status = self.server.noop()[0]
                if status == 250:
                    return True, None
            except Exception:
                self.server = None
                self.connected_account_email = None

        # Disconnect previous connection
        if self.server:
            try:
                self.server.quit()
            except Exception:
                pass
            self.server = None
            self.connected_account_email = None

        # Build fresh connection
        smtp_server = account.get('smtp_server', 'smtp.gmail.com')
        smtp_port = int(account.get('smtp_port', 587))
        use_tls = account.get('use_tls', True)
        password = account.get('password', '')

        print(f"🔌 Connecting to SMTP: {smtp_server}:{smtp_port} for {email_addr} (TLS: {use_tls})")
        try:
            if smtp_port == 465:
                context = ssl.create_default_context()
                self.server = smtplib.SMTP_SSL(smtp_server, smtp_port, context=context, timeout=30)
            else:
                self.server = smtplib.SMTP(smtp_server, smtp_port, timeout=30)
                self.server.ehlo()
                if use_tls:
                    context = ssl.create_default_context()
                    self.server.starttls(context=context)
                    self.server.ehlo()

            clean_password = str(password or '').replace(" ", "")
            try:
                self.server.login(email_addr, clean_password)
            except smtplib.SMTPAuthenticationError:
                self.server.login(email_addr, password)

            self.connected_account_email = email_addr
            print(f"✅ SMTP connected & authenticated for {email_addr}")
            return True, None

        except smtplib.SMTPAuthenticationError as auth_err:
            self.server = None
            self.connected_account_email = None
            err_msg = f"Authentication failed for {email_addr}. Check App Password."
            print(f"❌ {err_msg}: {auth_err}")
            return False, err_msg
        except (smtplib.SMTPConnectError, socket.error, socket.timeout) as conn_err:
            self.server = None
            self.connected_account_email = None
            err_msg = f"Network/Connection error for {email_addr} ({smtp_server}:{smtp_port}): {conn_err}"
            print(f"❌ {err_msg}")
            return False, f"Could not connect to SMTP server {smtp_server}:{smtp_port}: {conn_err}"
        except Exception as e:
            return False, f"SMTP Connection error for {email_addr}: {str(e)}"
    
    def send_single_email(self, to_email, subject, body, from_name="Sender", cc_emails=None, bcc_emails=None, attachments=None, is_html=False, custom_logo=None):
        """
        Send a single email with robust error handling and retry
        """
        # Ensure active SMTP connection for current account
        conn_ok, conn_error = self.ensure_connection()
        if not conn_ok:
            print(f"❌ Connection failed: {conn_error}")
            return False, conn_error
        
        try:
            print("📝 [Step 6] Preparing message...")
            msg = self.create_email_message(
                to_email=to_email,
                subject=subject,
                body=body,
                from_name=from_name,
                cc_emails=cc_emails,
                bcc_emails=bcc_emails,
                attachments=attachments,
                is_html=is_html,
                custom_logo=custom_logo
            )
            if not msg:
                return False, "Failed to construct MIME email message"
            
            # Destination recipients
            recipients = [str(to_email).strip()]
            if cc_emails:
                if isinstance(cc_emails, str):
                    recipients.extend([c.strip() for c in cc_emails.split(',') if c.strip()])
                elif isinstance(cc_emails, list):
                    recipients.extend([str(c).strip() for c in cc_emails if str(c).strip()])
            if bcc_emails:
                if isinstance(bcc_emails, str):
                    recipients.extend([b.strip() for b in bcc_emails.split(',') if b.strip()])
                elif isinstance(bcc_emails, list):
                    recipients.extend([str(b).strip() for b in bcc_emails if str(b).strip()])

            account = self.get_current_account() or {}
            sender_email = str(account.get('email') or self.connected_account_email or '').strip()
            if not sender_email:
                return False, "Sender email address is not configured"

            msg_raw = msg.as_string()
            print("=" * 60)
            print("🚀 [Step 7] Sending email via SMTP:")
            print(f"   Subject: {subject}")
            print(f"   From: {from_name} <{sender_email}>")
            print(f"   To: {to_email}")
            print(f"   CC: {cc_emails}")
            print(f"   BCC: {bcc_emails}")
            print(f"   All SMTP Envelope Recipients: {recipients}")
            print(f"   Attachment count: {len(attachments) if attachments else 0}")
            print(f"   HTML mode: {is_html}")
            print(f"   Custom Logo: {'Yes' if custom_logo else 'No (Default/None)'}")
            print(f"   Content-Type: {msg.get_content_type()}")
            print("--- MIME MESSAGE HEADERS ---")
            for header_name, header_val in msg.items():
                print(f"   {header_name}: {header_val}")
            print("=" * 60)
            
            # Retry loop
            last_err = None
            for attempt in range(2):
                try:
                    self.server.sendmail(
                        sender_email, 
                        recipients, 
                        msg_raw
                    )
                    print(f"✅ Email successfully delivered to {to_email} via {sender_email}")
                    return True, None
                except (smtplib.SMTPServerDisconnected, smtplib.SMTPSenderRefused) as net_err:
                    last_err = str(net_err)
                    print(f"🔄 Retrying send ({attempt+1}/2) due to: {net_err}")
                    self.server = None
                    self.connected_account_email = None
                    conn_ok, _ = self.ensure_connection()
                    if not conn_ok:
                        break
                except smtplib.SMTPRecipientsRefused as recip_err:
                    return False, f"Recipient address refused: {recip_err}"
                except smtplib.SMTPDataError as data_err:
                    return False, f"SMTP data error: {data_err}"
                except Exception as ex:
                    return False, f"Error sending message: {str(ex)}"
            
            return False, last_err or "Delivery failed after retry"
            
        except Exception as e:
            traceback.print_exc()
            err_msg = f"Unexpected send error: {str(e)}"
            print(f"❌ {err_msg}")
            self.server = None
            self.connected_account_email = None
            return False, err_msg
    
    def send_bulk_emails(self, recipients, subject, body, from_name="Sender", cc_emails=None, bcc_emails=None, attachments=None, is_html=False, delay_between_emails=1, separate_threads=False, custom_logo=None):
        """
        Send emails to multiple recipients with rotation
        """
        if attachments is None:
            attachments = []
        
        total_recipients = len(recipients)
        print(f"\n📧 Starting bulk email send...")
        print(f"📊 Total recipients: {total_recipients}")
        print(f"📊 Batch size: {self.batch_size} emails per account")
        print(f"📊 Number of accounts: {len(self.email_accounts)}")
        print(f"📧 From: {from_name}")
        print(f"📝 Subject: {subject}\n")

        for index, recipient in enumerate(recipients, 1):
            if isinstance(recipient, dict):
                to_email = recipient.get('email', '')
                personalized_body = recipient.get('body', body)
            else:
                to_email = recipient
                personalized_body = body

            email_subject = subject
            if separate_threads:
                thread_token = datetime.utcnow().strftime('%Y%m%d%H%M%S') + f"-{index:04d}"
                email_subject = f"{subject} | Ref:{thread_token}"
            
            # Check rotation BEFORE every send
            if self.needs_rotation():
                if not self.find_next_available_account():
                    print("🔄 All accounts exhausted. Resetting...")
                    if self.user_id:
                        try:
                            from models import EmailID
                            EmailID.reset_counts(self.user_id)
                        except Exception as db_error:
                            print(f"Failed to reset DB counters: {db_error}")

                    for acc in self.email_accounts:
                        acc['emails_sent'] = 0
                    self.current_account_index = 0
            
            print(f"[{index}/{total_recipients}] Sending to {to_email}...", end=" ")
            
            success, error_msg = self.send_single_email(
                to_email=to_email,
                subject=email_subject,
                body=personalized_body,
                from_name=from_name,
                cc_emails=cc_emails,
                bcc_emails=bcc_emails,
                attachments=attachments,
                is_html=is_html,
                custom_logo=custom_logo
            )
            
            current = self.get_current_account()
            if success:
                print(f"✅ Sent (Account: {current.get('email') if current else 'Unknown'})")
                self.increment_current_account()
                self.sent_entries.append({
                    'email': to_email,
                    'sender_email_id': current.get('_id') if current else None
                })
            else:
                print(f"❌ Failed: {error_msg}")
                self.failed.append({
                    'email': to_email,
                    'error': error_msg or 'Send failed',
                    'sender_email_id': current.get('_id') if current else None
                })
            
            # Delay between sends
            if index < total_recipients and delay_between_emails > 0:
                time.sleep(delay_between_emails)
        
        # Close SMTP connection when done
        if self.server:
            try:
                self.server.quit()
            except Exception:
                pass
            self.server = None

        self.print_summary()
        
        return {
            "total_sent": self.total_sent,
            "sent_entries": self.sent_entries,
            "failed": self.failed,
            "total_recipients": total_recipients
        }
    
    def print_summary(self):
        """Print sending summary"""
        print("\n" + "="*50)
        print("📊 SENDING SUMMARY")
        print("="*50)
        print(f"✅ Total emails sent: {self.total_sent}")
        print(f"❌ Failed: {len(self.failed)}")
        print(f"📧 Accounts used: {self.current_account_index + 1}")
        print("="*50)
        
        if self.failed:
            print("\n❌ Failed recipients:")
            for fail in self.failed:
                print(f"   {fail.get('email')}: {fail.get('error')}")
    
    def reset_counters(self):
        """Reset for new session"""
        self.current_account_index = 0
        self.total_sent = 0
        self.failed = []
        self.sent_entries = []
        print("🔄 EmailSender local counters reset")
    
    def set_initial_counts(self, counts_dict):
        """Set initial email counts from database values"""
        if not counts_dict:
            return
        for email_addr, count in counts_dict.items():
            for account in self.email_accounts:
                if account.get('email', '').lower() == str(email_addr).lower():
                    account['db_sent_count'] = count
        print(f"📊 Initialized email counts: {counts_dict}")