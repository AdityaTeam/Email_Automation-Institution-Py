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


class EmailSender:
    """Email sender with automatic rotation between multiple accounts"""
    
    def __init__(self, email_accounts, batch_size=25):
        """
        Initialize the email sender with accounts and batch size
        
        Args:
            email_accounts: List of email account dictionaries
            batch_size: Number of emails per account before rotation (default: 25)
        """
        self.email_accounts = email_accounts
        self.batch_size = batch_size
        self.current_account_index = 0
        self.emails_sent_with_current_account = 0
        self.total_sent = 0
        self.failed = []
        
    def get_current_account(self):
        """Get the current email account to use"""
        if not self.email_accounts:
            return None
        return self.email_accounts[self.current_account_index]
    
    def switch_account(self):
        print("🔁 Switching account...")
        self.current_account_index = (self.current_account_index + 1) % len(self.email_accounts)
        self.emails_sent_with_current_account = 0
        print("Now using:", self.email_accounts[self.current_account_index]['email'])
        
    def create_email_message(self, to_email, subject, body, from_name, attachments=[], is_html=False):
        """Create an email message with optional attachments"""
        account = self.get_current_account()
        if not account:
            return None
        
        # Use 'mixed' for attachments
        msg = MIMEMultipart('mixed')
        msg['From'] = f"{from_name} <{account['email']}>"
        msg['To'] = to_email
        msg['Subject'] = subject
        msg['Date'] = datetime.now().strftime('%a, %d %b %Y %H:%M:%S %z')
        
        # Create alternative for text/html
        alt = MIMEMultipart('alternative')
        if is_html:
            alt.attach(MIMEText(body, 'html'))
        else:
            alt.attach(MIMEText(body, 'plain'))
        msg.attach(alt)
        
        # Add attachments
        import mimetypes
        from email.mime.base import MIMEBase
        from email import encoders
        
        # log incoming attachment paths for debugging
        if attachments:
            print("EmailSender will attempt to attach files:", attachments)
        for att_path in attachments:
            try:
                if not os.path.exists(att_path):
                    print(f"Attachment path does not exist: {att_path}")
                    continue
                with open(att_path, 'rb') as f:
                    part = MIMEBase('application', 'octet-stream')
                    part.set_payload(f.read())
                
                encoders.encode_base64(part)
                
                filename = os.path.basename(att_path)
                part.add_header(
                    'Content-Disposition',
                    f'attachment; filename= {filename}'
                )
                
                ctype, _ = mimetypes.guess_type(att_path)
                if ctype:
                    part.add_header('Content-Type', ctype, name=filename)
                else:
                    part.add_header('Content-Type', 'application/octet-stream', name=filename)
                
                msg.attach(part)
            except Exception as e:
                print(f"Failed to attach {att_path}: {e}")
        
        return msg
    
    def send_single_email(self, to_email, subject, body, from_name="Sender", attachments=[], is_html=False):
        """Send a single email with optional attachments"""
        account = self.get_current_account()
        if not account:
            return False
        
        try:
            msg = self.create_email_message(to_email, subject, body, from_name, attachments, is_html)
            if not msg:
                return False
            
            # Get connection settings
            smtp_server = account.get('smtp_server', 'smtp.gmail.com')
            smtp_port = account.get('smtp_port', 587)
            use_ssl = account.get('use_ssl', False)
            use_tls = account.get('use_tls', True)
            
            # Connect to SMTP server based on SSL/TLS settings
            if use_ssl:
                # Use SSL connection (implicit SSL on connection)
                server = smtplib.SMTP_SSL(smtp_server, smtp_port)
            else:
                # Regular connection (with optional TLS)
                server = smtplib.SMTP(smtp_server, smtp_port)
                if use_tls:
                    server.starttls()
            
            # Login and send
            server.login(account['email'], account['password'])
            server.sendmail(account['email'], to_email, msg.as_string())
            server.quit()
            
            self.emails_sent_with_current_account += 1
            self.total_sent += 1
            
            return True
            
        except Exception as e:
            print(f"❌ Failed to send to {to_email}: {str(e)}")
            self.failed.append({"email": to_email, "error": str(e)})
            return False
    
    def send_bulk_emails(self, recipients, subject, body, from_name="Sender", attachments=[], is_html=False, delay_between_emails=1):
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
            
            # Check if we need to switch account
            if self.emails_sent_with_current_account >= self.batch_size:
                self.switch_account()
            
            # Send the email
            print(f"[{index}/{total_recipients}] Sending to {to_email}...", end=" ")
            
            success = self.send_single_email(to_email, subject, personalized_body, from_name, attachments, is_html)
            
            if success:
                print(f"✅ Sent (Account: {self.get_current_account()['email']})")
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
        """Reset the counters for a new sending session"""
        self.current_account_index = 0
        self.emails_sent_with_current_account = 0
        self.total_sent = 0
        self.failed = []
    
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