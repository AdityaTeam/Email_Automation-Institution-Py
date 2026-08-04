"""
User Panel Routes - Backup
Handles user dashboard, email management, and email sending
"""

from smtp_validator import validate_email
from flask import Blueprint, render_template, request, jsonify, session, redirect, url_for
from models import EmailID, ExcelFile, Template, Requirement, EmailLog
from database import MongoDB, Collections
from bson import ObjectId
import os
import re
import json
import mimetypes
import traceback
import pandas as pd
from werkzeug.utils import secure_filename
from email_sender import EmailSender
from flask import send_file
from models import RepositoryCategory, RepositoryFile

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
            if request.path.startswith('/api/') or request.is_json or request.accept_mimetypes.accept_json:
                return jsonify({'success': False, 'error': 'Session expired or not logged in. Please log in again.'}), 401
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

    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400

    file = request.files['file']

    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400

    filename = secure_filename(file.filename)

    os.makedirs('uploads', exist_ok=True)

    filepath = os.path.join('uploads', filename)

    file.save(filepath)

    try:

        if filename.endswith(('.xlsx', '.xls')):

            df = pd.read_excel(
                filepath,
                usecols=lambda x: x.lower().strip() in [
                    'email',
                    'name',
                    'institute'
                ],
                dtype=str,
                engine='openpyxl',
                na_filter=False
            )

        else:

            os.remove(filepath)

            return jsonify({
                'error': 'Only Excel files allowed'
            }), 400

        df.columns = [str(col).strip() for col in df.columns]

        email_col = None

        for col in df.columns:

            if col.lower() == 'email':
                email_col = col
                break

        if not email_col:

            os.remove(filepath)

            return jsonify({
                'error': 'Email column not found'
            }), 400

        name_col = None
        institute_col = None

        for col in df.columns:

            if col.lower() == 'name':
                name_col = col

            if col.lower() == 'institute':
                institute_col = col

        recipients = []

        valid_count = 0
        invalid_count = 0

        validation_cache = {}

        rows = df.to_dict(orient='records')

        for row in rows:

            email = str(row.get(email_col, '')).strip()

            if not email or email.lower() == 'nan':
                continue

            if email in validation_cache:

                is_valid, reason = validation_cache[email]

            else:

                is_valid, reason = validate_email(email)

                validation_cache[email] = (is_valid, reason)

            recipient = {
                'email': email,
                'status': 'VALID' if is_valid else 'INVALID',
                'reason': reason,
                'name': '',
                'institute': ''
            }

            if name_col:

                recipient['name'] = str(
                    row.get(name_col, '')
                ).strip()

            if institute_col:

                recipient['institute'] = str(
                    row.get(institute_col, '')
                ).strip()

            if is_valid:
                valid_count += 1
            else:
                invalid_count += 1

            recipients.append(recipient)

        excel_file = ExcelFile.create(
            session['user_id'],
            filename,
            file.filename,
            recipients
        )

        os.remove(filepath)

        return jsonify({

            'success': True,

            'file_id': str(excel_file['_id']),

            'total_count': len(recipients),

            'valid_count': valid_count,

            'invalid_count': invalid_count,

            'preview_recipients': recipients[:50],

            'valid_emails': [
                r for r in recipients
                if r['status'] == 'VALID'
            ],

            'invalid_emails': [
                r for r in recipients
                if r['status'] == 'INVALID'
            ]

        })

    except Exception as e:

        if os.path.exists(filepath):
            os.remove(filepath)

        return jsonify({
            'error': str(e)
        }), 500


@user_bp.route('/api/excel-files/<file_id>', methods=['GET'])
@require_login
def get_excel_file(file_id):
    """Get single excel file with validated recipients"""
    file = ExcelFile.get_by_id(file_id)
    
    if not file:
        return jsonify({'error': 'File not found'}), 404
    
    recipients = file.get('recipients', [])
    validation_cache = {}
    valid_count = 0
    invalid_count = 0
    
    # Ensure every recipient has status and reason
    for r in recipients:
        email = str(r.get('email', '')).strip()
        status = r.get('status')
        reason = r.get('reason')
        if not status or not reason:
            if email in validation_cache:
                is_valid, v_reason = validation_cache[email]
            else:
                is_valid, v_reason = validate_email(email)
                validation_cache[email] = (is_valid, v_reason)
            r['status'] = 'VALID' if is_valid else 'INVALID'
            r['reason'] = v_reason or ('Mailbox exists' if is_valid else 'Invalid email')
        
        if str(r.get('status', '')).upper() == 'VALID':
            valid_count += 1
        else:
            invalid_count += 1

    # Convert ObjectIds to string
    file['_id'] = str(file['_id'])
    file['user_id'] = str(file['user_id'])
    file['recipients'] = recipients
    
    total = len(recipients)
    rate = round((valid_count / total * 100), 1) if total > 0 else 0

    return jsonify({
        'file': file,
        'recipients': recipients,
        'total_count': total,
        'valid_count': valid_count,
        'invalid_count': invalid_count,
        'validation_rate': rate
    })


@user_bp.route('/api/validate-recipients', methods=['POST'])
@require_login
def validate_recipients_api():
    """Live batch validation of recipients"""
    try:
        data = request.get_json(silent=True) or {}
        input_recipients = data.get('recipients', [])
        if not input_recipients and 'file_id' in data:
            file = ExcelFile.get_by_id(data['file_id'])
            if file:
                input_recipients = file.get('recipients', [])

        validated_recipients = []
        validation_cache = {}
        valid_count = 0
        invalid_count = 0

        for item in input_recipients:
            if isinstance(item, str):
                email = item.strip()
                name = ''
                institute = ''
            elif isinstance(item, dict):
                email = str(item.get('email', '')).strip()
                name = str(item.get('name', '')).strip()
                institute = str(item.get('institute', '')).strip()
            else:
                continue

            if not email or email.lower() == 'nan':
                continue

            if email in validation_cache:
                is_valid, reason = validation_cache[email]
            else:
                is_valid, reason = validate_email(email)
                validation_cache[email] = (is_valid, reason)

            rec = {
                'email': email,
                'name': name,
                'institute': institute,
                'status': 'VALID' if is_valid else 'INVALID',
                'reason': reason or ('Mailbox exists' if is_valid else 'Invalid email')
            }
            if is_valid:
                valid_count += 1
            else:
                invalid_count += 1
            validated_recipients.append(rec)

        total = len(validated_recipients)
        rate = round((valid_count / total * 100), 1) if total > 0 else 0

        return jsonify({
            'success': True,
            'recipients': validated_recipients,
            'total_count': total,
            'valid_count': valid_count,
            'invalid_count': invalid_count,
            'validation_rate': rate
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@user_bp.route('/api/export-recipients', methods=['POST'])
@require_login
def export_recipients_api():
    """Export valid or invalid recipients as CSV or Excel"""
    try:
        data = request.get_json(silent=True) or {}
        recipients = data.get('recipients', [])
        status_filter = str(data.get('status', 'VALID')).upper()
        export_format = str(data.get('format', 'csv')).lower()

        filtered = [r for r in recipients if str(r.get('status', '')).upper() == status_filter]
        if not filtered and not recipients:
            return jsonify({'error': 'No recipients to export'}), 400

        rows = []
        for idx, r in enumerate(filtered, 1):
            rows.append({
                '#': idx,
                'Email': r.get('email', ''),
                'Name': r.get('name', ''),
                'Institute': r.get('institute', ''),
                'Status': r.get('status', ''),
                'Reason': r.get('reason', '')
            })

        df = pd.DataFrame(rows)
        output = io.BytesIO()
        if export_format in ['xlsx', 'excel']:
            filename = f"{status_filter.lower()}_recipients.xlsx"
            mimetype = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
            with pd.ExcelWriter(output, engine='openpyxl') as writer:
                df.to_excel(writer, index=False, sheet_name=f'{status_filter} Recipients')
        else:
            filename = f"{status_filter.lower()}_recipients.csv"
            mimetype = 'text/csv'
            df.to_csv(output, index=False)

        output.seek(0)
        return send_file(
            output,
            mimetype=mimetype,
            as_attachment=True,
            download_name=filename
        )
    except Exception as e:
        return jsonify({'error': str(e)}), 500


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
    """Send bulk emails with automatic sender rotation and full line-by-line tracing"""
    try:
        content_type = str(request.content_type or '')
        print("\n" + "=" * 80)
        print("🚀 [STEP 1] Request received for /api/send")
        print("   Content-Type:", request.content_type)
        print("   Form Keys:", list(request.form.keys()) if request.form else [])
        print("   Files Keys:", list(request.files.keys()) if request.files else [])
        print(f"   User ID (session): {session.get('user_id')}")
        print(f"   Username (session): {session.get('username')}")
        print("=" * 80)

        custom_logo = None
        attachments = []
        sender_email_id = ''
        template_id = None
        subject = ''
        body = ''
        from_name = ''
        is_html = False
        separate_threads = True
        recipients = []
        cc_emails = []
        signature_data = {}

        # 1. Strictly isolated multipart/form-data path (ZERO calls to request.get_json or request.json)
        if content_type.startswith("multipart/form-data") or 'multipart/form-data' in content_type:
            print("📋 [STEP 2] Parsing multipart/form-data form fields...")
            sender_email_id = str(request.form.get("sender_email_id", "") or "").strip()
            template_id = request.form.get("template_id")
            subject = str(request.form.get("subject", "") or "").strip()
            body = str(request.form.get("body", "") or "").strip()
            from_name = str(request.form.get("from_name") or session.get("username", "Sender") or "Sender").strip()
            
            is_html_val = request.form.get("is_html", "false")
            is_html = str(is_html_val).lower() in ["true", "1", "yes"]
            
            sep_val = request.form.get("separate_threads", "true")
            separate_threads = str(sep_val).lower() in ["true", "1", "yes"]

            raw_recipients = request.form.get("recipients")
            if raw_recipients:
                try:
                    recipients = json.loads(raw_recipients)
                except Exception as e:
                    print(f"❌ [STEP 2] Malformed JSON in recipients: {e}")
                    return jsonify({'success': False, 'error': f'Invalid JSON format in recipients: {e}'}), 400
            else:
                recipients = []

            raw_cc = request.form.get("cc_emails")
            if raw_cc:
                try:
                    cc_emails = json.loads(raw_cc)
                except Exception as e:
                    print(f"⚠️ [STEP 2] Malformed JSON in cc_emails: {e}")
                    cc_emails = [c.strip() for c in str(raw_cc).split(',') if c.strip()]
            else:
                cc_emails = []

            raw_sig = request.form.get("signature_data")
            if raw_sig:
                try:
                    signature_data = json.loads(raw_sig)
                except Exception as e:
                    print(f"⚠️ [STEP 2] Malformed JSON in signature_data: {e}")
                    signature_data = {}
            else:
                signature_data = {}

        # 2. application/json request path
        elif content_type.startswith("application/json") or 'application/json' in content_type:
            print("📋 [STEP 2] Parsing application/json payload...")
            try:
                data = request.get_json(silent=True) or {}
            except Exception as e:
                return jsonify({'success': False, 'error': f'Invalid JSON payload: {e}'}), 400

            recipients = data.get('recipients', [])
            cc_emails = data.get('cc_emails', [])
            sender_email_id = str(data.get('sender_email_id', '') or '').strip()
            from_name = str(data.get('from_name') or session.get('username', 'Sender') or 'Sender').strip()
            subject = str(data.get('subject', '') or '').strip()
            body = str(data.get('body', '') or '').strip()
            template_id = data.get('template_id')
            is_html = bool(data.get('is_html', False))
            separate_threads = bool(data.get('separate_threads', True))
            signature_data = data.get('signature_data', {})
        else:
            print("📋 [STEP 2] Parsing fallback request...")
            if request.form:
                sender_email_id = str(request.form.get("sender_email_id", "") or "").strip()
                template_id = request.form.get("template_id")
                subject = str(request.form.get("subject", "") or "").strip()
                body = str(request.form.get("body", "") or "").strip()
                from_name = str(request.form.get("from_name") or session.get("username", "Sender") or "Sender").strip()
                is_html = str(request.form.get("is_html", "")).lower() in ["true", "1", "yes"]
                separate_threads = str(request.form.get("separate_threads", "")).lower() in ["true", "1", "yes"]
                try:
                    recipients = json.loads(request.form.get("recipients", "[]"))
                except Exception:
                    recipients = []
                try:
                    cc_emails = json.loads(request.form.get("cc_emails", "[]"))
                except Exception:
                    cc_emails = []
                try:
                    signature_data = json.loads(request.form.get("signature_data", "{}"))
                except Exception:
                    signature_data = {}
            else:
                data = request.get_json(silent=True) or {}
                recipients = data.get('recipients', [])
                cc_emails = data.get('cc_emails', [])
                sender_email_id = str(data.get('sender_email_id', '') or '').strip()
                from_name = str(data.get('from_name') or session.get('username', 'Sender') or 'Sender').strip()
                subject = str(data.get('subject', '') or '').strip()
                body = str(data.get('body', '') or '').strip()
                template_id = data.get('template_id')
                is_html = bool(data.get('is_html', False))
                separate_threads = bool(data.get('separate_threads', True))
                signature_data = data.get('signature_data', {})

        # [STEP 3] Parse uploaded logo file safely
        if request.files and 'logo' in request.files:
            logo_file = request.files.get("logo")
            if logo_file is not None and getattr(logo_file, 'filename', None) and logo_file.filename.strip():
                print("🖼️ [STEP 3] Logo received:", logo_file.filename)
                filename = secure_filename(logo_file.filename)
                if not filename:
                    filename = "company_logo.png"
                ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
                allowed_logo_exts = {'png', 'jpg', 'jpeg', 'svg', 'webp'}
                
                print(f"   [STEP 3] Logo filename: '{filename}', Ext: '{ext}', Content-Type: '{getattr(logo_file, 'content_type', '')}'")
                
                if ext not in allowed_logo_exts:
                    print(f"❌ [STEP 3] Unsupported logo extension '{ext}'")
                    return jsonify({
                        'success': False,
                        'error': f"Unsupported logo format (.{ext}). Allowed formats: PNG, JPG, JPEG, SVG, WEBP."
                    }), 400

                try:
                    logo_bytes = logo_file.read()
                except Exception as read_err:
                    print(f"❌ [STEP 3] Error reading logo file: {read_err}")
                    return jsonify({'success': False, 'error': f'Failed to read uploaded logo file: {read_err}'}), 400

                logo_size = len(logo_bytes) if logo_bytes else 0
                print(f"   [STEP 3] Logo bytes read: {logo_size} bytes")

                if logo_size > 2 * 1024 * 1024:
                    print(f"❌ [STEP 3] Logo file exceeds 2MB limit ({logo_size} bytes)")
                    return jsonify({
                        'success': False,
                        'error': 'Logo file size exceeds the maximum allowed limit of 2 MB.'
                    }), 400

                if logo_size == 0:
                    print("⚠️ [STEP 3] Warning: Uploaded logo file is 0 bytes, falling back to default logo")
                else:
                    mimetype = getattr(logo_file, 'content_type', None) or mimetypes.guess_type(filename)[0] or 'image/png'
                    custom_logo = {
                        'filename': filename,
                        'data': logo_bytes,
                        'mimetype': mimetype
                    }
                    print(f"✅ [STEP 3] Custom logo validated and ready: {filename} ({logo_size} bytes, MIME: {mimetype})")

        # [STEP 4] Normalize and validate parameters
        if isinstance(cc_emails, str):
            cc_emails = [c.strip() for c in cc_emails.split(',') if c.strip()]
        elif isinstance(cc_emails, list):
            cc_emails = [str(c).strip() for c in cc_emails if str(c).strip()]
        else:
            cc_emails = []

        print("📋 [STEP 4] Validating parameters:")
        print(f"   Sender ID: {sender_email_id}")
        print(f"   From Name: {from_name}")
        print(f"   Subject: {subject}")
        print(f"   Template ID: {template_id}")
        print(f"   Is HTML: {is_html}")
        print(f"   Custom Logo: {'Yes' if custom_logo else 'No'}")
        print(f"   CC Emails: {cc_emails}")
        print(f"   Signature Data: {signature_data}")

        if not recipients:
            print("❌ [STEP 4] Validation Error: No recipients provided")
            return jsonify({'success': False, 'error': 'No recipients provided'}), 400
        if not sender_email_id:
            print("❌ [STEP 4] Validation Error: No sender email ID selected")
            return jsonify({'success': False, 'error': 'Please select a sender email ID'}), 400
        if not subject:
            print("❌ [STEP 4] Validation Error: Email subject is missing")
            return jsonify({'success': False, 'error': 'Email subject is required'}), 400
        if not body:
            print("❌ [STEP 4] Validation Error: Email body is missing")
            return jsonify({'success': False, 'error': 'Email body is required'}), 400

        if not isinstance(recipients, list):
            print("❌ [STEP 4] Validation Error: Recipients must be a list")
            return jsonify({'success': False, 'error': 'Recipients must be a list'}), 400

        normalized_recipients = []
        for r in recipients:
            if isinstance(r, dict):
                email = str(r.get('email', '') or '').strip()
                name = str(r.get('name', '') or '').strip() if r.get('name') else ''
                institute = str(r.get('institute', '') or '').strip() if r.get('institute') else ''
            elif isinstance(r, str):
                email = r.strip()
                name = ''
                institute = ''
            else:
                continue

            if email and '@' in email:
                normalized_recipients.append({'email': email, 'name': name, 'institute': institute})

        print(f"👥 [STEP 4] Total parsed valid recipients: {len(normalized_recipients)}")

        if not normalized_recipients:
            print("❌ [STEP 4] Validation Error: No valid email addresses found in recipients")
            return jsonify({'success': False, 'error': 'No valid recipient email addresses found in request'}), 400

        # [STEP 5] Initialize sender accounts from database
        user_id = session.get('user_id')
        user_email_ids = []
        try:
            user_email_ids = EmailID.get_by_user_with_passwords(user_id) if user_id else []
        except Exception as e:
            print(f"⚠️ Warning retrieving email accounts for user: {e}")

        try:
            selected_account_doc = EmailID.get_by_id_with_password(sender_email_id)
            if selected_account_doc:
                doc_id_str = str(selected_account_doc.get('_id', ''))
                already_present = any(str(e.get('_id', '')) == doc_id_str for e in user_email_ids)
                if not already_present:
                    user_email_ids.insert(0, selected_account_doc)
        except Exception as e:
            print(f"⚠️ Warning retrieving selected sender account: {e}")

        if not user_email_ids:
            print(f"❌ [STEP 5] Sender Account Error: No email accounts found for user {user_id} or ID {sender_email_id}")
            return jsonify({'success': False, 'error': 'No sender email accounts found for your user. Please configure an email account first.'}), 400

        email_accounts = []
        for eid in user_email_ids:
            email_accounts.append({
                'email': str(eid.get('email', '') or '').strip(),
                'password': str(eid.get('password', '') or '').strip(),
                'smtp_server': str(eid.get('smtp_server') or 'smtp.gmail.com').strip(),
                'smtp_port': int(eid.get('smtp_port') or 587),
                'use_tls': eid.get('use_tls', True),
                'use_ssl': eid.get('use_ssl', False),
                '_id': str(eid.get('_id', '')),
                'emails_sent': eid.get('emails_sent', 0)
            })

        start_index = 0
        for i, acc in enumerate(email_accounts):
            if acc['_id'] == sender_email_id or acc['email'] == sender_email_id:
                start_index = i
                break

        # [STEP 6] Preparing MIME, personalizing body, and embedding signature / logo
        print("📝 [STEP 6] Preparing message bodies and resolving attachments...")
        if template_id and str(template_id).strip():
            try:
                template = Template.get_by_id(template_id)
                if template and 'attachments' in template and isinstance(template['attachments'], list):
                    BASE_DIR = os.path.abspath(os.getcwd())
                    for rel_path in template['attachments']:
                        if not rel_path:
                            continue
                        if os.path.isabs(rel_path):
                            abs_path = rel_path
                        else:
                            abs_path = os.path.normpath(os.path.join(BASE_DIR, rel_path))

                        if os.path.exists(abs_path):
                            file_size = os.path.getsize(abs_path)
                            mime_type, _ = mimetypes.guess_type(abs_path)
                            print(f"📎 Attachment verified: {abs_path} (Size: {file_size} bytes, MIME: {mime_type})")
                            attachments.append(abs_path)
                        else:
                            print(f"⚠️ Attachment missing on disk: {abs_path}")
            except Exception as tmpl_err:
                print(f"⚠️ Warning: Error resolving template attachments: {tmpl_err}")

        # Check if default logo exists on disk
        default_logo_paths = [
            os.path.join(os.getcwd(), 'backend', 'uploads', 'logo', 'company_logo.jpeg'),
            os.path.join(os.getcwd(), 'uploads', 'logo', 'company_logo.jpeg'),
            os.path.join(os.path.dirname(__file__), '..', 'uploads', 'logo', 'company_logo.jpeg'),
        ]
        has_default_logo = any(os.path.exists(p) for p in default_logo_paths)
        has_logo = (custom_logo is not None) or has_default_logo

        # Auto-enable is_html if a logo is present, signature is present, or body contains HTML
        if has_logo or custom_logo or ('<' in body and '>' in body):
            is_html = True

        # Build signature and body
        try:
            signature = Template.build_signature(signature_data)
        except Exception:
            signature = ""

        if is_html:
            try:
                processed_body = Template.process_body(body)
            except Exception:
                processed_body = body
            try:
                processed_signature = Template.process_body(signature)
            except Exception:
                processed_signature = signature

            if has_logo:
                logo_img_tag = '<br><br><img src="cid:company_logo" alt="Company Logo" style="max-width: 140px; max-height: 70px; height: auto;" border="0" />'
                if processed_signature:
                    processed_signature = processed_signature + logo_img_tag
                else:
                    processed_signature = logo_img_tag
        else:
            processed_body = body
            processed_signature = signature

        personalized_recipients = []
        for r in normalized_recipients:
            p_body = processed_body
            if r.get('name'):
                p_body = p_body.replace('{{name}}', r['name'])
            if r.get('institute'):
                p_body = p_body.replace('{{institute}}', r['institute'])
            if processed_signature:
                if is_html:
                    p_body = p_body + '<br><br>' + processed_signature
                else:
                    p_body = p_body + '\n\n' + processed_signature
            personalized_recipients.append({'email': r['email'], 'body': p_body})

        if personalized_recipients:
            print(f"📝 [STEP 6] Prepared {len(personalized_recipients)} messages. Sample HTML body for {personalized_recipients[0]['email']}:\n{personalized_recipients[0]['body']}")

        # [STEP 7] Initializing EmailSender and executing SMTP dispatch
        print("🚀 [STEP 7] Initializing EmailSender and starting bulk dispatch...")
        BATCH_SIZE = 25
        sender = EmailSender(email_accounts, batch_size=BATCH_SIZE, user_id=user_id)
        sender.current_account_index = start_index

        result = sender.send_bulk_emails(
            personalized_recipients,
            subject,
            "",
            from_name,
            cc_emails=cc_emails,
            attachments=attachments,
            is_html=is_html,
            delay_between_emails=1,
            separate_threads=separate_threads,
            custom_logo=custom_logo
        )

        sent_entries = result.get("sent_entries", [])
        failed_list = result.get("failed", [])
        sent_count = len(sent_entries)
        print(f"📊 [STEP 7] Sending finished: {sent_count} sent, {len(failed_list)} failed")

        # [STEP 8] Updating database logs safely
        print("💾 [STEP 8] Updating database logs...")
        for sent_entry in sent_entries:
            log_sender_id = sent_entry.get('sender_email_id') or sender_email_id
            try:
                EmailLog.create(user_id, log_sender_id, sent_entry['email'], subject, 'sent')
            except Exception as log_err:
                print(f"   ⚠️ Database write warning for sent log: {log_err}")

        for fail_entry in failed_list:
            log_sender_id = fail_entry.get('sender_email_id') or sender_email_id
            try:
                EmailLog.create(
                    user_id,
                    log_sender_id,
                    fail_entry.get('email', ''),
                    subject,
                    'failed',
                    fail_entry.get('error', 'Send failed')
                )
            except Exception as log_err:
                print(f"   ⚠️ Database write warning for failed log: {log_err}")

        try:
            stats = EmailLog.get_stats(user_id)
        except Exception:
            stats = {'sent': sent_count, 'failed': len(failed_list)}

        try:
            refreshed_email_ids = EmailID.get_by_user(user_id)
            email_usage = []
            for eid in refreshed_email_ids:
                email_usage.append({
                    '_id': str(eid['_id']),
                    'email': eid.get('email', ''),
                    'emails_sent': eid.get('emails_sent', 0)
                })
        except Exception:
            email_usage = []

        try:
            recent_logs = EmailLog.get_by_user(user_id, limit=10)
            for log in recent_logs:
                log['_id'] = str(log['_id'])
                log['user_id'] = str(log['user_id'])
                log['sender_email_id'] = str(log['sender_email_id'])
                log['sent_at'] = log['sent_at'].isoformat() if log.get('sent_at') else None
        except Exception:
            recent_logs = []

        # [STEP 9] Returning JSON response to client
        print("🎉 [STEP 9] Returning JSON response to client:")
        print(f"   Success: True, Sent: {sent_count}, Failed: {len(failed_list)}")
        print("=" * 80 + "\n")

        return jsonify({
            'success': True,
            'sent': sent_count,
            'failed': len(failed_list),
            'failed_list': failed_list,
            'dashboard_stats': stats,
            'email_usage': email_usage,
            'recent_logs': recent_logs
        }), 200

    except Exception as e:
        tb = traceback.format_exc()
        print("\n" + "=" * 80)
        print("🔥 CRITICAL BACKEND EXCEPTION IN /api/send:")
        print(tb)
        print("=" * 80 + "\n")
        return jsonify({
            'success': False,
            'error': str(e),
            'traceback': tb
        }), 500


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
    """Get paginated email logs (FEATURE 2)"""
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
# ==========================
# DATA REPOSITORY
# ==========================

@user_bp.route('/data-repository')
@require_login
def data_repository():

    return render_template(
        'user/data_repository.html',
        username=session['username']
    )


@user_bp.route('/user/categories')
@require_login
def get_categories():

    fixed_categories = [
        "Industry",
        "Doctor",
        "Play School",
        "General"
    ]

    result = []

    for category in fixed_categories:

        count = len(
            RepositoryFile.get_by_category(category)
        )

        result.append({
            "category": category,
            "files_count": count
        })

    return jsonify(result)


@user_bp.route('/user/category-page/<category>')
@require_login
def category_page(category):

    return render_template(
        'user/category_files.html',
        category=category
    )


@user_bp.route('/user/category/<category>')
@require_login
def get_category_files(category):

    files = RepositoryFile.get_by_category(category)

    result = []

    for file in files:

        result.append({
            "id": str(file["_id"]),
            "file_name": file.get("filename", ""),
            "status": file.get("status", "Available"),
            "allocated_to": file.get("allocated_to", ""),
            "download_count": file.get("download_count", 0),
            "category": file.get("category", "General")
        })

    return jsonify(result)


# ==========================
# ALLOCATE FILE
# ==========================

@user_bp.route('/user/allocate-file/<file_id>', methods=['POST'])
@require_login
def allocate_file(file_id):

    username = session['username']

    db = MongoDB.get_db()

    # Check if user already has an allocated file
    existing_file = db[Collections.REPOSITORY_FILES].find_one({
        "allocated_to": username
    })

    if existing_file:
        return jsonify({
            "success": False,
            "message": f"You already have an allocated file: {existing_file.get('filename')}"
        }), 400

    file = RepositoryFile.get_by_id(file_id)

    if not file:
        return jsonify({
            "success": False,
            "message": "File not found"
        }), 404

    if file.get("allocated_to"):
        return jsonify({
            "success": False,
            "message": f"File already allocated to {file.get('allocated_to')}"
        }), 400

    db[Collections.REPOSITORY_FILES].update_one(
        {
            "_id": ObjectId(file_id)
        },
        {
            "$set": {
                "allocated_to": username,
                "status": "Allocated"
            }
        }
    )

    return jsonify({
        "success": True,
        "message": "File allocated successfully"
    })


# ==========================
# UNALLOCATE FILE
# ==========================

@user_bp.route('/user/unallocate-file/<file_id>', methods=['POST'])
@require_login
def unallocate_file(file_id):

    username = session['username']

    file = RepositoryFile.get_by_id(file_id)

    if not file:
        return jsonify({
            "success": False,
            "message": "File not found"
        }), 404

    if file.get("allocated_to") != username:
        return jsonify({
            "success": False,
            "message": f"File is allocated to {file.get('allocated_to')}"
        }), 403

    db = MongoDB.get_db()

    db[Collections.REPOSITORY_FILES].update_one(
        {
            "_id": ObjectId(file_id)
        },
        {
            "$set": {
                "allocated_to": None,
                "status": "Available"
            }
        }
    )

    return jsonify({
        "success": True,
        "message": "File unallocated successfully"
    })


# ==========================
# DOWNLOAD FILE
# ==========================

@user_bp.route('/user/download-file/<file_id>')
@require_login
def download_file(file_id):

    username = session['username']

    file = RepositoryFile.get_by_id(file_id)

    if not file:
        return "File not found", 404

    if file.get("allocated_to") != username:
        return f"File is allocated to {file.get('allocated_to')}", 403

    file_path = file.get("path")

    if not file_path:
        return "Path missing in database", 404

    if not os.path.exists(file_path):
        return f"Missing file: {file_path}", 404

    db = MongoDB.get_db()

    db[Collections.REPOSITORY_FILES].update_one(
        {
            "_id": ObjectId(file_id)
        },
        {
            "$inc": {
                "download_count": 1
            }
        }
    )

    return send_file(
        file_path,
        as_attachment=True
    )