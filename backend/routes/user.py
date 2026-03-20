"""
User Panel Routes - Backup
Handles user dashboard, email management, and email sending
"""

from flask import Blueprint, render_template, request, jsonify, session, redirect, url_for
from regex import template
from models import EmailID, ExcelFile, Template, Requirement, EmailLog
from database import MongoDB, Collections
from bson import ObjectId
import os
import pandas as pd
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
    """Upload and process Excel/CSV"""
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
        
        df.columns = [col.strip() for col in df.columns]
        
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
        
        recipients = []
        for _, row in df.iterrows():
            email = str(row[email_col]).strip() if pd.notna(row[email_col]) else ''
            if email and '@' in email:
                recipient = {'email': email}
                if name_col:
                    recipient['name'] = str(row[name_col]).strip() if pd.notna(row[name_col]) else ''
                if institute_col:
                    recipient['institute'] = str(row[institute_col]).strip() if pd.notna(row[institute_col]) else ''
                recipients.append(recipient)
        
        excel_file = ExcelFile.create(session['user_id'], filename, file.filename, recipients)
        os.remove(filepath)
        
        return jsonify({
            'success': True,
            'file_id': str(excel_file['_id']),
            'recipients': recipients[:10],
            'count': len(recipients)
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
    
    return render_template('user/compose.html',
                           username=session['username'],
                           email_ids=email_ids,
                           excel_files=excel_files,
                           requirements=requirements)


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
    requirements = Requirement.get_all()
    for r in requirements:
        r['_id'] = str(r['_id'])
    return jsonify({'requirements': requirements})


@user_bp.route('/api/send', methods=['POST'])
@require_login
def send_emails():
    """Send bulk emails with automatic sender rotation"""
    data = request.json
    recipients = data.get('recipients', [])
    sender_email_id = data.get('sender_email_id', '')
    from_name = data.get('from_name', session['username'])
    subject = data.get('subject', '')
    body = data.get('body', '')
    template_id = data.get('template_id')
    attachments = []
    is_html = data.get('is_html', False)
    signature_data = data.get('signature_data', {})
    
    # debug start
    print("send_emails called. template_id=", template_id)
    # Fetch template attachments if template_id provided
    if template_id:
        template = Template.get_by_id(template_id)
        if template and 'attachments' in template:
            # Convert to absolute paths
            BASE_DIR = os.path.abspath(os.getcwd())

            for rel_path in template['attachments']:
                abs_path = os.path.join(BASE_DIR, rel_path)

                if os.path.exists(abs_path):
                    attachments.append(abs_path)
                else:
                    print("Attachment missing:", abs_path)
        else:
            # no attachments key or template not found
            print("No attachments found for template", template_id)

    # debug log always show attachments list (even if empty)
    print("Computed attachments list:", attachments)
    
    if not recipients or not sender_email_id or not subject or not body:
        return jsonify({'error': 'All fields required'}), 400
    
    signature = Template.build_signature(signature_data)
    user_email_ids = EmailID.get_by_user_with_passwords(session['user_id'])
    
    # Build email accounts list with their current sent counts from database
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
            'emails_sent': eid.get('emails_sent', 0)  # Get current count from DB
        })
    
    # If no email accounts available
    if not email_accounts:
        return jsonify({'error': 'No sender email IDs configured'}), 400
    
    # Find starting index based on selected sender
    start_index = 0
    for i, acc in enumerate(email_accounts):
        if acc['_id'] == sender_email_id:
            start_index = i
            break
    
    if not is_html:
        body = Template.process_body(body)
        signature = Template.process_body(signature)
        is_html = True
    
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
        sender = EmailSender(email_accounts, batch_size=BATCH_SIZE)
        
        # Set the current account index to start from selected sender
        sender.current_account_index = start_index
        
        sent_count = 0
        failed_list = []
        
        for r in personalized_recipients:
            # CRITICAL: Check rotation BEFORE every send (DB-driven)
            if sender.needs_rotation():
                available = sender.find_next_available_account()
                if not available:
                    # All accounts exhausted → RESET
                    print("🔄 ALL ACCOUNTS EXHAUSTED - RESETTING COUNTS!")
                    
                    EmailID.reset_counts(session['user_id'])
                    
                    # Reset ALL local account counts to sync with DB
                    for acc in email_accounts:
                        acc['emails_sent'] = 0
                    
                    # Reset EmailSender state
                    sender.current_account_index = 0
            
            current_acc = sender.get_current_account()
            if not current_acc:
                failed_list.append({'email': r['email'], 'error': 'No available sender'})
                continue
            
            print(f"📧 Using {current_acc['email']} ({sender.get_account_sent_count()}/25)")
            
            success = sender.send_single_email(
                to_email=r['email'],
                subject=subject,
                body=r['body'],
                from_name=from_name,
                attachments=attachments,
                is_html=is_html
            )
            
            if success:
                # SINGLE increment after successful send (DB + local sync)
                sender.increment_current_account()
                EmailID.increment_sent_count(current_acc['_id'])
                
                EmailLog.create(session['user_id'], current_acc['_id'], r['email'], subject, 'sent')
                sent_count += 1
                print(f"✅ Sent #{sent_count} via {current_acc['email']}")
            else:
                error_msg = sender.failed[-1]['error'] if sender.failed else 'Unknown'
                EmailLog.create(session['user_id'], sender_email_id, r['email'], subject, 'failed', error_msg)
                failed_list.append({'email': r['email'], 'error': error_msg})
                print(f"❌ Failed: {error_msg}")
        
        return jsonify({'success': True, 'sent': sent_count, 'failed': len(failed_list), 'failed_list': failed_list})
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
    """Get email logs"""
    logs = EmailLog.get_by_user(session['user_id'])
    email_ids = {str(eid['_id']): eid['email'] for eid in EmailID.get_by_user(session['user_id'])}
    for log in logs:
        log['_id'] = str(log['_id'])
        log['user_id'] = str(log['user_id'])
        log['sender_email_id'] = str(log['sender_email_id'])
        log['sender_email'] = email_ids.get(str(log['sender_email_id']), 'Unknown')
    return jsonify({'logs': logs})

