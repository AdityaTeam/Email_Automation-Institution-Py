"""
Admin Panel Routes
Handles admin dashboard, user management, and system control
"""

from flask import Blueprint, render_template, request, jsonify, session, redirect, url_for
from models import User, EmailID, ExcelFile, Template, Requirement, EmailLog
from database import MongoDB, Collections
from bson import ObjectId
import os
from werkzeug.utils import secure_filename

admin_bp = Blueprint('admin', __name__)


def require_admin(f):
    from functools import wraps
    from flask import request, jsonify

    @wraps(f)
    def decorated_function(*args, **kwargs):
        # If API request → return JSON error
        if request.path.startswith('/api/'):
            if 'user_id' not in session:
                return jsonify({'error': 'Unauthorized'}), 401
            if session.get('role') != 'admin':
                return jsonify({'error': 'Forbidden'}), 403
        else:
            # Normal page routes
            if 'user_id' not in session:
                return redirect(url_for('auth.login'))
            if session.get('role') != 'admin':
                return redirect(url_for('user.dashboard'))

        return f(*args, **kwargs)

    return decorated_function


@admin_bp.route('/admin')
@require_admin
def dashboard():
    """Admin dashboard"""
    # Get system stats
    users = User.get_all()
    total_users = len(users)
    
    all_email_ids = []
    for user in users:
        user_email_ids = EmailID.get_by_user(str(user['_id']))
        all_email_ids.extend(user_email_ids)
    
    total_email_ids = len(all_email_ids)
    total_excel_files = sum(len(ExcelFile.get_by_user(str(u['_id']))) for u in users)
    stats = EmailLog.get_stats()
    
    # Get recent logs
    recent_logs = EmailLog.get_all(limit=20)
    
    # Get users with basic info
    user_list = []
    for user in users:
        user_stats = EmailLog.get_stats(str(user['_id']))
        user_email_count = len(EmailID.get_by_user(str(user['_id'])))
        user_files = ExcelFile.get_by_user(str(user['_id']))
        
        user_list.append({
            '_id': str(user['_id']),
            'username': user['username'],
            'created_at': user.get('created_at'),
            'is_active': user.get('is_active', True),
            'email_ids_count': user_email_count,
            'files_count': len(user_files),
            'emails_sent': user_stats['sent']
        })
    
    return render_template('admin/dashboard.html',
                           username=session['username'],
                           total_users=total_users,
                           total_email_ids=total_email_ids,
                           total_excel_files=total_excel_files,
                           stats=stats,
                           recent_logs=recent_logs,
                           users=user_list)


@admin_bp.route('/admin/users')
@require_admin
def users():
    """User management page"""
    users_list = User.get_all()
    
    user_data = []
    for user in users_list:
        user_stats = EmailLog.get_stats(str(user['_id']))
        user_email_ids = EmailID.get_by_user(str(user['_id']))
        user_files = ExcelFile.get_by_user(str(user['_id']))
        
        user_data.append({
            '_id': str(user['_id']),
            'username': user['username'],
            'created_at': user.get('created_at'),
            'is_active': user.get('is_active', True),
            'email_ids_count': len(user_email_ids),
            'files_count': len(user_files),
            'emails_sent': user_stats['sent'],
            'emails_failed': user_stats['failed']
        })
    
    return render_template('admin/users.html',
                           username=session['username'],
                           users=user_data)


@admin_bp.route('/admin/user/<user_id>')
@require_admin
def view_user(user_id):
    """View user details with passwords"""
    user = User.get_by_id(user_id)
    if not user:
        return "User not found", 404
    
    # Get email IDs with decrypted passwords (admin only)
    email_ids = EmailID.get_by_user_with_passwords(user_id)
    excel_files = ExcelFile.get_by_user(user_id)
    logs = EmailLog.get_by_user(user_id, limit=50)
    stats = EmailLog.get_stats(user_id)
    
    # Convert ObjectIds to strings for template
    for eid in email_ids:
        eid['_id'] = str(eid['_id'])
    for file in excel_files:
        file['_id'] = str(file['_id'])
    for log in logs:
        log['_id'] = str(log['_id'])
        log['user_id'] = str(log['user_id'])
        log['sender_email_id'] = str(log.get('sender_email_id', ''))
    
    # Get sender emails for logs
    email_id_map = {str(eid['_id']): eid['email'] for eid in email_ids}
    for log in logs:
        log['sender_email'] = email_id_map.get(str(log.get('sender_email_id')), 'Unknown')
    
    return render_template('admin/user_detail.html',
                           username=session['username'],
                           view_user={
                               '_id': str(user['_id']),
                               'username': user['username'],
                               'password': user['password'],  # bcrypt hash (not decrypted)
                               'created_at': user.get('created_at'),
                               'is_active': user.get('is_active', True)
                           },
                           email_ids=email_ids,
                           excel_files=excel_files,
                           logs=logs,
                           stats=stats)


@admin_bp.route('/admin/users/<user_id>', methods=['DELETE'])
@require_admin
def delete_user(user_id):
    """Delete a user"""
    if User.delete(user_id):
        return jsonify({'success': True})
    return jsonify({'error': 'Failed to delete user'}), 400


# ==================== Password Management ====================

@admin_bp.route('/admin/users/<user_id>/reset-password', methods=['POST'])
@require_admin
def reset_user_password(user_id):
    """Reset a user's password"""
    data = request.json
    new_password = data.get('new_password', '').strip()
    
    if not new_password or len(new_password) < 4:
        return jsonify({'error': 'Password must be at least 4 characters'}), 400
    
    if User.reset_password(user_id, new_password):
        return jsonify({'success': True, 'message': 'Password reset successfully'})
    return jsonify({'error': 'Failed to reset password'}), 400


@admin_bp.route('/admin/email-ids/<email_id>/reset-password', methods=['POST'])
@require_admin
def reset_email_password(email_id):
    """Reset an email ID's password"""
    data = request.json
    new_password = data.get('new_password', '').strip()
    
    if not new_password:
        return jsonify({'error': 'Password is required'}), 400
    
    if EmailID.update_password(email_id, new_password):
        return jsonify({'success': True, 'message': 'Email password updated successfully'})
    return jsonify({'error': 'Failed to update password'}), 400


# ==================== Template Management ====================

@admin_bp.route('/admin/templates')
@require_admin
def templates():
    requirements = Requirement.get_all()
    templates_list = Template.get_all()
    
    # Convert ObjectIds
    for r in requirements:
        r['_id'] = str(r['_id'])
    
    for t in templates_list:
        t['_id'] = str(t['_id'])
        t['requirement_id'] = str(t['requirement_id'])
    
    req_map = {str(r['_id']): r['name'] for r in requirements}
    
    for t in templates_list:
        t['requirement_name'] = req_map.get(str(t.get('requirement_id')), 'Unknown')
    
    return render_template(
        'admin/templates.html',
        username=session['username'],
        requirements=requirements,
        templates=templates_list
    )


@admin_bp.route('/api/admin/requirements', methods=['GET'])
@require_admin
def get_requirements():
    """Get all requirements"""
    requirements = Requirement.get_all()
    for r in requirements:
        r['_id'] = str(r['_id'])
    return jsonify({'requirements': requirements})


@admin_bp.route('/api/admin/requirements', methods=['POST'])
@require_admin
def add_requirement():
    """Add a new requirement"""
    data = request.json
    name = data.get('name', '').strip()
    
    if not name:
        return jsonify({'error': 'Name is required'}), 400
    
    req = Requirement.create(name)
    if req:
        return jsonify({'success': True, 'requirement_id': str(req['_id'])})
    return jsonify({'error': 'Failed to create requirement'}), 400


@admin_bp.route('/api/admin/requirements/<req_id>', methods=['PUT'])
@require_admin
def update_requirement(req_id):
    """Update a requirement"""
    data = request.json
    name = data.get('name', '').strip()
    
    if not name:
        return jsonify({'error': 'Name is required'}), 400
    
    if Requirement.update(req_id, name):
        return jsonify({'success': True})
    return jsonify({'error': 'Failed to update requirement'}), 400


@admin_bp.route('/api/admin/requirements/<req_id>', methods=['DELETE'])
@require_admin
def delete_requirement(req_id):
    """Delete a requirement"""
    if Requirement.delete(req_id):
        return jsonify({'success': True})
    return jsonify({'error': 'Failed to delete requirement'}), 400


@admin_bp.route('/api/admin/templates', methods=['GET'])
@require_admin
def get_all_templates():
    """Get all templates"""
    templates_list = Template.get_all()
    requirements = Requirement.get_all()
    
    req_map = {str(r['_id']): r['name'] for r in requirements}
    
    for t in templates_list:
        t['_id'] = str(t['_id'])
        t['requirement_id'] = str(t['requirement_id'])
        t['requirement_name'] = req_map.get(str(t.get('requirement_id')), 'Unknown')
    
    return jsonify({'templates': templates_list})


@admin_bp.route('/api/admin/templates', methods=['POST'])
@require_admin
def add_template():
    """Add a new template"""
    data = request.form
    files = request.files.getlist('attachments')
    
    requirement_id = data.get('requirement_id')
    name = data.get('name', '').strip()
    subject = data.get('subject', '').strip()
    body = data.get('body', '').strip()
    
    if not all([requirement_id, name, subject, body]):
        return jsonify({'error': 'All fields are required'}), 400
    
    # Handle attachments
    attachments = []
    ALLOWED_EXTENSIONS = {'pdf', 'docx', 'xlsx', 'pptx', 'png', 'jpg', 'jpeg', 'zip'}
    upload_dir = 'uploads/template_attachments'
    os.makedirs(upload_dir, exist_ok=True)
    
    for file in files:
        if file and file.filename:
            filename = secure_filename(file.filename)
            if '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS:
                filepath = os.path.join(upload_dir, filename)
                file.save(filepath)
                attachments.append(f"uploads/template_attachments/{filename}")
            else:
                return jsonify({'error': f'Invalid file type: {file.filename}'}), 400
    
    template_data = {
        'requirement_id': requirement_id,
        'name': name,
        'subject': subject,
        'body': body,
        'attachments': attachments
    }
    
    template = Template.create(template_data)
    if template:
        return jsonify({'success': True, 'template_id': str(template['_id']), 'attachments': attachments})
    return jsonify({'error': 'Failed to create template'}), 400


@admin_bp.route('/api/admin/templates/<template_id>', methods=['PUT'])
@require_admin
def update_template(template_id):
    """Update a template"""
    data = request.form
    files = request.files.getlist('attachments')
    
    template_data = {
        'name': data.get('name', '').strip(),
        'subject': data.get('subject', '').strip(),
        'body': data.get('body', '').strip()
    }
    
    if not all([template_data['name'], template_data['subject'], template_data['body']]):
        return jsonify({'error': 'All fields are required'}), 400
    
    # Handle attachments (replace existing)
    attachments = []
    ALLOWED_EXTENSIONS = {'pdf', 'docx', 'xlsx', 'pptx','png', 'jpg', 'jpeg', 'zip'}
    upload_dir = 'uploads/template_attachments'
    os.makedirs(upload_dir, exist_ok=True)
    
    for file in files:
        if file and file.filename:
            filename = secure_filename(file.filename)
            if '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS:
                filepath = os.path.join(upload_dir, filename)
                file.save(filepath)
                attachments.append(f"uploads/template_attachments/{filename}")
            else:
                return jsonify({'error': f'Invalid file type: {file.filename}'}), 400
    
    template_data['attachments'] = attachments
    
    if Template.update(template_id, template_data):
        return jsonify({'success': True, 'attachments': attachments})
    return jsonify({'error': 'Failed to update template'}), 400


@admin_bp.route('/api/admin/templates/<template_id>', methods=['DELETE'])
@require_admin
def delete_template(template_id):
    """Delete a template"""
    if Template.delete(template_id):
        return jsonify({'success': True})
    return jsonify({'error': 'Failed to delete template'}), 400


# ==================== Logs ====================

@admin_bp.route('/admin/logs')
@require_admin
def logs():
    """View all email logs"""
    logs_list = EmailLog.get_all(limit=100)
    
    # Get all users for mapping
    users = User.get_all()
    user_map = {str(u['_id']): u['username'] for u in users}
    
    # Get all email IDs
    db = MongoDB.get_db()
    all_email_ids = {}
    if db is not None:
        for user in users:
            email_ids = EmailID.get_by_user(str(user['_id']))
            for eid in email_ids:
                all_email_ids[str(eid['_id'])] = eid['email']
    
    for log in logs_list:
        log['_id'] = str(log['_id'])
        log['user_id'] = str(log['user_id'])
        log['username'] = user_map.get(log['user_id'], 'Unknown')
        log['sender_email_id'] = str(log.get('sender_email_id', ''))
        log['sender_email'] = all_email_ids.get(str(log.get('sender_email_id')), 'Unknown')
    
    return render_template('admin/logs.html',
                           username=session['username'],
                           logs=logs_list)


@admin_bp.route('/api/admin/logs', methods=['GET'])
@require_admin
def get_all_logs():
    """Get paginated email logs (FEATURE 2)"""
    page = int(request.args.get('page', 1))
    limit = int(request.args.get('limit', 100))
    logs_list = EmailLog.get_all_paginated(page, limit)
    
    total_count = EmailLog.get_count()
    
    users = User.get_all()
    user_map = {str(u['_id']): u['username'] for u in users}
    
    all_email_ids = {}
    for user in users:
        email_ids = EmailID.get_by_user(str(user['_id']))
        for eid in email_ids:
            all_email_ids[str(eid['_id'])] = eid['email']
    
    for log in logs_list:
        log['_id'] = str(log['_id'])
        log['user_id'] = str(log['user_id'])
        log['username'] = user_map.get(log['user_id'], 'Unknown')
        log['sender_email_id'] = str(log.get('sender_email_id', ''))
        log['sender_email'] = all_email_ids.get(str(log.get('sender_email_id')), 'Unknown')
        # Ensure sent_at is a string
        if 'sent_at' in log and log['sent_at']:
            log['sent_at'] = log['sent_at'].isoformat() if hasattr(log['sent_at'], 'isoformat') else str(log['sent_at'])
    
    return jsonify({
        'logs': logs_list, 
        'total': total_count,
        'page': page, 
        'limit': limit,
        'total_pages': (total_count + limit - 1) // limit
    })


# ==================== Statistics ====================

@admin_bp.route('/api/admin/cc-emails', methods=['GET'])
@require_admin
def get_cc_emails():
    print("🔍 DEBUG API: /api/admin/cc-emails GET called")
    from models import CcEmail
    
    cc_emails = CcEmail.get_all()
    print(f"🔍 DEBUG API: CcEmail.get_all() returned {len(cc_emails)} items")
    print(f"🔍 DEBUG API: First item: {cc_emails[0] if cc_emails else 'NONE'}")
    
    safe_data = []

    
    for cc in cc_emails:
        safe_data.append({
            "_id": str(cc.get("_id")),
            "email": cc.get("email"),
            "created_at": str(cc.get("created_at")) if cc.get("created_at") else None
        })
    
    return jsonify({'cc_emails': safe_data})


@admin_bp.route('/api/admin/cc-emails', methods=['POST'])
@require_admin
def add_cc_email():
    """Add CC email"""
    data = request.json
    email = data.get('email', '').strip()
    if not email or '@' not in email:
        return jsonify({'error': 'Valid email required'}), 400
    
    from models import CcEmail
    cc = CcEmail.create(email)
    if cc:
        cc['_id'] = str(cc['_id'])
        if 'created_at' in cc and cc['created_at']:
            cc['created_at'] = cc['created_at'].isoformat() if hasattr(cc['created_at'], 'isoformat') else str(cc['created_at'])
        return jsonify({'success': True, 'cc_email': cc})
    return jsonify({'error': 'Failed to add'}), 500


@admin_bp.route('/api/admin/cc-emails/<cc_id>', methods=['DELETE'])
@require_admin
def delete_cc_email(cc_id):
    """Delete CC email"""
    from models import CcEmail
    if CcEmail.delete(cc_id):
        return jsonify({'success': True})
    return jsonify({'error': 'Failed to delete'}), 400


@admin_bp.route('/admin/cc')
@require_admin
def cc_management():
    """CC Emails and Logo management page"""
    return render_template('admin/cc.html', username=session['username'])


# @admin_bp.route('/api/admin/logo', methods=['GET'])
# @require_admin
# def get_logo_status():
#     """Get current logo status"""
#     import os
#     logo_path = 'backend/uploads/logo/company_logo.jpeg'
#     logo_exists = os.path.exists(logo_path)
#     return jsonify({
#         'logo_exists': logo_exists,
#         'logo_path': logo_path
#     })


@admin_bp.route('/api/admin/logo-upload', methods=['POST'])
@require_admin
def upload_logo():
    """Upload company logo for email signature"""
    if 'logo' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400
    
    file = request.files['logo']
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400
    
    from werkzeug.utils import secure_filename
    filename = secure_filename(file.filename)
    if not filename.lower().endswith(('.png', '.jpg', '.jpeg')):
        return jsonify({'error': 'Only PNG/JPG allowed'}), 400
    
    logo_dir = 'backend/uploads/logo'
    os.makedirs(logo_dir, exist_ok=True)
    logo_path = os.path.join(logo_dir, 'company_logo.jpeg')
    
    file.save(logo_path)
    return jsonify({'success': True, 'message': 'Logo uploaded successfully'})


@admin_bp.route('/api/admin/stats')
@require_admin
def get_stats():
    """Get system statistics"""
    users = User.get_all()
    
    total_users = len(users)
    total_email_ids = sum(len(EmailID.get_by_user(str(u['_id']))) for u in users)
    total_excel_files = sum(len(ExcelFile.get_by_user(str(u['_id']))) for u in users)
    stats = EmailLog.get_stats()
    
    return jsonify({
        'total_users': total_users,
        'total_email_ids': total_email_ids,
        'total_excel_files': total_excel_files,
        'emails_sent': stats['sent'],
        'emails_failed': stats['failed']
    })


