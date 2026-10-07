"""
User Panel Routes
Handles user dashboard, email management, CSV/Excel parsing, verification, and email sending
"""

from flask import Blueprint, render_template, request, jsonify, session, redirect, url_for
from models import EmailID, ExcelFile, Template, Requirement, Department, EmailLog
from database import MongoDB, Collections
from bson import ObjectId
import os
import pandas as pd
import re
import dns.resolver
import smtplib
import socket
from werkzeug.utils import secure_filename
from email_sender import EmailSender

user_bp = Blueprint('user', __name__)

# SMTP Configuration Auto-Detection
SMTP_CONFIG = {
    'gmail.com': {'smtp_server': 'smtp.gmail.com', 'smtp_port': 587},
    'googlemail.com': {'smtp_server': 'smtp.gmail.com', 'smtp_port': 587},
    'yahoo.com': {'smtp_server': 'smtp.mail.yahoo.com', 'smtp_port': 587},
    'yahoo.co.uk': {'smtp_server': 'smtp.mail.yahoo.com', 'smtp_port': 587},
    'outlook.com': {'smtp_server': 'smtp.office365.com', 'smtp_port': 587},
    'hotmail.com': {'smtp_server': 'smtp.office365.com', 'smtp_port': 587},
    'live.com': {'smtp_server': 'smtp.office365.com', 'smtp_port': 587},
    'office365.com': {'smtp_server': 'smtp.office365.com', 'smtp_port': 587},
    'zoho.com': {'smtp_server': 'smtp.zoho.com', 'smtp_port': 587},
    'protonmail.com': {'smtp_server': 'smtp.protonmail.com', 'smtp_port': 587},
    'proton.me': {'smtp_server': 'smtp.protonmail.com', 'smtp_port': 587},
    'gmx.com': {'smtp_server': 'smtp.gmx.com', 'smtp_port': 587},
    'icloud.com': {'smtp_server': 'smtp.mail.me.com', 'smtp_port': 587},
    'me.com': {'smtp_server': 'smtp.mail.me.com', 'smtp_port': 587},
    'mac.com': {'smtp_server': 'smtp.mail.me.com', 'smtp_port': 587},
    'fastmail.com': {'smtp_server': 'smtp.fastmail.com', 'smtp_port': 587},
    'mail.com': {'smtp_server': 'smtp.mail.com', 'smtp_port': 587},
    'aol.com': {'smtp_server': 'smtp.aol.com', 'smtp_port': 587},
    'yandex.com': {'smtp_server': 'smtp.yandex.com', 'smtp_port': 587},
    'yandex.ru': {'smtp_server': 'smtp.yandex.com', 'smtp_port': 587},
}

DEFAULT_SMTP = {'smtp_server': 'smtp.gmail.com', 'smtp_port': 587}

EMAIL_REGEX = r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$"

# Block non-deliverable documentation and throwaway email domains
DISALLOWED_DOMAINS = {
    'example.com', 'example.org', 'example.net', 
    'test.com', 'test.org', 'test.net',
    'invalid.com', 'localhost', 'local.domain',
    'mailinator.com', 'tempmail.com', 'dispostable.com', 
    '10minutemail.com', 'guerrillamail.com', 'trashmail.com'
}


def verify_email_smtp(email):
    """
    Verify email existence using strict regex syntax checks, reserved domain filtering,
    DNS MX lookups, and SMTP socket verification.
    """
    if not isinstance(email, str):
        return False, "Invalid email type"

    email = email.strip()

    # 1. Regex Syntax Check BEFORE domain splitting
    if not re.match(EMAIL_REGEX, email):
        return False, "Invalid syntax"

    # Safely split domain
    parts = email.split('@')
    if len(parts) != 2:
        return False, "Invalid email format"

    domain = parts[1].strip().lower().rstrip('.')

    # Domain sanity check to prevent IDNA codec crash
    if not domain or '..' in domain or domain.startswith('-') or domain.endswith('-'):
        return False, "Invalid domain format"

    # 2. Block reserved test / disposable domains immediately
    if domain in DISALLOWED_DOMAINS:
        return False, f"Reserved/fake test domain ({domain})"

    # 3. DNS MX Record Lookup
    try:
        records = dns.resolver.resolve(domain, 'MX')
        if not records or len(records) == 0:
            return False, "No MX records found"
        mx_host = str(records[0].exchange).rstrip('.')
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers):
        return False, "Domain or MX record does not exist"
    except Exception as e:
        return False, f"Domain lookup failed ({type(e).__name__})"

    # 4. SMTP Socket Connection & RCPT TO Verification
    try:
        server = smtplib.SMTP(timeout=4)
        server.connect(mx_host, 25)
        server.helo("verify.local")
        server.mail("verify@verify.local")

        code, _ = server.rcpt(email)
        server.quit()

        if code == 250:
            return True, "Valid/Deliverable"
        elif code == 550:
            return False, "Mailbox does not exist"
        else:
            return False, f"Rejected with server code {code}"

    except (socket.error, smtplib.SMTPException):
        # Port 25 is commonly blocked on residential or local networks.
        # Since MX records exist and domain is not in the blocklist, treat domain as valid.
        return True, "Valid domain (Port 25 check bypassed)"


def detect_smtp_settings(email):
    """Automatically detect SMTP settings based on email domain"""
    if not email or '@' not in email:
        return DEFAULT_SMTP.copy()
    domain = email.split('@')[-1].strip().lower()
    if domain in SMTP_CONFIG:
        return SMTP_CONFIG[domain].copy()
    return DEFAULT_SMTP.copy()


def require_login(f):
    """Decorator to require user login"""
    from functools import wraps
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('auth.login'))
        return f(*args, **kwargs)
    return decorated_function


@user_bp.route('/dashboard')
@require_login
def dashboard():
    """User dashboard"""
    if session.get('role') == 'admin':
        return redirect(url_for('admin.dashboard'))
    
    user_id = session['user_id']
    email_ids = EmailID.get_by_user(user_id)
    excel_files = ExcelFile.get_by_user(user_id)
    stats = EmailLog.get_stats(user_id)
    
    return render_template('user/dashboard.html', 
                           username=session['username'],
                           email_ids=email_ids,
                           excel_files=excel_files,
                           stats=stats)


@user_bp.route('/email-ids')
@require_login
def email_ids():
    """Email IDs management page"""
    email_ids = EmailID.get_by_user(session['user_id'])
    return render_template('user/email_ids.html',
                           username=session['username'],
                           email_ids=email_ids)


@user_bp.route('/api/email-ids', methods=['GET'])
@require_login
def get_email_ids():
    """Get user's email IDs (without passwords for API)"""
    email_ids = EmailID.get_by_user(session['user_id'])
    for eid in email_ids:
        eid['_id'] = str(eid['_id'])
        eid['user_id'] = str(eid['user_id'])
        if 'password' in eid:
            del eid['password']
    return jsonify({'email_ids': email_ids})


@user_bp.route('/api/email-ids', methods=['POST'])
@require_login
def add_email_id():
    """Add new email ID with auto SMTP detection"""
    data = request.json
    email = data.get('email', '').strip()
    password = data.get('password', '').strip()
    
    if not email or not password:
        return jsonify({'error': 'Email and password are required'}), 400
    
    if '@' not in email:
        return jsonify({'error': 'Invalid email address'}), 400
    
    smtp = detect_smtp_settings(email)
    
    email_data = {
        'email': email,
        'password': password,
        'smtp_server': smtp['smtp_server'],
        'smtp_port': smtp['smtp_port'],
        'use_tls': True,
        'use_ssl': False
    }
    
    result = EmailID.create(session['user_id'], email_data)
    if result:
        return jsonify({'success': True, 'message': f'Added! SMTP: {smtp["smtp_server"]}:{smtp["smtp_port"]}'})
    return jsonify({'error': 'Failed to add'}), 400


@user_bp.route('/api/email-ids/<email_id>', methods=['DELETE'])
@require_login
def delete_email_id(email_id):
    """Delete email ID"""
    if EmailID.delete(email_id):
        return jsonify({'success': True})
    return jsonify({'error': 'Failed to delete'}), 400


@user_bp.route('/uploads')
@require_login
def uploads():
    """Upload management page"""
    excel_files = ExcelFile.get_by_user(session['user_id'])
    return render_template('user/uploads.html',
                           username=session['username'],
                           excel_files=excel_files)


@user_bp.route('/api/upload', methods=['POST'])
@require_login
def upload_file():
    """Upload, process Excel/CSV, and separate valid vs. non-existent emails"""
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400
    
    filename = secure_filename(file.filename)
    filepath = os.path.join('uploads', filename)
    os.makedirs('uploads', exist_ok=True)
    file.save(filepath)
    
    try:
        if filename.endswith('.csv'):
            df = pd.read_csv(filepath)
        elif filename.endswith(('.xlsx', '.xls')):
            df = pd.read_excel(filepath)
        else:
            os.remove(filepath)
            return jsonify({'error': 'Invalid format. Use CSV or Excel'}), 400
        
        df.columns = [str(col).strip() for col in df.columns]
        
        email_col = None
        for col in df.columns:
            if col.lower() == 'email':
                email_col = col
                break
        
        if not email_col:
            os.remove(filepath)
            return jsonify({'error': 'Missing Email column'}), 400
        
        name_col = None
        institute_col = None
        for col in df.columns:
            if col.lower() == 'name':
                name_col = col
            if col.lower() == 'institute':
                institute_col = col
        
        valid_recipients = []
        invalid_recipients = []

        for _, row in df.iterrows():
            raw_email = row[email_col]

            # Safely handle NaN / Float values from Pandas
            if pd.isna(raw_email):
                continue

            email = str(raw_email).strip()

            if email and '@' in email:
                recipient = {'email': email}
                if name_col and pd.notna(row[name_col]):
                    recipient['name'] = str(row[name_col]).strip()
                if institute_col and pd.notna(row[institute_col]):
                    recipient['institute'] = str(row[institute_col]).strip()
                
                # Verify email existence safely
                is_valid, reason = verify_email_smtp(email)
                recipient['status_reason'] = reason

                if is_valid:
                    valid_recipients.append(recipient)
                else:
                    invalid_recipients.append(recipient)
        
        # Save ONLY valid recipients into database record
        excel_file = ExcelFile.create(session['user_id'], filename, file.filename, valid_recipients)
        os.remove(filepath)
        
        return jsonify({
            'success': True,
            'file_id': str(excel_file['_id']),
            'valid_count': len(valid_recipients),
            'invalid_count': len(invalid_recipients),
            'valid_recipients': valid_recipients,
            'invalid_recipients': invalid_recipients
        })
    except Exception as e:
        if os.path.exists(filepath):
            os.remove(filepath)
        return jsonify({'error': str(e)}), 500


@user_bp.route('/api/excel-files/<file_id>', methods=['GET'])
@require_login
def get_excel_file(file_id):
    """Get single excel file with recipients"""
    file = ExcelFile.get_by_id(file_id)
    
    if not file:
        return jsonify({'error': 'File not found'}), 404
    
    # Convert ObjectIds to string
    file['_id'] = str(file['_id'])
    file['user_id'] = str(file['user_id'])
    
    return jsonify({'file': file})


@user_bp.route('/api/excel-files/<file_id>', methods=['DELETE'])
@require_login
def delete_excel_file(file_id):
    """Delete excel file"""
    if ExcelFile.delete(file_id):
        return jsonify({'success': True})
    return jsonify({'error': 'Failed to delete'}), 400


@user_bp.route('/compose')
@require_login
def compose():
    """Email composition page"""
    user_id = session['user_id']
    email_ids = EmailID.get_by_user(user_id)
    excel_files = ExcelFile.get_by_user(user_id)
    requirements = Requirement.get_all()
    departments = Department.get_all()
    for requirement in requirements:
        requirement['_id'] = str(requirement['_id'])
        requirement['department_id'] = str(requirement['department_id']) if requirement.get('department_id') else ''
    for department in departments:
        department['_id'] = str(department['_id'])
    
    return render_template('user/compose.html',
                           username=session['username'],
                           email_ids=email_ids,
                           excel_files=excel_files,
                           requirements=requirements,
                           departments=departments)


@user_bp.route('/api/templates', methods=['GET'])
@require_login
def get_templates():
    """Get templates"""
    requirement_id = request.args.get('requirement_id')
    if requirement_id:
        templates = Template.get_by_requirement(requirement_id)
    else:
        templates = Template.get_all()
    for t in templates:
        t['_id'] = str(t['_id'])
        t['requirement_id'] = str(t['requirement_id'])
    return jsonify({'templates': templates})


@user_bp.route('/api/requirements', methods=['GET'])
@require_login
def get_requirements():
    """Get requirements"""
    requirements = Requirement.get_all(request.args.get('department_id'))
    for r in requirements:
        r['_id'] = str(r['_id'])
        r['department_id'] = str(r['department_id']) if r.get('department_id') else ''
    return jsonify({'requirements': requirements})


@user_bp.route('/api/cc-emails', methods=['GET'])
@require_login
def get_cc_emails():
    from models import CcEmail
    
    cc_emails = CcEmail.get_all()
    
    safe_cc = []
    for cc in cc_emails:
        safe_cc.append({
            '_id': str(cc['_id']),
            'email': cc.get('email'),
            'created_at': str(cc.get('created_at')) if cc.get('created_at') else None
        })
    
    return jsonify({'cc_emails': safe_cc})


@user_bp.route('/api/send', methods=['POST'])
@require_login
def send_emails():
    """Send bulk emails with automatic sender rotation"""

    data = request.json
    recipients = data.get('recipients', [])
    cc_emails = data.get('cc_emails', [])
    sender_email_id = data.get('sender_email_id', '')
    from_name = data.get('from_name', session['username'])
    subject = data.get('subject', '')
    body = data.get('body', '')
    template_id = data.get('template_id')
    attachments = []
    is_html = data.get('is_html', False)
    separate_threads = data.get('separate_threads', True)
    signature_data = data.get('signature_data', {})

    if not isinstance(cc_emails, list):
        cc_emails = []
    cc_emails = [cc.strip() for cc in cc_emails if isinstance(cc, str) and cc.strip()]

    # Fetch template attachments if template_id provided
    if template_id:
        template = Template.get_by_id(template_id)
        if template and 'attachments' in template:
            BASE_DIR = os.path.abspath(os.getcwd())

            for rel_path in template['attachments']:
                abs_path = os.path.join(BASE_DIR, rel_path)

                if os.path.exists(abs_path):
                    attachments.append(abs_path)
    
    if not recipients or not sender_email_id or not subject or not body:
        return jsonify({'error': 'All fields required'}), 400
    
    signature = Template.build_signature(signature_data)
    user_email_ids = EmailID.get_by_user_with_passwords(session['user_id'])
    
    email_accounts = []
    for eid in user_email_ids:
        email_accounts.append({
            'email': eid['email'],
            'password': eid['password'],
            'smtp_server': eid['smtp_server'],
            'smtp_port': eid['smtp_port'],
            'use_tls': eid.get('use_tls', True),
            'use_ssl': eid.get('use_ssl', False),
            '_id': str(eid['_id']),
            'emails_sent': eid.get('emails_sent', 0)
        })
    
    if not email_accounts:
        return jsonify({'error': 'No sender email IDs configured'}), 400
    
    start_index = 0
    for i, acc in enumerate(email_accounts):
        if acc['_id'] == sender_email_id:
            start_index = i
            break
    
    if is_html:
        body = Template.process_body(body)
        signature = Template.process_body(signature)
    
    personalized_recipients = []
    for r in recipients:
        personalized_body = body
        name = r.get('name', '')
        if name:
            personalized_body = personalized_body.replace('{{name}}', name)
        institute = r.get('institute', '')
        if institute:
            personalized_body = personalized_body.replace('{{institute}}', institute)
        personalized_body = personalized_body + '\n\n' + signature
        personalized_recipients.append({'email': r['email'], 'body': personalized_body})
    
    try:
        BATCH_SIZE = 25
        sender = EmailSender(email_accounts, batch_size=BATCH_SIZE, user_id=session['user_id'])
        sender.current_account_index = start_index
        
        result = sender.send_bulk_emails(
            personalized_recipients,
            subject,
            "",
            from_name,
            cc_emails=cc_emails,
            attachments=attachments,
            is_html=is_html,
            delay_between_emails=5,
            separate_threads=separate_threads
        )
        
        sent_entries = result.get("sent_entries", [])
        failed_list = result.get("failed", [])
        sent_count = len(sent_entries)
        
        for sent_entry in sent_entries:
            log_sender_id = sent_entry.get('sender_email_id') or sender_email_id
            EmailLog.create(session['user_id'], log_sender_id, sent_entry['email'], subject, 'sent')

        for fail_entry in failed_list:
            log_sender_id = fail_entry.get('sender_email_id') or sender_email_id
            EmailLog.create(
                session['user_id'],
                log_sender_id,
                fail_entry.get('email', ''),
                subject,
                'failed',
                fail_entry.get('error', 'Send failed')
            )

        stats = EmailLog.get_stats(session['user_id'])
        refreshed_email_ids = EmailID.get_by_user(session['user_id'])
        email_usage = []
        for eid in refreshed_email_ids:
            email_usage.append({
                '_id': str(eid['_id']),
                'email': eid.get('email', ''),
                'emails_sent': eid.get('emails_sent', 0)
            })

        recent_logs = EmailLog.get_by_user(session['user_id'], limit=10)
        for log in recent_logs:
            log['_id'] = str(log['_id'])
            log['user_id'] = str(log['user_id'])
            log['sender_email_id'] = str(log['sender_email_id'])
            log['sent_at'] = log['sent_at'].isoformat() if log.get('sent_at') else None

        return jsonify({
            'success': True,
            'sent': sent_count,
            'failed': len(failed_list),
            'failed_list': failed_list,
            'dashboard_stats': stats,
            'email_usage': email_usage,
            'recent_logs': recent_logs
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@user_bp.route('/logs')
@require_login
def logs():
    """Email logs page"""
    logs = EmailLog.get_by_user(session['user_id'])
    email_ids = {str(eid['_id']): eid['email'] for eid in EmailID.get_by_user(session['user_id'])}
    for log in logs:
        log['_id'] = str(log['_id'])
        log['user_id'] = str(log['user_id'])
        log['sender_email_id'] = str(log['sender_email_id'])
        log['sender_email'] = email_ids.get(log['sender_email_id'], 'Unknown')
    return render_template('user/logs.html', username=session['username'], logs=logs)


@user_bp.route('/api/logs', methods=['GET'])
@require_login
def get_logs():
    """Get paginated email logs"""
    page = int(request.args.get('page', 1))
    limit = int(request.args.get('limit', 100))
    logs = EmailLog.get_by_user_paginated(session['user_id'], page, limit)
    total_count = EmailLog.get_count(session['user_id'])
    
    email_ids = {str(eid['_id']): eid['email'] for eid in EmailID.get_by_user(session['user_id'])}
    for log in logs:
        log['_id'] = str(log['_id'])
        log['user_id'] = str(log['user_id'])
        log['sender_email_id'] = str(log['sender_email_id'])
        log['sender_email'] = email_ids.get(str(log['sender_email_id']), 'Unknown')
    
    return jsonify({
        'logs': logs,
        'total': total_count,
        'page': page,
        'limit': limit,
        'total_pages': (total_count + limit - 1) // limit if total_count > 0 else 1
    })