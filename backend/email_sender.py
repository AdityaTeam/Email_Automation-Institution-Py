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


class EmailSender:
    """Email sender with SMTP pooling & Gmail-safe rotation"""
    
    def __init__(self, email_accounts, batch_size=15):
        """
        Initialize with Gmail-safe batch_size=15
        """
        self.email_accounts = email_accounts
        self.batch_size = batch_size
        self.current_account_index = 0
        self.total_sent = 0
        self.failed = []
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
            print(f"📊 {current['email']}: {current['emails_sent']}/25")
    
    def needs_rotation(self):
        "\"\"Check if current account needs rotation (DB-driven)\"\"\""
        count = self.get_account_sent_count()
        print(f"🔍 {self.get_current_account()['email']}: {count}/25")
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
                print(f"✅ Selected: {current['email']} ({self.get_account_sent_count()}/25)")
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

        

    def create_email_message(self, to_email, subject, body, from_name="Sender", cc_emails=None, attachments=[]):
        """Create email message (PLAIN TEXT + LOGO ATTACHMENT)"""

        account = self.get_current_account()
        if not account:
            return None

        msg = MIMEMultipart()
        msg['From'] = f"{from_name} <{account['email']}>"
        msg['To'] = to_email

        if cc_emails:
            msg['Cc'] = ", ".join(cc_emails)

        msg['Subject'] = subject
        msg['Date'] = datetime.now().strftime('%a, %d %b %Y %H:%M:%S %z')

        # ✅ Plain text only
        msg.attach(MIMEText(body, 'plain'))

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
        """Pool-aware connection validation/reconnect"""
        import time
        import socket
        
        if self.current_account is None:
            return False
            
        # Rotation cooldown
        if time.time() - self.last_rotation < 30:
            time.sleep(30 - (time.time() - self.last_rotation))
        
        # Validate existing connection
        if self.server and self.current_account:
            try:
                self.server.noop()
                self.server.rset()
                return True
            except:
                print("🔌 Stale connection → Reconnecting...")
                self.server = None
        
        # Create/connect new pooled connection
        try:
            # socket.setdefaulttimeout(120.0)
            smtp_server = self.current_account.get('smtp_server', 'smtp.gmail.com')
            smtp_port = self.current_account.get('smtp_port', 587)
            use_ssl = self.current_account.get('use_ssl', False)
            use_tls = self.current_account.get('use_tls', True)
            
            print(f"🔌 Pool connect: {smtp_server}:{smtp_port}")
            
            if use_ssl:
                self.server = smtplib.SMTP_SSL(smtp_server, smtp_port, timeout=120)
            else:
                self.server = smtplib.SMTP(smtp_server, smtp_port, timeout=120)
                self.server.ehlo()
                if use_tls:
                    self.server.starttls()
                    self.server.ehlo()
            
            self.server.login(self.current_account['email'], self.current_account['password'])
            print("✅ Pooled connection ready")
            return True
            
        except Exception as e:
            print(f"❌ Pool connect failed: {e}")
            self.server = None
            return False
    
    def send_single_email(self, to_email, subject, body, from_name="Sender", cc_emails=None, attachments=[]):
        """Send using pooled connection + validation"""
        import socket
        
        # ✅ FIX: set current account
        self.current_account = self.get_current_account()

        if not self.ensure_connection():
            print("❌ Cannot establish pooled connection")
            return False
        
        try:
            # Message prep
            msg = self.create_email_message(to_email, subject, body, from_name, cc_emails, attachments)
            if not msg:
                return False
            
            recipients = [to_email]
            if cc_emails:
                recipients.extend(cc_emails)
            
            print(f"📤 Pool send → {to_email}")
            
            # Final validation + reset
            self.server.noop()
            self.server.rset()
            
            # Triple retry with backoff
            for attempt in range(3):
                try:
                    self.server.sendmail(
                        self.current_account['email'], 
                        recipients, 
                        msg.as_string()
                    )
                    print("✅ Pooled send success")
                    self.server.quit()
                    self.server = None  # Force new connection for next send
                    return True
                except smtplib.SMTPServerDisconnected as e:
                    print(f"🔄 Pool retry {attempt+1}/3: {e}")
                    time.sleep(2 ** attempt)  # Exponential backoff
                    self.ensure_connection()
                    continue
            
            print("❌ All retries exhausted")
            return False
            
        except Exception as e:
            print(f"❌ Pool send error: {e}")
            self.server = None
            return False
        finally:
            socket.setdefaulttimeout(None)

    
    def send_bulk_emails(self, recipients, subject, body, from_name="Sender", cc_emails=None, attachments=[], is_html=False, delay_between_emails=1):
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
            else:
                to_email = recipient
                personalized_body = body
            
            # CRITICAL: Check rotation BEFORE every send (DB-driven)
            if self.needs_rotation():
                if not self.find_next_available_account():
                    print("🔄 All accounts exhausted. Resetting...")

                    # 🔥 Reset all counts (DB should also reset outside)
                    for acc in self.email_accounts:
                        acc['emails_sent'] = 0

                    self.reset_counters()   

                    # Try again after reset
                    self.current_account_index = 0
            
            # Send the email
            print(f"[{index}/{total_recipients}] Sending to {to_email}...", end=" ")
            
            success = self.send_single_email(
                to_email,
                subject,
                personalized_body,
                from_name,
                cc_emails=cc_emails,      # ✅ correct
                attachments=attachments   # ✅ correct
            )
            
            if success:
                print(f"✅ Sent (Account: {self.get_current_account()['email']})")
                # 🔥 CRITICAL FIX
                self.increment_current_account()
            else:
                print(f"❌ Failed")
            
            # Add delay between emails (except for the last one)
            if index < total_recipients:
                time.sleep(delay_between_emails)
        
        # Print summary
        self.print_summary()
        
        return {
            "total_sent": self.total_sent,
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