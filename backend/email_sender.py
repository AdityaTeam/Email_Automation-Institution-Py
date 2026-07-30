"""
Email Sender Service
Handles email sending with rotation logic
UPDATED: Support for both TLS and SSL connections
"""

import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
import os 
from email.mime.image import MIMEImage
import time
from email.utils import formatdate
import re
import json
import base64
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


class EmailSender:
    """Email sender with SMTP pooling & Gmail-safe rotation"""
    
    def __init__(self, email_accounts, batch_size=25, user_id=None):
        """
        Initialize with configurable batch_size (default 25 emails per account)
        """
        self.email_accounts = email_accounts
        self.batch_size = batch_size
        self.user_id = user_id
        self.current_account_index = 0
        self.total_sent = 0
        self.failed = []
        self.sent_entries = []
        self.server = None  # SMTP Connection Pooling
        self.current_account = None
        self.last_rotation = 0
        
    def get_current_account(self):
        """Get the current email account to use"""
        if not self.email_accounts:
            return None
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
                    print(f"Failed to persist sent count for {current['email']}: {db_error}")
            print(f"📊 {current['email']}: {current['emails_sent']}/{self.batch_size}")
    
    def needs_rotation(self):
        "\"\"Check if current account needs rotation (DB-driven)\"\"\""
        count = self.get_account_sent_count()
        print(f"🔍 {self.get_current_account()['email']}: {count}/{self.batch_size}")
        if count >= self.batch_size:
            print(f"🚫 LIMIT REACHED for {self.get_current_account()['email']}")
            return True
        return False
    
    def find_next_available_account(self):
        """Find next account with sent_count < batch_size"""
        total_accounts = len(self.email_accounts)
        start_index = self.current_account_index
        
        for i in range(total_accounts):
            self.current_account_index = (start_index + i) % total_accounts
            if not self.needs_rotation():
                current = self.get_current_account()
                print(f"✅ Selected: {current['email']} ({self.get_account_sent_count()}/{self.batch_size})")
                return True
        
        # All accounts exhausted
        print("🔄 ALL ACCOUNTS EXHAUSTED - Need reset!")
        return False
    
    def switch_account(self):
        print("🔁 Rotating to next available account...")
        if not self.find_next_available_account():
            print("⚠️  No available accounts - reset required")
        else:
            print(f"🔄 Now using: {self.get_current_account()['email']}")

        

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

    def create_email_message(self, to_email, subject, body, from_name="Sender", cc_emails=None, attachments=None, is_html=False):
        """Create MIME-safe email with plain or HTML body + optional attachments."""

        account = self.get_current_account()
        if not account:
            return None

        if attachments is None:
            attachments = []

        msg = MIMEMultipart('mixed')
        msg['From'] = f"{from_name} <{account['email']}>"
        msg['To'] = to_email

        sanitized_cc = []
        if cc_emails:
            sanitized_cc = [cc.strip() for cc in cc_emails if isinstance(cc, str) and cc.strip()]
            if sanitized_cc:
                msg['Cc'] = ", ".join(sanitized_cc)

        msg['Subject'] = subject
        msg['Date'] = formatdate(localtime=True)

        body = body or ""
        if is_html:
            alternative_part = MIMEMultipart('alternative')
            plain_fallback = self._html_to_plain_text(body)
            alternative_part.attach(MIMEText(plain_fallback or body, 'plain', 'utf-8'))
            alternative_part.attach(MIMEText(body, 'html', 'utf-8'))
            msg.attach(alternative_part)
        else:
            msg.attach(MIMEText(body, 'plain', 'utf-8'))

        # ✅ Attach LOGO from folder
        logo_path = os.path.join(os.getcwd(), "backend", "uploads", "logo", "company_logo.jpeg")
        if os.path.exists(logo_path):
            try:
                with open(logo_path, 'rb') as f:
                    img = MIMEImage(f.read())
                    img.add_header('Content-Disposition', 'attachment', filename="company_logo.jpeg")
                    msg.attach(img)
                    print("✅ Logo attached")
            except Exception as e:
                print("❌ Logo attach error:", e)
        else:
            print("⚠️ Logo not found at:", logo_path)

        # ✅ Attach files
        import mimetypes
        from email.mime.base import MIMEBase
        from email import encoders

        for att_path in attachments:
            try:
                if not os.path.exists(att_path):
                    print(f"❌ File not found: {att_path}")
                    continue

                with open(att_path, "rb") as f:
                    file_data = f.read()

                mime_type, _ = mimetypes.guess_type(att_path)
                if mime_type:
                    main_type, sub_type = mime_type.split("/")
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
                print(f"✅ Attached: {filename}")

            except Exception as e:
                print(f"❌ Attachment error: {e}")
        return msg
    
    def ensure_connection(self):
        """Validates OAuth credentials and initializes Gmail API service"""
        import time
        
        if self.current_account is None:
            return False, "No current account set"
            
        # Rotation cooldown
        if time.time() - self.last_rotation < 30:
            time.sleep(30 - (time.time() - self.last_rotation))
            
        try:
            # We assume provider='google'
            if self.current_account.get('provider', 'google') != 'google':
                return False, "Only Google accounts are supported in this version"
                
            client_id = os.getenv("OAUTH_CLIENT_ID", "")
            client_secret = os.getenv("OAUTH_CLIENT_SECRET", "")
            
            # If creds don't have client_id from env, try to load from credentials.json
            if not client_id and os.path.exists('credentials.json'):
                with open('credentials.json', 'r') as f:
                    client_config = json.load(f)
                    if 'web' in client_config:
                        client_id = client_config['web']['client_id']
                        client_secret = client_config['web']['client_secret']
                    elif 'installed' in client_config:
                        client_id = client_config['installed']['client_id']
                        client_secret = client_config['installed']['client_secret']

            access_token = self.current_account.get('access_token')
            refresh_token = self.current_account.get('refresh_token')
            token_uri = self.current_account.get('token_uri', "https://oauth2.googleapis.com/token")

            # Logging as requested
            print(f"Has access_token: {'Yes' if access_token else 'No'}")
            print(f"Has refresh_token: {'Yes' if refresh_token else 'No'}")
            print(f"Has client_id: {'Yes' if client_id else 'No'}")
            print(f"Has client_secret: {'Yes' if client_secret else 'No'}")
            print(f"Has token_uri: {'Yes' if token_uri else 'No'}")

            if not refresh_token:
                return False, "Reconnect your Google account. No refresh token is stored."

            creds = Credentials(
                token=access_token,
                refresh_token=refresh_token,
                token_uri=token_uri,
                client_id=client_id,
                client_secret=client_secret
            )

            # Auto-refresh if expired
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
                
            self.server = build('gmail', 'v1', credentials=creds, cache_discovery=False)
            print("✅ OAuth Gmail API connection ready")
            return True, None
            
        except Exception as e:
            print(f"❌ OAuth connect failed: {e}")
            self.server = None
            return False, str(e)
    
    def send_single_email(self, to_email, subject, body, from_name="Sender", cc_emails=None, attachments=None, is_html=False):
        """Send using Gmail API"""
        self.current_account = self.get_current_account()

        conn_success, conn_err = self.ensure_connection()
        if not conn_success:
            print(f"❌ Cannot establish API connection: {conn_err}")
            return False, f"Connection Error: {conn_err}"
        
        try:
            # Message prep
            msg = self.create_email_message(
                to_email,
                subject,
                body,
                from_name,
                cc_emails,
                attachments,
                is_html
            )
            if not msg:
                return False, "Failed to create message"
            
            # Encode MIME message for Gmail API
            print("\n========== MIME MESSAGE ==========")
            print(msg.as_string())
            print("==================================")
            
            raw_message = base64.urlsafe_b64encode(msg.as_bytes()).decode('utf-8')
            print(f"Base64 String Length: {len(raw_message)}")
            
            last_error = None
            # Triple retry with backoff
            for attempt in range(3):
                print("\n--------------------------------")
                print(f"Attempt: {attempt + 1}")
                print(f"Recipient: {to_email}")
                print(f"Current Exception: {last_error}")
                print("--------------------------------")
                
                try:
                    request_body = {'raw': raw_message}
                    print("Raw request body sent to Gmail:")
                    print(request_body)
                    
                    try:
                        self.server.users().messages().send(userId='me', body=request_body).execute()
                    except Exception as e:
                        import traceback
                        traceback.print_exc()
                        print(repr(e))
                        if hasattr(e, "resp"):
                            print("Status:", getattr(e.resp, "status", None))
                        if hasattr(e, "content"):
                            print("Content:", getattr(e, "content", None))
                        raise e
                        
                    print("✅ API send success")
                    return True, None
                except Exception as e:
                    last_error = e
                    print(f"🔄 API retry {attempt+1}/3: {e}")
                    import time
                    time.sleep(2 ** attempt)  # Exponential backoff
                    continue
            
            print("❌ All retries exhausted")
            print(f"Final Error: {last_error}")
            return False, f"All retries exhausted: {last_error}"
            
        except Exception as e:
            print(f"❌ API send error: {e}")
            self.server = None
            return False, str(e)
        finally:
            pass

    
    def send_bulk_emails(self, recipients, subject, body, from_name="Sender", cc_emails=None, attachments=None, is_html=False, delay_between_emails=1, separate_threads=False):
        """
        Send emails to multiple recipients with rotation
        
        Args:
            recipients: List of email addresses or list of dictionaries with 'email' key
            subject: Email subject
            body: Email body (plain text or HTML)
            from_name: Display name for sender
            attachments: List of attachment file paths
            is_html: Whether body is HTML
            delay_between_emails: Delay in seconds between emails
        """
        import time

        if attachments is None:
            attachments = []
        
        total_recipients = len(recipients)
        print(f"\n📧 Starting bulk email send...")
        print(f"📊 Total recipients: {total_recipients}")
        print(f"📊 Batch size: {self.batch_size} emails per account")
        print(f"📊 Number of accounts: {len(self.email_accounts)}")
        print(f"📧 From: {from_name}")
        print(f"📝 Subject: {subject}\n")
        print("📎 Attaching files:", attachments)

        for index, recipient in enumerate(recipients, 1):
            # Extract email address if recipient is a dict
            if isinstance(recipient, dict):
                to_email = recipient.get('email', '')
                # Use personalized body if available
                personalized_body = recipient.get('body', body)
                status = recipient.get('status', 'VALID')
            else:
                to_email = recipient
                personalized_body = body
                status = 'VALID'

            email_subject = subject
            if separate_threads:
                thread_token = datetime.utcnow().strftime('%Y%m%d%H%M%S') + f"-{index:04d}"
                email_subject = f"{subject} | Ref:{thread_token}"

            if status == 'INVALID':
                print(f"[{index}/{total_recipients}] ❌ Skipping {to_email} (Invalid Email)")
                current = self.get_current_account()
                self.failed.append({
                    'email': to_email,
                    'error': 'Mailbox rejected (550)',
                    'sender_email_id': current.get('_id') if current else None
                })
                continue
            
            # CRITICAL: Check rotation BEFORE every send (DB-driven)
            if self.needs_rotation():
                if not self.find_next_available_account():
                    print("🔄 All accounts exhausted. Resetting...")

                    # Reset counters in DB and local cache, but keep total_sent for this run.
                    if self.user_id:
                        try:
                            from models import EmailID
                            EmailID.reset_counts(self.user_id)
                        except Exception as db_error:
                            print(f"Failed to reset DB counters: {db_error}")

                    for acc in self.email_accounts:
                        acc['emails_sent'] = 0
                    self.current_account_index = 0
            
            # Send the email
            print(f"[{index}/{total_recipients}] Sending to {to_email}...", end=" ")
            
            success, error_msg = self.send_single_email(
                to_email,
                email_subject,
                personalized_body,
                from_name,
                cc_emails=cc_emails,
                attachments=attachments,
                is_html=is_html
            )
            
            if success:
                print(f"✅ Sent (Account: {self.get_current_account()['email']})")
                # 🔥 CRITICAL FIX
                self.increment_current_account()
                current = self.get_current_account()
                self.sent_entries.append({
                    'email': to_email,
                    'sender_email_id': current.get('_id') if current else None
                })
            else:
                print(f"❌ Failed: {error_msg}")
                current = self.get_current_account()
                self.failed.append({
                    'email': to_email,
                    'error': error_msg or 'Send failed',
                    'sender_email_id': current.get('_id') if current else None
                })
            
            # Add delay between emails (except for the last one)
            if index < total_recipients:
                time.sleep(delay_between_emails)
        
        # Print summary
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
                print(f"   {fail['email']}: {fail['error']}")
    
    def reset_counters(self):
        """Reset for new session (local state only - DB reset external)"""
        self.current_account_index = 0
        self.total_sent = 0
        self.failed = []
        print("🔄 EmailSender local counters reset")
    
    def set_initial_counts(self, counts_dict):
        """
        Set initial email counts from database values
        
        Args:
            counts_dict: Dictionary mapping email address to sent count
                         {'email1@example.com': 20, 'email2@example.com': 5}
        """
        for email_addr, count in counts_dict.items():
            for account in self.email_accounts:
                if account['email'].lower() == email_addr.lower():
                    account['db_sent_count'] = count
        print(f"📊 Initialized email counts: {counts_dict}")