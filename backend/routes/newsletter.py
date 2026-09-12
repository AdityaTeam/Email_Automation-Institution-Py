"""
Newsletter Feature Routes Blueprint
Handles creating, editing, previewing, and sending newsletters using IsolatedEmailSender.
Keeps existing routes/user.py completely untouched.
"""

from flask import Blueprint, render_template, request, jsonify, session, redirect, url_for
from new_models import NewsletterModel, NewEmailLogModel
from models import EmailID, ExcelFile
from isolated_email_sender import IsolatedEmailSender
from database import MongoDB
from bson import ObjectId
import json
import traceback

newsletter_bp = Blueprint('newsletter', __name__)


def require_login(f):
    from functools import wraps
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            if request.path.startswith('/api/') or request.is_json or request.accept_mimetypes.accept_json:
                return jsonify({'success': False, 'error': 'Session expired or not logged in. Please log in again.'}), 401
            return redirect(url_for('auth.login'))
        return f(*args, **kwargs)
    return decorated_function


@newsletter_bp.route('/newsletter')
@require_login
def newsletter_page():
    """Newsletter management and creation page"""
    if session.get('role') == 'admin':
        return redirect(url_for('admin.dashboard'))
    
    user_id = session['user_id']
    email_ids = EmailID.get_by_user(user_id)
    excel_files = ExcelFile.get_by_user(user_id)
    
    return render_template('user/newsletter.html',
                           username=session['username'],
                           email_ids=email_ids,
                           excel_files=excel_files)


@newsletter_bp.route('/api/newsletters', methods=['GET'])
@require_login
def get_newsletters():
    """List all saved newsletters for user"""
    items = NewsletterModel.get_by_user(session['user_id'])
    return jsonify({'success': True, 'newsletters': items})


@newsletter_bp.route('/api/newsletters', methods=['POST'])
@require_login
def save_newsletter():
    """Create or update newsletter"""
    data = request.get_json(silent=True) or {}
    title = str(data.get('title', '')).strip()
    subject = str(data.get('subject', '')).strip()
    content = str(data.get('content', '')).strip()
    blocks = data.get('blocks', [])
    newsletter_id = data.get('id')

    if not title:
        return jsonify({'success': False, 'error': 'Newsletter title is required'}), 400
    if not subject:
        return jsonify({'success': False, 'error': 'Newsletter subject is required'}), 400
    if not content and not blocks:
        return jsonify({'success': False, 'error': 'Newsletter content cannot be empty'}), 400

    user_id = session['user_id']
    if newsletter_id and str(newsletter_id).strip():
        updated = NewsletterModel.update(newsletter_id, user_id, title, subject, content, blocks)
        if updated:
            return jsonify({'success': True, 'message': 'Newsletter updated successfully', 'id': newsletter_id})
        return jsonify({'success': False, 'error': 'Failed to update newsletter'}), 400
    else:
        created = NewsletterModel.create(user_id, title, subject, content, blocks)
        if created:
            return jsonify({'success': True, 'message': 'Newsletter created successfully', 'id': created['_id']})
        return jsonify({'success': False, 'error': 'Failed to create newsletter'}), 400


@newsletter_bp.route('/api/newsletters/<newsletter_id>', methods=['GET'])
@require_login
def get_newsletter_detail(newsletter_id):
    """Get single newsletter details"""
    item = NewsletterModel.get_by_id(newsletter_id)
    if not item:
        return jsonify({'success': False, 'error': 'Newsletter not found'}), 404
    return jsonify({'success': True, 'newsletter': item})


@newsletter_bp.route('/api/newsletters/<newsletter_id>', methods=['DELETE'])
@require_login
def delete_newsletter(newsletter_id):
    """Delete a newsletter"""
    deleted = NewsletterModel.delete(newsletter_id, session['user_id'])
    if deleted:
        return jsonify({'success': True, 'message': 'Newsletter deleted'})
    return jsonify({'success': False, 'error': 'Failed to delete newsletter'}), 400


@newsletter_bp.route('/api/newsletters/send', methods=['POST'])
@require_login
def send_newsletter_email():
    """Send newsletter via isolated email sender pipeline"""
    try:
        data = request.get_json(silent=True) or {}
        sender_email_id = str(data.get('sender_email_id', '') or '').strip()
        from_name = str(data.get('from_name') or session.get('username', 'Marketing Sender')).strip()
        subject = str(data.get('subject', '') or '').strip()
        content = str(data.get('content', '') or '').strip()
        recipients = data.get('recipients', [])
        cc_emails = data.get('cc_emails', [])

        if not recipients:
            return jsonify({'success': False, 'error': 'No recipients selected'}), 400
        if not sender_email_id:
            return jsonify({'success': False, 'error': 'Please select a sender email account'}), 400
        if not subject or not content:
            return jsonify({'success': False, 'error': 'Subject and content are required'}), 400

        # Retrieve sender account info
        user_id = session['user_id']
        user_email_ids = EmailID.get_by_user_with_passwords(user_id)
        selected_doc = EmailID.get_by_id_with_password(sender_email_id)

        if selected_doc:
            doc_id_str = str(selected_doc.get('_id', ''))
            already_present = any(str(e.get('_id', '')) == doc_id_str for e in user_email_ids)
            if not already_present:
                user_email_ids.insert(0, selected_doc)

        if not user_email_ids:
            return jsonify({'success': False, 'error': 'No active sender accounts found'}), 400

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

        # Render HTML body wrapper for newsletter
        html_body = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
body {{ font-family: 'Segoe UI', Arial, sans-serif; background-color: #f4f6f8; margin: 0; padding: 20px; color: #333; }}
.container {{ max-width: 650px; margin: 0 auto; background: #ffffff; padding: 30px; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.06); }}
.content {{ font-size: 15px; line-height: 1.6; color: #2d3748; }}
.footer {{ margin-top: 30px; padding-top: 15px; border-top: 1px solid #e2e8f0; font-size: 12px; color: #718096; text-align: center; }}
</style>
</head>
<body>
<div class="container">
<div class="content">
{content}
</div>
<div class="footer">
Sent via Corporate Newsletter System
</div>
</div>
</body>
</html>"""

        # Normalize recipients list
        normalized_recipients = []
        for r in recipients:
            if isinstance(r, dict):
                email = str(r.get('email', '')).strip()
                if email and '@' in email:
                    normalized_recipients.append({'email': email, 'body': html_body, 'subject': subject})
            elif isinstance(r, str):
                email = r.strip()
                if email and '@' in email:
                    normalized_recipients.append({'email': email, 'body': html_body, 'subject': subject})

        if not normalized_recipients:
            return jsonify({'success': False, 'error': 'No valid recipient email addresses found'}), 400

        # Initialize Isolated Email Sender
        sender = IsolatedEmailSender(email_accounts, batch_size=25, user_id=user_id, feature_tag='newsletter')
        sender.current_account_index = start_index

        result = sender.send_bulk_emails(
            recipients=normalized_recipients,
            subject=subject,
            body=html_body,
            from_name=from_name,
            cc_emails=cc_emails,
            is_html=True
        )

        sent_entries = result.get('sent_entries', [])
        failed_entries = result.get('failed', [])

        # Record independent logs in new_email_logs
        for item in sent_entries:
            NewEmailLogModel.create(
                user_id=user_id,
                feature_type='newsletter',
                sender_email_id=item.get('sender_email_id') or sender_email_id,
                recipient=item['email'],
                subject=subject,
                status='sent'
            )

        for item in failed_entries:
            NewEmailLogModel.create(
                user_id=user_id,
                feature_type='newsletter',
                sender_email_id=item.get('sender_email_id') or sender_email_id,
                recipient=item.get('email', ''),
                subject=subject,
                status='failed',
                error=item.get('error', 'Send failed')
            )

        return jsonify({
            'success': True,
            'sent': len(sent_entries),
            'failed': len(failed_entries),
            'failed_list': failed_entries
        })

    except Exception as e:
        tb = traceback.format_exc()
        print("[NEWSLETTER ROUTE ERROR]:", tb)
        return jsonify({'success': False, 'error': str(e)}), 500
