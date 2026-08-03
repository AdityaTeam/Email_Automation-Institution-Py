"""
Email Sender Service
Handles email sending with rotation logic
Support for both TLS and SSL connections
"""

import os
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
from email.utils import formatdate


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
            return None
        if self.current_account_index >= len(self.email_accounts):
            self.current_account_index = 0
        return self.email_accounts[self.current_account_index]
    
    def get_account_sent_count(self):
        """Get sent count for current account from DB-synced data"""
        current = self.get_current_account()
        if current:
            return current.get('emails_sent', 0)
        return float('inf')
    
    def increment_current_account(self):
        """Increment sent count for current account (DB-synced)"""
        current = self.get_current_account()
        if current:
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

    def create_email_message(self, to_email, subject, body, from_name="Sender", cc_emails=None, bcc_emails=None, attachments=None, is_html=False):
        """Create MIME-safe email with plain or HTML body + optional attachments."""
        account = self.get_current_account()
        if not account:
            return None

        if attachments is None:
            attachments = []

        msg = MIMEMultipart('mixed')
        sender_email = str(account.get('email') or '').strip()
        from_header = f"{from_name} <{sender_email}>" if from_name else sender_email
        msg['From'] = from_header
        msg['To'] = str(to_email or '').strip()

        sanitized_cc = []
        if cc_emails:
            if isinstance(cc_emails, str):
                sanitized_cc = [c.strip() for c in cc_emails.split(',') if c.strip()]
            elif isinstance(cc_emails, list):
                sanitized_cc = [str(c).strip() for c in cc_emails if str(c).strip()]
            if sanitized_cc:
                msg['Cc'] = ", ".join(sanitized_cc)

        sanitized_bcc = []
        if bcc_emails:
            if isinstance(bcc_emails, str):
                sanitized_bcc = [b.strip() for b in bcc_emails.split(',') if b.strip()]
            elif isinstance(bcc_emails, list):
                sanitized_bcc = [str(b).strip() for b in bcc_emails if str(b).strip()]
            if sanitized_bcc:
                msg['Bcc'] = ", ".join(sanitized_bcc)

        msg['Subject'] = str(subject or '')
        msg['Date'] = formatdate(localtime=True)

        body_str = str(body or '')
        if is_html:
            alternative_part = MIMEMultipart('alternative')
            plain_fallback = self._html_to_plain_text(body_str)
            alternative_part.attach(MIMEText(plain_fallback or body_str, 'plain', 'utf-8'))
            alternative_part.attach(MIMEText(body_str, 'html', 'utf-8'))
            msg.attach(alternative_part)
        else:
            msg.attach(MIMEText(body_str, 'plain', 'utf-8'))

        # Safe logo attachment
        possible_logo_paths = [
            os.path.join(os.getcwd(), "backend", "uploads", "logo", "company_logo.jpeg"),
            os.path.join(os.getcwd(), "uploads", "logo", "company_logo.jpeg"),
            os.path.join(os.path.dirname(__file__), "uploads", "logo", "company_logo.jpeg"),
            os.path.join(os.path.dirname(__file__), "..", "uploads", "logo", "company_logo.jpeg")
        ]
        for logo_path in possible_logo_paths:
            if os.path.exists(logo_path):
                try:
                    with open(logo_path, 'rb') as f:
                        img_data = f.read()
                    if img_data:
                        part = MIMEBase('image', 'jpeg')
                        part.set_payload(img_data)
                        encoders.encode_base64(part)
                        part.add_header('Content-Disposition', 'attachment', filename="company_logo.jpeg")
                        part.add_header('Content-ID', '<company_logo>')
                        msg.attach(part)
                        print(f"✅ Logo attached ({len(img_data)} bytes) from: {logo_path}")
                    break
                except Exception as e:
                    print(f"⚠️ Logo attach warning: {e}")

        # Attach custom files
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
                msg.attach(part)
                print(f"✅ Attached file: {filename} ({file_size} bytes, MIME: {main_type}/{sub_type})")

            except Exception as e:
                print(f"❌ Attachment error for {att_path}: {e}")

        return msg
    
    def ensure_connection(self):
        """Pool-aware connection validation/reconnect with account matching"""
        self.current_account = self.get_current_account()
            
        if not self.current_account:
            return False, "No sender email account configured or available."
        
        email_addr = str(self.current_account.get('email') or '').strip()
        password = str(self.current_account.get('password') or '').strip()
        smtp_server = str(self.current_account.get('smtp_server') or 'smtp.gmail.com').strip()
        smtp_port = int(self.current_account.get('smtp_port') or 587)
        use_ssl = bool(self.current_account.get('use_ssl', False)) or smtp_port == 465
        use_tls = bool(self.current_account.get('use_tls', True)) and not use_ssl

        # Validate existing connection - must belong to same email
        if self.server:
            if self.connected_account_email == email_addr:
                try:
                    status = self.server.noop()[0]
                    if status == 250:
                        self.server.rset()
                        return True, None
                except Exception:
                    print("🔌 Stale connection → Reconnecting...")
            try:
                self.server.quit()
            except Exception:
                pass
            self.server = None
            self.connected_account_email = None
        
        # Connect new SMTP connection
        print("=" * 60)
        print("🔌 [Step 4] SMTP connecting & verifying configuration:")
        print(f"   SMTP Host: {smtp_server}")
        print(f"   SMTP Port: {smtp_port}")
        print(f"   TLS Enabled: {use_tls}")
        print(f"   SSL Enabled: {use_ssl}")
        print(f"   Username: {email_addr}")
        print(f"   Password Provided: {'Yes (length ' + str(len(password)) + ')' if password else 'NO - EMPTY'}")
        print("=" * 60)

        try:
            if use_ssl:
                self.server = smtplib.SMTP_SSL(smtp_server, smtp_port, timeout=30)
            else:
                self.server = smtplib.SMTP(smtp_server, smtp_port, timeout=30)
                self.server.ehlo()
                if use_tls:
                    self.server.starttls()
                    self.server.ehlo()
            
            print(f"🔐 Authenticating with SMTP server for {email_addr}...")
            self.server.login(email_addr, password)
            self.connected_account_email = email_addr
            print(f"✅ [Step 5] SMTP login success & connection authenticated for {email_addr}")
            return True, None
            
        except smtplib.SMTPAuthenticationError as auth_err:
            err_msg = f"SMTP Authentication failed for {email_addr}: (Code {auth_err.smtp_code}) {auth_err.smtp_error.decode('utf-8', errors='ignore') if isinstance(auth_err.smtp_error, bytes) else auth_err.smtp_error}"
            print(f"❌ {err_msg}")
            self.server = None
            self.connected_account_email = None
            return False, err_msg
        except smtplib.SMTPConnectError as conn_err:
            err_msg = f"Failed to connect to SMTP server {smtp_server}:{smtp_port}: {conn_err}"
            print(f"❌ {err_msg}")
            self.server = None
            self.connected_account_email = None
            return False, err_msg
        except socket.timeout:
            err_msg = f"SMTP connection timed out connecting to {smtp_server}:{smtp_port}"
            print(f"❌ {err_msg}")
            self.server = None
            self.connected_account_email = None
            return False, err_msg
        except Exception as e:
            err_msg = f"SMTP connection/login error for {email_addr}: {str(e)}"
            print(f"❌ {err_msg}")
            self.server = None
            self.connected_account_email = None
            return False, err_msg
    
    def send_single_email(self, to_email, subject, body, from_name="Sender", cc_emails=None, bcc_emails=None, attachments=None, is_html=False):
        """Send single email with robust exception handling and detailed diagnostics"""
        self.current_account = self.get_current_account()
        if not self.current_account:
            return False, "No sender account available"

        conn_ok, conn_error = self.ensure_connection()
        if not conn_ok:
            return False, conn_error or "Cannot establish SMTP connection"
        
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
                is_html=is_html
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

            sender_email = str(self.current_account.get('email') or '').strip()
            
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
            print(f"   Plain text body preview: {(body[:80] if body else '')}...")
            print("=" * 60)
            
            # Retry loop
            last_err = None
            for attempt in range(2):
                try:
                    self.server.sendmail(
                        sender_email, 
                        recipients, 
                        msg.as_string()
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
            err_msg = f"Unexpected send error: {str(e)}"
            print(f"❌ {err_msg}")
            self.server = None
            self.connected_account_email = None
            return False, err_msg
    
    def send_bulk_emails(self, recipients, subject, body, from_name="Sender", cc_emails=None, bcc_emails=None, attachments=None, is_html=False, delay_between_emails=1, separate_threads=False):
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
                is_html=is_html
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