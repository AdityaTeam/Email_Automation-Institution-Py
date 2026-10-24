"""
User Panel Routes
Handles user dashboard, email management, CSV/Excel parsing, verification, and email sending
"""

from flask import Blueprint, render_template, request, jsonify, session, redirect, url_for, send_file
from models import EmailID, ExcelFile, Template, Requirement, Department, EmailLog
import os
import pandas as pd
import re
import dns.resolver
import io
import base64
import html
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
    'zoho.eu': {'smtp_server': 'smtp.zoho.eu', 'smtp_port': 587},
    'zoho.in': {'smtp_server': 'smtp.zoho.in', 'smtp_port': 587},
    'zoho.com.au': {'smtp_server': 'smtp.zoho.com.au', 'smtp_port': 587},
    'zohomail.com': {'smtp_server': 'smtp.zoho.com', 'smtp_port': 587},
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

EMAIL_REGEX = r"^[^@\s]+@[^@\s]+$"

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
    Verify email syntax, reserved-domain status, and the recipient domain's MX records.

    DNS can verify that a domain accepts mail, but it cannot reliably prove that a
    specific mailbox exists. SMTP recipient probing is intentionally not used.
    """
    if not isinstance(email, str):
        return False, "Invalid email type"

    email = email.strip()

    # 1. Regex Syntax Check BEFORE domain splitting
    if not re.fullmatch(EMAIL_REGEX, email):
        return False, "Invalid syntax"

    # Safely split domain
    parts = email.split('@')
    if len(parts) != 2:
        return False, "Invalid email format"

    domain = parts[1].strip().lower().rstrip('.')

    # Domain sanity check to prevent IDNA codec crash
    local_part = parts[0]
    if (
        not local_part
        or len(local_part) > 64
        or local_part.startswith('.')
        or local_part.endswith('.')
        or '..' in local_part
        or len(domain) > 253
        or '..' in domain
        or domain.startswith('-')
        or domain.endswith('-')
    ):
        return False, "Invalid domain format"

    domain_labels = domain.split('.')
    if (
        len(domain_labels) < 2
        or any(
            not label
            or len(label) > 63
            or label.startswith('-')
            or label.endswith('-')
            or not re.fullmatch(r'[a-z0-9-]+', label)
            for label in domain_labels
        )
        or len(domain_labels[-1]) < 2
    ):
        return False, "Invalid domain format"

    # 2. Block reserved test / disposable domains immediately
    if domain in DISALLOWED_DOMAINS:
        return False, f"Reserved/fake test domain ({domain})"

    # 3. DNS MX Record Lookup
    try:
        records = dns.resolver.resolve(domain, 'MX')
        if not records or len(records) == 0:
            return False, "No MX records found"
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return False, "Domain or MX record does not exist"
    except dns.resolver.NoNameservers:
        return False, "Unable to verify domain MX records (no nameservers)"
    except dns.resolver.Timeout:
        return False, "Unable to verify domain MX records (lookup timed out)"
    except dns.exception.DNSException as e:
        return False, f"Unable to verify domain MX records ({type(e).__name__})"

    # MX presence validates the receiving domain. SMTP RCPT probing is deliberately
    # not used because it is unreliable and can be treated as unsolicited traffic.
    return True, "Domain accepts mail (MX record found; mailbox not confirmed)"


def detect_smtp_settings(email):
    """Automatically detect SMTP settings based on email domain"""
    if not email or '@' not in email:
        return DEFAULT_SMTP.copy()
    domain = email.split('@')[-1].strip().lower()
    if domain in SMTP_CONFIG:
        return SMTP_CONFIG[domain].copy()
    return {}


def build_html_signature(signature_data, has_logo=False):
    """Build a compact, email-client-safe HTML signature."""
    name = html.escape(signature_data.get('executive_name', '').strip())
    position = html.escape(signature_data.get('position', '').strip())
    company = html.escape(signature_data.get('company_name', '').strip())
    phone = html.escape(signature_data.get('company_phone', '').strip())
    email_address = signature_data.get('company_email', '').strip()
    website = signature_data.get('company_website', '').strip()
    safe_email = html.escape(email_address)
    safe_website = html.escape(website)
    website_url = website if website.startswith(('http://', 'https://')) else f'https://{website}'
    safe_website_url = html.escape(website_url, quote=True)

    contact_rows = []
    if phone:
        contact_rows.append(
            f'<div style="margin-top:7px;color:#475569;">'
            f'<span style="color:#94a3b8;">T</span>&nbsp; {phone}</div>'
        )
    if email_address:
        contact_rows.append(
            f'<div style="margin-top:3px;">'
            f'<span style="color:#94a3b8;">E</span>&nbsp; '
            f'<a href="mailto:{safe_email}" style="color:#2563eb;text-decoration:none;">'
            f'{safe_email}</a></div>'
        )
    if website:
        contact_rows.append(
            f'<div style="margin-top:3px;">'
            f'<span style="color:#94a3b8;">W</span>&nbsp; '
            f'<a href="{safe_website_url}" style="color:#2563eb;text-decoration:none;">'
            f'{safe_website}</a></div>'
        )

    identity = '<br>'.join(value for value in (name, position, company) if value)
    logo_cell = ''
    divider = ''
    if has_logo:
        logo_cell = (
            '<td valign="middle" width="150" style="width:150px;padding:0 22px 0 0;">'
            '<img src="cid:company_logo" alt="Company logo" width="128" '
            'style="display:block;width:128px;max-width:128px;height:auto;border:0;">'
            '</td>'
        )
        divider = '<td width="1" style="width:1px;background:#dbe3ec;font-size:1px;line-height:1px;">&nbsp;</td>'

    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'style="margin-top:24px;font-family:Arial,Helvetica,sans-serif;color:#1f2937;'
        'font-size:13px;line-height:1.35;">'
        '<tr>'
        f'{logo_cell}{divider}'
        '<td valign="middle" style="padding:0 0 0 22px;">'
        '<div style="margin:0 0 8px;color:#64748b;font-size:12px;letter-spacing:.2px;">'
        'Best regards,</div>'
        f'<div style="font-size:15px;font-weight:700;color:#111827;">{identity}</div>'
        + ''.join(contact_rows)
        + '</td></tr></table>'
    )


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
    """Add a sender account with provider defaults or explicit SMTP settings."""
    data = request.get_json(silent=True) or {}
    email = data.get('email', '').strip()
    password = data.get('password', '').strip()
    
    if not email or not password:
        return jsonify({'error': 'Email and password are required'}), 400
    
    if '@' not in email:
        return jsonify({'error': 'Invalid email address'}), 400
    
    smtp = detect_smtp_settings(email)
    smtp_server = data.get('smtp_server', '').strip().lower()
    smtp_port = data.get('smtp_port', 587)
    use_ssl = bool(data.get('use_ssl', False))
    use_tls = bool(data.get('use_tls', not use_ssl))

    if smtp_server:
        if not re.match(r'^[a-zA-Z0-9.-]+$', smtp_server) or '..' in smtp_server:
            return jsonify({'error': 'Invalid SMTP server hostname'}), 400
        try:
            smtp_port = int(smtp_port)
        except (TypeError, ValueError):
            return jsonify({'error': 'SMTP port must be a number'}), 400
        if not 1 <= smtp_port <= 65535:
            return jsonify({'error': 'SMTP port must be between 1 and 65535'}), 400
        smtp = {'smtp_server': smtp_server, 'smtp_port': smtp_port}
    elif not smtp:
        return jsonify({
            'error': 'SMTP settings are required for custom domains. '
                     'Enter the SMTP server and port supplied by your email provider.'
        }), 400

    if use_ssl:
        use_tls = False
    
    email_data = {
        'email': email,
        'password': password,
        'smtp_server': smtp['smtp_server'],
        'smtp_port': smtp_port if smtp_server else smtp['smtp_port'],
        'use_tls': use_tls,
        'use_ssl': use_ssl
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
    for excel_file in excel_files:
        valid_rows = excel_file.get('recipients', [])
        invalid_rows = excel_file.get('invalid_recipients', [])
        all_rows = excel_file.get('all_recipients') or valid_rows + invalid_rows
        excel_file['valid_count'] = len(valid_rows)
        excel_file['invalid_count'] = len(invalid_rows)
        excel_file['total_count'] = len(all_rows)
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
        for col in df.columns:
            if col.lower() == 'name':
                name_col = col
        
        valid_recipients = []
        invalid_recipients = []
        all_recipients = []
        original_columns = [
            column for column in df.columns if column.lower() != 'institute'
        ]

        for _, row in df.iterrows():
            raw_email = row[email_col]
            email = '' if pd.isna(raw_email) else str(raw_email).strip()
            recipient = {'email': email}
            for column in original_columns:
                value = row[column]
                if pd.isna(value):
                    value = ''
                elif not isinstance(value, (str, int, float, bool)):
                    value = str(value)
                recipient.setdefault('original_data', {})[column] = value
            if name_col and recipient['original_data'].get(name_col):
                recipient['name'] = str(recipient['original_data'][name_col]).strip()
            is_valid, reason = verify_email_smtp(email)
            recipient['status_reason'] = reason
            recipient['is_valid'] = is_valid
            all_recipients.append(recipient)
            (valid_recipients if is_valid else invalid_recipients).append(recipient)

        excel_file = ExcelFile.create(
            session['user_id'], filename, file.filename, valid_recipients,
            all_recipients=all_recipients, invalid_recipients=invalid_recipients,
            original_columns=original_columns
        )
        if excel_file is None:
            return jsonify({'error': 'Could not save the uploaded file'}), 500
        os.remove(filepath)
        
        return jsonify({
            'success': True,
            'file_id': str(excel_file['_id']),
            'valid_count': len(valid_recipients),
            'invalid_count': len(invalid_recipients),
            'valid_recipients': valid_recipients,
            'invalid_recipients': invalid_recipients,
            'total_count': len(all_recipients),
            'validation_percentage': round(
                (len(valid_recipients) / len(all_recipients) * 100), 1
            ) if all_recipients else 0
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
    
    if not file or str(file.get('user_id')) != str(session['user_id']):
        return jsonify({'error': 'File not found'}), 404
    
    # Convert ObjectIds to string
    file['_id'] = str(file['_id'])
    file['user_id'] = str(file['user_id'])
    # Older records may not have validation metadata. Reconstruct a stable
    # response from the valid and invalid partitions used for sending/export.
    valid_rows = file.get('recipients', [])
    invalid_rows = file.get('invalid_recipients', [])
    if not file.get('all_recipients'):
        file['all_recipients'] = valid_rows + invalid_rows
    file.setdefault('valid_count', len(valid_rows))
    file.setdefault('invalid_count', len(invalid_rows))
    file.setdefault('total_count', len(file['all_recipients']))
    valid_emails = {str(row.get('email', '')).strip().lower() for row in valid_rows}
    for row in file['all_recipients']:
        row['is_valid'] = str(row.get('email', '')).strip().lower() in valid_emails
        row.setdefault(
            'status_reason',
            'Domain accepts mail (mailbox not confirmed)'
            if row['is_valid'] else 'Failed validation'
        )
    
    return jsonify({'file': file})


def _export_recipients(file_id, valid):
    """Export the selected validation subset while retaining source columns."""
    file = ExcelFile.get_by_id(file_id)
    if not file or str(file.get('user_id')) != str(session['user_id']):
        return jsonify({'error': 'File not found'}), 404
    rows = file.get('recipients', []) if valid else file.get('invalid_recipients', [])
    columns = file.get('original_columns') or ['Email', 'Name']
    columns = [column for column in columns if column.lower() != 'institute']
    data = [{column: row.get('original_data', {}).get(column, '') for column in columns} for row in rows]
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        pd.DataFrame(data, columns=columns).to_excel(writer, index=False)
    output.seek(0)
    suffix = 'valid' if valid else 'invalid'
    return send_file(output, as_attachment=True, download_name=f'{suffix}_recipients.xlsx',
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@user_bp.route('/api/excel-files/<file_id>/export-valid', methods=['GET'])
@require_login
def export_valid_recipients(file_id):
    return _export_recipients(file_id, True)


@user_bp.route('/api/excel-files/<file_id>/export-invalid', methods=['GET'])
@require_login
def export_invalid_recipients(file_id):
    return _export_recipients(file_id, False)


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
    """Return admin-managed templates for user selection only."""
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
    if not isinstance(recipients, list):
        return jsonify({'error': 'Recipients must be a list'}), 400
    if isinstance(recipients, list):
        recipients = [
            recipient for recipient in recipients
            if isinstance(recipient, dict) and recipient.get('email')
            and recipient.get('is_valid', True) is not False
        ]
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
    inline_logo = None

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

    logo_data_url = signature_data.get('logo_data_url')
    if logo_data_url:
        match = re.match(r'^data:image/(png|jpe?g|gif|webp);base64,([A-Za-z0-9+/=]+)$', logo_data_url)
        if not match:
            return jsonify({'error': 'Invalid company logo'}), 400
        try:
            logo_bytes = base64.b64decode(match.group(2), validate=True)
        except (ValueError, base64.binascii.Error):
            return jsonify({'error': 'Invalid company logo data'}), 400
        if not logo_bytes or len(logo_bytes) > 2 * 1024 * 1024:
            return jsonify({'error': 'Company logo must be smaller than 2 MB'}), 400
        subtype = 'jpeg' if match.group(1) in ('jpg', 'jpeg') else match.group(1)
        inline_logo = {'data': logo_bytes, 'subtype': subtype, 'filename': f'company_logo.{subtype}'}
        # Inline images require an HTML part; preserve the text body as the
        # plain-text alternative generated by EmailSender.
        is_html = True
    
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
        signature = build_html_signature(signature_data, has_logo=inline_logo is not None)
    
    personalized_recipients = []
    for r in recipients:
        personalized_body = body
        name = r.get('name', '')
        if name:
            personalized_body = personalized_body.replace('{{name}}', name)
        personalized_body = (
            personalized_body + ('<br><br>' if is_html else '\n\n') + signature
        )
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
            separate_threads=separate_threads,
            inline_logo=inline_logo
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