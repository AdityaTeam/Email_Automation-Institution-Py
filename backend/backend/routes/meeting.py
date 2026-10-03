"""
Google Meet & Event Management Routes
Allows Admins to create meetings with rich metadata (Topic, Banner, Description)
and provides a View-Only interface for registered Users.
"""

from flask import Blueprint, render_template, request, jsonify, session, redirect, url_for
from datetime import datetime, timezone
import os

# Fallback imports to support execution from any directory context
try:
    from google_meet_service import create_google_meet_space
    from database import get_db, Collections
except ImportError:
    try:
        from backend.google_meet_service import create_google_meet_space
        from backend.database import get_db, Collections
    except ImportError:
        from ..google_meet_service import create_google_meet_space
        from ..database import get_db, Collections

meeting_bp = Blueprint('meeting', __name__)

DEFAULT_ADMIN_HOST = os.getenv("GOOGLE_MEET_HOST_EMAIL", "admin@yourdomain.com")


# ==============================================================================
# ADMIN PAGES & API ROUTES
# ==============================================================================

@meeting_bp.route('/admin/meetings')
def admin_meetings_page():
    if session.get('role') != 'admin' and not session.get('is_admin'):
        return redirect(url_for('auth.login'))
    return render_template('admin/meetings.html', username=session.get('username', 'Admin'))


@meeting_bp.route('/api/admin/meetings/create', methods=['POST'])
def api_admin_create_meeting():
    if session.get('role') != 'admin' and not session.get('is_admin'):
        return jsonify({"error": "Unauthorized: Admin privileges required"}), 403

    payload = request.get_json(silent=True) or {}
    title = (payload.get("title") or "").strip()
    topic = (payload.get("topic") or "").strip()
    description = (payload.get("description") or "").strip()
    banner_url = (payload.get("banner_url") or "").strip()
    scheduled_time = (payload.get("scheduled_time") or "").strip()

    if not title or not topic:
        return jsonify({"error": "Meeting Title and Topic are required fields."}), 400

    # Default tech banner if no URL provided
    if not banner_url:
        banner_url = "https://images.unsplash.com/photo-1517245386807-bb43f82c33c4?w=1200&q=80"

    # 1. Generate Google Meet Space
    res = create_google_meet_space(impersonated_admin_email=DEFAULT_ADMIN_HOST)
    if not res.get("success"):
        return jsonify({"error": f"Google Meet API Error: {res.get('error')}"}), 500

    meeting_uri = res["meeting_uri"]
    meeting_code = res["meeting_code"]

    # 2. Save meeting details to MongoDB
    try:
        db = get_db()
        record = {
            "title": title,
            "topic": topic,
            "description": description,
            "banner_url": banner_url,
            "scheduled_time": scheduled_time or "Live / Immediate",
            "meeting_uri": meeting_uri,
            "meeting_code": meeting_code,
            "host_email": DEFAULT_ADMIN_HOST,
            "created_by": session.get('username', 'Admin'),
            "created_at": datetime.now(timezone.utc),
            "status": "Active"
        }
        collection_name = getattr(Collections, 'GOOGLE_MEETINGS', 'google_meetings')
        db_res = db[collection_name].insert_one(record)

        return jsonify({
            "success": True,
            "message": "Meeting created successfully!",
            "meeting_id": str(db_res.inserted_id),
            "meetingUri": meeting_uri
        }), 200
    except Exception as exc:
        return jsonify({"error": f"Database save failed: {exc}"}), 500


@meeting_bp.route('/api/admin/meetings/<meeting_id>', methods=['DELETE'])
def api_admin_delete_meeting(meeting_id):
    if session.get('role') != 'admin' and not session.get('is_admin'):
        return jsonify({"error": "Unauthorized access"}), 403

    try:
        from bson.objectid import ObjectId
        db = get_db()
        collection_name = getattr(Collections, 'GOOGLE_MEETINGS', 'google_meetings')
        db[collection_name].delete_one({"_id": ObjectId(meeting_id)})
        return jsonify({"success": True, "message": "Meeting deleted."}), 200
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ==============================================================================
# USER PAGE & VIEW-ONLY APIS
# ==============================================================================

@meeting_bp.route('/user/meetings')
@meeting_bp.route('/meetings')
def user_meetings_page():
    if 'user_id' not in session:
        return redirect(url_for('auth.login'))
    return render_template('user/meetings.html', username=session.get('username', 'User'))


@meeting_bp.route('/api/meetings', methods=['GET'])
def api_get_meetings():
    if 'user_id' not in session:
        return jsonify({"error": "Unauthorized access"}), 401

    try:
        db = get_db()
        collection_name = getattr(Collections, 'GOOGLE_MEETINGS', 'google_meetings')
        meetings = list(db[collection_name].find().sort("created_at", -1))
        
        formatted = []
        for m in meetings:
            m['_id'] = str(m['_id'])
            if isinstance(m.get('created_at'), datetime):
                m['created_at'] = m['created_at'].strftime('%Y-%m-%d %H:%M UTC')
            formatted.append(m)

        return jsonify({"success": True, "meetings": formatted}), 200
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500