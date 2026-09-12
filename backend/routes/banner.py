"""
Banner with Content Routes Blueprint
Handles Blogger-like block content editor, Pillow image rendering, and isolated email dispatch.
Keeps existing routes/user.py completely untouched.
"""

from flask import Blueprint, render_template, request, jsonify, session, redirect, url_for, send_from_directory
from new_models import BannerModel, NewEmailLogModel
from models import EmailID, ExcelFile
from banner_renderer import render_banner_to_image, BANNER_UPLOAD_FOLDER
from isolated_email_sender import IsolatedEmailSender
from werkzeug.utils import secure_filename
from smtp_validator import validate_email
import os
import uuid
import traceback

banner_bp = Blueprint('banner', __name__)


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


@banner_bp.route('/banner-builder')
@require_login
def banner_page():
    """Banner with Content management and Blogger-like editor page"""
    if session.get('role') == 'admin':
        return redirect(url_for('admin.dashboard'))
    
    user_id = session['user_id']
    email_ids = EmailID.get_by_user(user_id)
    excel_files = ExcelFile.get_by_user(user_id)
    
    return render_template('user/banner.html',
                           username=session['username'],
                           email_ids=email_ids,
                           excel_files=excel_files)


@banner_bp.route('/uploads/banners/<path:filename>')
def serve_banner_file(filename):
    """Serve uploaded or rendered banner files"""
    return send_from_directory(BANNER_UPLOAD_FOLDER, filename)


@banner_bp.route('/api/banners', methods=['GET'])
@require_login
def get_banners():
    """List all saved banners for user"""
    items = BannerModel.get_by_user(session['user_id'])
    return jsonify({'success': True, 'banners': items})


@banner_bp.route('/api/banners', methods=['POST'])
@require_login
def save_banner():
    """Save or update banner structured content JSON"""
    data = request.get_json(silent=True) or {}
    title = str(data.get('title', '')).strip()
    blocks = data.get('blocks', [])
    bg_color = str(data.get('bgColor', '#ffffff')).strip()
    width = int(data.get('width', 650))
    banner_id = data.get('id')

    if not title:
        return jsonify({'success': False, 'error': 'Banner campaign title is required'}), 400
    if not blocks or not isinstance(blocks, list):
        return jsonify({'success': False, 'error': 'Banner must contain at least one content block'}), 400

    user_id = session['user_id']
    if banner_id and str(banner_id).strip():
        updated = BannerModel.update(banner_id, user_id, title, blocks, bg_color, width)
        if updated:
            return jsonify({'success': True, 'message': 'Banner design updated successfully', 'id': banner_id})
        return jsonify({'success': False, 'error': 'Failed to update banner design'}), 400
    else:
        created = BannerModel.create(user_id, title, blocks, bg_color, width)
        if created:
            return jsonify({'success': True, 'message': 'Banner design saved successfully', 'id': created['_id']})
        return jsonify({'success': False, 'error': 'Failed to save banner design'}), 400



@banner_bp.route('/api/banners/upload-image', methods=['POST'])
@require_login
def upload_block_image():
    """Upload custom image block file"""
    if 'image' not in request.files:
        return jsonify({'success': False, 'error': 'No image file uploaded'}), 400
    
    file = request.files['image']
    if file.filename == '':
        return jsonify({'success': False, 'error': 'No image selected'}), 400

    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ['.jpg', '.jpeg', '.png', '.gif', '.webp']:
        return jsonify({'success': False, 'error': 'Invalid image type. Allowed: JPG, PNG, GIF, WEBP'}), 400

    os.makedirs(BANNER_UPLOAD_FOLDER, exist_ok=True)
    filename = f"block_img_{uuid.uuid4().hex[:10]}{ext}"
    filepath = os.path.join(BANNER_UPLOAD_FOLDER, filename)
    file.save(filepath)

    return jsonify({
        'success': True,
        'url': f"/uploads/banners/{filename}",
        'filepath': filepath
    })


@banner_bp.route('/api/banners/render-image', methods=['POST'])
@require_login
def render_banner_image_api():
    """Render structured content blocks into a single composite high-quality PNG image"""
    try:
        data = request.get_json(silent=True) or {}
        title = data.get('title', 'Email Banner')
        blocks = data.get('blocks', [])
        bg_color = data.get('bgColor', '#ffffff')
        width = int(data.get('width', 650))
        banner_id = data.get('id')

        if not blocks:
            return jsonify({'success': False, 'error': 'No content blocks provided'}), 400

        print(f"[BANNER ROUTE DEBUG] Received /api/banners/render-image request for '{title}' with {len(blocks)} blocks:")
        for idx, b in enumerate(blocks, 1):
            print(f"  [BLOCK #{idx}] id={b.get('id')} | type={b.get('type')} | layout={b.get('layout')} | align={b.get('align')} | source={b.get('source')} | content={b.get('content')}")

        render_input = {
            'title': title,
            'bgColor': bg_color,
            'width': width,
            'blocks': blocks
        }

        output_filename = f"banner_{uuid.uuid4().hex[:10]}.png"
        render_res = render_banner_to_image(render_input, output_filename)

        if banner_id and str(banner_id).strip():
            BannerModel.update(banner_id, session['user_id'], title, blocks, bg_color, width, rendered_image_url=render_res['url'])

        return jsonify({
            'success': True,
            'image_url': render_res['url'],
            'filepath': render_res['filepath'],
            'width': render_res['width'],
            'height': render_res['height']
        })

    except Exception as e:
        tb = traceback.format_exc()
        print("[BANNER RENDER API ERROR]:", tb)
        return jsonify({'success': False, 'error': str(e)}), 500


@banner_bp.route('/api/banners/validate-recipients', methods=['POST'])
@require_login
def validate_banner_recipients():
    """Isolated recipient validation for Banner Builder"""
    try:
        recipient_list = []

        # Check if direct file upload is present
        if 'file' in request.files or 'recipient_file' in request.files:
            file = request.files.get('file') or request.files.get('recipient_file')
            if file and file.filename != '':
                filename = file.filename.lower()
                import pandas as pd
                import io

                file_bytes = file.read()
                if filename.endswith('.csv'):
                    df = pd.read_csv(io.BytesIO(file_bytes))
                else:
                    df = pd.read_excel(io.BytesIO(file_bytes))

                possible_cols = ['email', 'email address', 'email_address', 'e-mail', 'recipient', 'recipients', 'mail']
                matched_col = None
                for col in df.columns:
                    clean_c = str(col).strip().lower()
                    if clean_c in possible_cols:
                        matched_col = col
                        break

                if not matched_col:
                    matched_col = df.columns[0]

                for _, row in df.iterrows():
                    val = str(row[matched_col] or '').strip()
                    if val and val.lower() != 'nan':
                        recipient_list.append({'email': val, 'name': ''})

        if not recipient_list:
            data = request.get_json(silent=True) or {}
            file_id = data.get('file_id')
            raw_recipients = data.get('recipients', [])

            if file_id:
                excel_file = ExcelFile.get_by_id(file_id)
                if not excel_file:
                    return jsonify({'success': False, 'error': 'Recipient file not found', 'recipients': []}), 404
                recipient_list = excel_file.get('recipients', [])
            elif raw_recipients and isinstance(raw_recipients, list):
                recipient_list = raw_recipients

        if not recipient_list:
            return jsonify({'success': False, 'error': 'No recipients or recipient file provided', 'recipients': []}), 400

        validated_recipients = []
        seen_emails = set()
        valid_count = 0
        invalid_count = 0
        unknown_count = 0

        for r in recipient_list:
            if isinstance(r, dict):
                email = str(r.get('email', '') or '').strip()
                name = str(r.get('name', '') or '').strip()
            elif isinstance(r, str):
                email = str(r or '').strip()
                name = ''
            else:
                continue

            if not email or email.lower() == 'nan':
                continue

            clean_email = email.lower()
            if clean_email in seen_emails:
                continue
            seen_emails.add(clean_email)

            is_valid, reason, status_str = validate_email(email)
            if status_str == 'VALID':
                valid_count += 1
            elif status_str == 'UNKNOWN':
                unknown_count += 1
            else:
                invalid_count += 1

            validated_recipients.append({
                'email': email,
                'name': name,
                'status': status_str,
                'reason': reason or ('Valid email syntax and mail domain' if is_valid else 'Invalid email syntax')
            })

        total = len(validated_recipients)
        rate = round((valid_count / total * 100), 1) if total > 0 else 0.0

        return jsonify({
            'success': True,
            'total': total,
            'valid': valid_count,
            'invalid': invalid_count,
            'unknown': unknown_count,
            'rate': rate,
            'total_count': total,
            'valid_count': valid_count,
            'invalid_count': invalid_count,
            'unknown_count': unknown_count,
            'validation_rate': rate,
            'recipients': validated_recipients,
            'valid_recipients': [r for r in validated_recipients if r['status'] in ('VALID', 'UNKNOWN')]
        })

    except Exception as e:
        tb = traceback.format_exc()
        print("[BANNER VALIDATE ROUTE ERROR]:", tb)
        return jsonify({'success': False, 'error': str(e), 'recipients': [], 'total': 0, 'valid': 0, 'invalid': 0, 'unknown': 0, 'rate': 0}), 500


@banner_bp.route('/api/banners/send', methods=['POST'])
@require_login
def send_banner_email():
    """Send banner email containing the single rendered image to valid recipients"""
    try:
        data = request.get_json(silent=True) or {}
        sender_email_id = str(data.get('sender_email_id', '') or '').strip()
        from_name = str(data.get('from_name') or session.get('username', 'Marketing Team')).strip()
        subject = str(data.get('subject', '') or '').strip()
        blocks = data.get('blocks', [])
        image_url = str(data.get('image_url', '') or '').strip()
        recipients = data.get('recipients', [])
        cc_emails = data.get('cc_emails', [])

        if not recipients:
            return jsonify({'success': False, 'error': 'No recipients selected'}), 400
        if not sender_email_id:
            return jsonify({'success': False, 'error': 'Please select a sender email account'}), 400
        if not subject:
            return jsonify({'success': False, 'error': 'Subject is required'}), 400

        user_id = session['user_id']

        # Ensure rendered image exists
        if not image_url or not os.path.exists(os.path.join(BANNER_UPLOAD_FOLDER, os.path.basename(image_url))):
            if blocks:
                render_res = render_banner_to_image({
                    'title': subject,
                    'blocks': blocks,
                    'bgColor': data.get('bgColor', '#ffffff'),
                    'width': int(data.get('width', 650))
                })
                image_url = render_res['url']

        filename = os.path.basename(image_url)
        full_img_path = os.path.join(BANNER_UPLOAD_FOLDER, filename)

        if not os.path.exists(full_img_path):
            return jsonify({'success': False, 'error': 'Rendered banner image not found on server'}), 400

        # Retrieve sender account info
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

        # Construct single final image HTML body with CID attachment
        html_body = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
body {{ font-family: 'Segoe UI', Arial, sans-serif; background-color: #f4f6f8; margin: 0; padding: 20px; text-align: center; }}
.container {{ max-width: 680px; margin: 0 auto; background: #ffffff; padding: 20px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.08); }}
.banner-img {{ max-width: 100%; height: auto; display: block; margin: 0 auto; border: 0; border-radius: 4px; }}
.footer {{ margin-top: 20px; font-size: 12px; color: #888888; }}
</style>
</head>
<body>
<div class="container">
<img src="cid:banner_render_image" alt="Email Banner" class="banner-img" />
<div class="footer">Sent via Marketing Campaign System</div>
</div>
</body>
</html>"""

        inline_images = {
            'banner_render_image': full_img_path
        }

        # Server-side Recipient Validation & Filtering (Only VALID & UNKNOWN pass through, INVALID rejected)
        validated_recipients = []
        seen_emails = set()
        invalid_count = 0

        for r in recipients:
            if isinstance(r, dict):
                email = str(r.get('email', '')).strip()
            elif isinstance(r, str):
                email = r.strip()
            else:
                continue

            if not email or '@' not in email:
                invalid_count += 1
                continue

            clean_email = email.lower()
            if clean_email in seen_emails:
                continue
            seen_emails.add(clean_email)

            is_valid, reason, status_str = validate_email(email)
            if status_str in ('VALID', 'UNKNOWN'):
                validated_recipients.append({
                    'email': email,
                    'body': html_body,
                    'subject': subject
                })
            else:
                invalid_count += 1

        if not validated_recipients:
            return jsonify({
                'success': False,
                'error': 'No valid recipient email addresses found after validation',
                'total_recipients': len(recipients),
                'valid_recipients': 0,
                'invalid_recipients': invalid_count
            }), 400

        # Initialize Isolated Email Sender
        sender = IsolatedEmailSender(email_accounts, batch_size=25, user_id=user_id, feature_tag='banner')
        sender.current_account_index = start_index

        result = sender.send_bulk_emails(
            recipients=validated_recipients,
            subject=subject,
            body=html_body,
            from_name=from_name,
            cc_emails=cc_emails,
            is_html=True,
            inline_images=inline_images
        )

        sent_entries = result.get('sent_entries', [])
        failed_entries = result.get('failed', [])

        # Record independent logs in new_email_logs
        for item in sent_entries:
            NewEmailLogModel.create(
                user_id=user_id,
                feature_type='banner',
                sender_email_id=item.get('sender_email_id') or sender_email_id,
                recipient=item['email'],
                subject=subject,
                status='sent'
            )

        for item in failed_entries:
            NewEmailLogModel.create(
                user_id=user_id,
                feature_type='banner',
                sender_email_id=item.get('sender_email_id') or sender_email_id,
                recipient=item.get('email', ''),
                subject=subject,
                status='failed',
                error=item.get('error', 'Send failed')
            )

        return jsonify({
            'success': True,
            'message': 'Banner email campaign dispatched successfully',
            'total_recipients': len(recipients),
            'valid_recipients': len(validated_recipients),
            'invalid_recipients': invalid_count,
            'sent': len(sent_entries),
            'failed': len(failed_entries),
            'failed_list': failed_entries,
            'image_url': image_url
        })

    except Exception as e:
        tb = traceback.format_exc()
        print("[BANNER SEND ROUTE ERROR]:", tb)
        return jsonify({'success': False, 'error': str(e)}), 500


@banner_bp.route('/api/banners/<banner_id>', methods=['GET'])
@require_login
def get_banner_detail(banner_id):
    """Get single banner design details"""
    item = BannerModel.get_by_id(banner_id)
    if not item:
        return jsonify({'success': False, 'error': 'Banner design not found'}), 404
    return jsonify({'success': True, 'banner': item})


@banner_bp.route('/api/banners/<banner_id>', methods=['DELETE'])
@require_login
def delete_banner(banner_id):
    """Delete saved banner design"""
    deleted = BannerModel.delete(banner_id, session['user_id'])
    if deleted:
        return jsonify({'success': True, 'message': 'Banner design deleted'})
    return jsonify({'success': False, 'error': 'Failed to delete banner design'}), 400
