"""
Isolated Email Sender Service (NEW ISOLATED PIPELINE)
Handles email sending for Newsletter and Banner with Content features.
This module is strictly isolated from email_sender.py to ensure zero disruption to existing email system.
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
from email.header import Header


def safe_print_str(val):
    """Sanitize string for Windows console output to prevent UnicodeEncodeError"""
    if val is None:
        return ""
    return str(val).encode('ascii', 'replace').decode('ascii')


class IsolatedEmailSender:
    """Isolated Email sender for Newsletter and Banner features with SMTP pooling & rotation"""
    
    def __init__(self, email_accounts, batch_size=25, user_id=None, feature_tag='newsletter'):
        self.email_accounts = email_accounts or []
        self.batch_size = batch_size
        self.user_id = user_id
        self.feature_tag = feature_tag  # 'newsletter' or 'banner'
        self.current_account_index = 0
        self.total_sent = 0
        self.failed = []
        self.sent_entries = []
        self.server = None
        self.connected_account_email = None
        self.current_account = None
        
    def get_current_account(self):
        if not self.email_accounts:
            self.current_account = None
            return None
        if self.current_account_index >= len(self.email_accounts):
            self.current_account_index = 0
        account = self.email_accounts[self.current_account_index]
        self.current_account = account
        return account
    
    def get_account_sent_count(self):
        current = self.get_current_account()
        if current and isinstance(current, dict):
            return current.get('emails_sent', 0)
        return float('inf')
    
    def increment_current_account(self):
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
                    print(f"[{self.feature_tag.upper()}] Failed to persist sent count for {safe_print_str(current.get('email'))}: {safe_print_str(db_error)}")
            print(f"[COUNTER] [{self.feature_tag.upper()}] {safe_print_str(current.get('email'))}: {current.get('emails_sent', 0)}/{self.batch_size}")
    
    def needs_rotation(self):
        current = self.get_current_account()
        if not current:
            return True
        count = self.get_account_sent_count()
        if count >= self.batch_size:
            print(f"[LIMIT REACHED] [{self.feature_tag.upper()}] for {safe_print_str(current.get('email'))}")
            return True
        return False
    
    def find_next_available_account(self):
        if not self.email_accounts:
            return False
        total_accounts = len(self.email_accounts)
        start_index = self.current_account_index
        
        for i in range(total_accounts):
            self.current_account_index = (start_index + i) % total_accounts
            if not self.needs_rotation():
                current = self.get_current_account()
                if current:
                    print(f"[SELECTED] [{self.feature_tag.upper()}] Selected: {safe_print_str(current.get('email'))} ({self.get_account_sent_count()}/{self.batch_size})")
                    return True
        return False
    
    def _html_to_plain_text(self, html_content):
        if not html_content:
            return ""
        text = re.sub(r'(?is)<(script|style).*?>.*?</\1>', '', html_content)
        text = re.sub(r'(?i)<br\s*/?>', '\n', text)
        text = re.sub(r'(?i)</(p|div|li|h1|h2|h3|h4|h5|h6)>', '\n', text)
        text = re.sub(r'(?is)<[^>]+>', '', text)
        text = text.replace('&nbsp;', ' ').replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text.strip()

    def create_email_message(self, to_email, subject, body, from_name="Sender", cc_emails=None, bcc_emails=None, attachments=None, is_html=False, inline_images=None):
        """
        Create MIME message with full UTF-8 support for Subject, From name, and Body.
        """
        account = self.get_current_account()
        if not account:
            return None

        if attachments is None:
            attachments = []

        sender_email = str(account.get('email') or '').strip()

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

        body_str = body if isinstance(body, str) else str(body or '')
        subject_str = subject if isinstance(subject, str) else str(subject or '')

        if is_html:
            alt_part = MIMEMultipart('alternative')
            plain_fallback = self._html_to_plain_text(body_str)
            alt_part.attach(MIMEText(plain_fallback or body_str, 'plain', 'utf-8'))
            alt_part.attach(MIMEText(body_str, 'html', 'utf-8'))

            # Attach inline images if present (e.g., banner rendering or logo)
            if inline_images and isinstance(inline_images, dict):
                related_part = MIMEMultipart('related')
                related_part.attach(alt_part)
                
                for cid, img_info in inline_images.items():
                    try:
                        if isinstance(img_info, dict):
                            img_bytes = img_info.get('data')
                            img_filename = img_info.get('filename', f'{cid}.png')
                            img_mime = img_info.get('mimetype', 'image/png')
                        elif isinstance(img_info, str) and os.path.exists(img_info):
                            with open(img_info, 'rb') as f:
                                img_bytes = f.read()
                            img_filename = os.path.basename(img_info)
                            img_mime = mimetypes.guess_type(img_info)[0] or 'image/png'
                        else:
                            continue

                        if img_bytes:
                            sub_type = img_mime.split('/')[-1] if '/' in img_mime else 'png'
                            part = MIMEImage(img_bytes, _subtype=sub_type)
                            part.add_header('Content-ID', f'<{cid}>')
                            part.add_header('Content-Disposition', 'inline', filename=img_filename)
                            related_part.attach(part)
                    except Exception as e:
                        print(f"[WARNING] [{self.feature_tag.upper()}] Error attaching inline image {safe_print_str(cid)}: {safe_print_str(e)}")

                body_part = related_part
            else:
                body_part = alt_part
        else:
            body_part = MIMEText(body_str, 'plain', 'utf-8')

        if attachments:
            msg = MIMEMultipart('mixed')
            msg.attach(body_part)

            for att_path in attachments:
                try:
                    if not att_path or not os.path.exists(att_path):
                        continue

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
                    part.add_header("Content-Disposition", f'attachment; filename="{filename}"')
                    msg.attach(part)
                except Exception as e:
                    print(f"[ERROR] [{self.feature_tag.upper()}] Attachment error: {safe_print_str(e)}")
        else:
            msg = body_part

        # Use Header for UTF-8 encoded Subject and From display name
        if from_name:
            try:
                from_display = Header(from_name, 'utf-8').encode()
                msg['From'] = f"{from_display} <{sender_email}>"
            except Exception:
                msg['From'] = f"{from_name} <{sender_email}>"
        else:
            msg['From'] = sender_email

        msg['To'] = str(to_email or '').strip()
        if sanitized_cc:
            msg['Cc'] = ", ".join(sanitized_cc)
        if sanitized_bcc:
            msg['Bcc'] = ", ".join(sanitized_bcc)

        try:
            msg['Subject'] = Header(subject_str, 'utf-8')
        except Exception:
            msg['Subject'] = subject_str

        msg['Date'] = formatdate(localtime=True)
        msg['Message-ID'] = make_msgid()

        return msg

    def ensure_connection(self):
        account = self.get_current_account()
        if not account:
            return False, "No sender accounts available"

        email_addr = account.get('email')
        if not email_addr:
            return False, "Current account missing email"

        if self.server and self.connected_account_email == email_addr:
            try:
                status = self.server.noop()[0]
                if status == 250:
                    return True, None
            except Exception:
                self.server = None
                self.connected_account_email = None

        if self.server:
            try:
                self.server.quit()
            except Exception:
                pass
            self.server = None

        smtp_server = account.get('smtp_server', 'smtp.gmail.com')
        smtp_port = int(account.get('smtp_port', 587))
        use_tls = account.get('use_tls', True)
        password = account.get('password', '')

        print(f"[SMTP CONNECT] [{self.feature_tag.upper()}] Connecting to SMTP: {smtp_server}:{smtp_port} for {safe_print_str(email_addr)}")
        try:
            if smtp_port == 465:
                context = ssl.create_default_context()
                self.server = smtplib.SMTP_SSL(smtp_server, smtp_port, context=context, timeout=8)
            else:
                self.server = smtplib.SMTP(smtp_server, smtp_port, timeout=8)
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
            print(f"[SUCCESS] [{self.feature_tag.upper()}] SMTP connected & authenticated for {safe_print_str(email_addr)}")
            return True, None

        except Exception as e:
            self.server = None
            self.connected_account_email = None
            err_msg = f"SMTP Connection error for {email_addr}: {str(e)}"
            print(f"[ERROR] [{self.feature_tag.upper()}] {safe_print_str(err_msg)}")
            return False, err_msg

    def send_single_email(self, to_email, subject, body, from_name="Sender", cc_emails=None, bcc_emails=None, attachments=None, is_html=False, inline_images=None):
        if not to_email or not str(to_email).strip():
            return False, "Recipient email address is missing"
        
        to_email_str = str(to_email).strip()
        body_str = body if isinstance(body, str) else str(body or '')
        subject_str = subject if isinstance(subject, str) else str(subject or '')

        conn_ok, conn_error = self.ensure_connection()
        if not conn_ok:
            return False, conn_error
        
        try:
            msg = self.create_email_message(
                to_email=to_email_str,
                subject=subject_str,
                body=body_str,
                from_name=from_name,
                cc_emails=cc_emails,
                bcc_emails=bcc_emails,
                attachments=attachments,
                is_html=is_html,
                inline_images=inline_images
            )
            if not msg:
                return False, "Failed to construct MIME email message"

            recipients = [to_email_str]
            if cc_emails:
                if isinstance(cc_emails, str):
                    recipients.extend([c.strip() for c in cc_emails.split(',') if c.strip()])
                elif isinstance(cc_emails, list):
                    recipients.extend([str(c).strip() for c in cc_emails if str(c).strip()])

            account = self.get_current_account() or {}
            sender_email = str(account.get('email') or self.connected_account_email or '').strip()
            msg_raw = msg.as_string()

            for attempt in range(2):
                try:
                    self.server.sendmail(sender_email, recipients, msg_raw)
                    print(f"[DELIVERED] [{self.feature_tag.upper()}] Delivered to {safe_print_str(to_email)} via {safe_print_str(sender_email)}")
                    return True, None
                except (smtplib.SMTPServerDisconnected, smtplib.SMTPSenderRefused) as net_err:
                    self.server = None
                    self.connected_account_email = None
                    conn_ok, _ = self.ensure_connection()
                    if not conn_ok:
                        break
                except Exception as ex:
                    return False, f"Error sending message: {str(ex)}"
            
            return False, "Delivery failed after retry"
            
        except Exception as e:
            traceback.print_exc()
            return False, f"Unexpected send error: {str(e)}"

    def send_bulk_emails(self, recipients, subject, body, from_name="Sender", cc_emails=None, bcc_emails=None, attachments=None, is_html=False, delay_between_emails=1, separate_threads=False, inline_images=None):
        if attachments is None:
            attachments = []
        
        total_recipients = len(recipients)
        print(f"\n[BULK START] [{self.feature_tag.upper()}] Starting bulk email send...")

        for index, recipient in enumerate(recipients, 1):
            if isinstance(recipient, dict):
                to_email = recipient.get('email', '')
                personalized_body = recipient.get('body', body)
                base_subject = recipient.get('subject', subject)
            else:
                to_email = recipient
                personalized_body = body
                base_subject = subject

            email_subject = base_subject
            if separate_threads:
                thread_token = datetime.utcnow().strftime('%Y%m%d%H%M%S') + f"-{index:04d}"
                email_subject = f"{base_subject} | Ref:{thread_token}"
            
            if self.needs_rotation():
                if not self.find_next_available_account():
                    if self.user_id:
                        try:
                            from models import EmailID
                            EmailID.reset_counts(self.user_id)
                        except Exception:
                            pass
                    for acc in self.email_accounts:
                        acc['emails_sent'] = 0
                    self.current_account_index = 0
            
            success, error_msg = self.send_single_email(
                to_email=to_email,
                subject=email_subject,
                body=personalized_body,
                from_name=from_name,
                cc_emails=cc_emails,
                bcc_emails=bcc_emails,
                attachments=attachments,
                is_html=is_html,
                inline_images=inline_images
            )
            
            current = self.get_current_account()
            if success:
                self.increment_current_account()
                self.sent_entries.append({
                    'email': to_email,
                    'sender_email_id': current.get('_id') if current else None
                })
            else:
                self.failed.append({
                    'email': to_email,
                    'error': error_msg or 'Send failed',
                    'sender_email_id': current.get('_id') if current else None
                })
            
            if index < total_recipients and delay_between_emails > 0:
                time.sleep(delay_between_emails)
        
        if self.server:
            try:
                self.server.quit()
            except Exception:
                pass
            self.server = None

        return {
            "total_sent": self.total_sent,
            "sent_entries": self.sent_entries,
            "failed": self.failed,
            "total_recipients": total_recipients
        }
