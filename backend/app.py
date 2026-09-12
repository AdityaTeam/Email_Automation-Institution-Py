"""
Flask Web Application for Multi-User Bulk Email Sender System
With MongoDB integration and role-based access control
"""

import os
import sys
import io

# Failsafe UTF-8 rewrapper for Windows console stdout/stderr
if sys.platform == 'win32' or hasattr(sys.stdout, 'buffer'):
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='backslashreplace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='backslashreplace')
    except Exception:
        pass

from flask import Flask, render_template, request, jsonify
import traceback

try:
    from database import MongoDB, init_db
    from routes.auth import auth_bp
    from routes.user import user_bp
    from routes.admin import admin_bp
    from routes.updates import updates_bp
    from routes.newsletter import newsletter_bp
    from routes.banner import banner_bp
except ImportError:
    from backend.database import MongoDB, init_db
    from backend.routes.auth import auth_bp
    from backend.routes.user import user_bp
    from backend.routes.admin import admin_bp
    from backend.routes.updates import updates_bp
    from backend.routes.newsletter import newsletter_bp
    from backend.routes.banner import banner_bp
import traceback

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', '3c935dc77ecc7312ef3414aaf939f276')

# Configure upload folder
UPLOAD_FOLDER = 'uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size

# Register blueprints
app.register_blueprint(auth_bp)
app.register_blueprint(user_bp)
app.register_blueprint(admin_bp)
app.register_blueprint(updates_bp)
app.register_blueprint(newsletter_bp)
app.register_blueprint(banner_bp)


@app.route('/')
def index():
    """Root route - redirect based on authentication"""
    from flask import session, redirect, url_for
    if 'user_id' in session:
        if session.get('role') == 'admin':
            return redirect(url_for('admin.dashboard'))
        return redirect(url_for('user.dashboard'))
    return redirect(url_for('auth.login'))


@app.errorhandler(400)
def bad_request(e):
    """Handle 400 errors"""
    if request.path.startswith('/api/') or request.is_json or request.accept_mimetypes.accept_json:
        return jsonify({'success': False, 'error': str(getattr(e, 'description', e))}), 400
    return render_template('error.html', error=str(getattr(e, 'description', 'Bad Request'))), 400


@app.errorhandler(415)
def unsupported_media_type(e):
    """Handle 415 Unsupported Media Type"""
    print("[WARNING] 415 Unsupported Media Type intercepted:", e)
    if request.path.startswith('/api/') or request.is_json or request.accept_mimetypes.accept_json:
        return jsonify({
            'success': False,
            'error': f"Unsupported Media Type (415): {str(getattr(e, 'description', e))}"
        }), 415
    return render_template('error.html', error='Unsupported Media Type (415)'), 415


@app.errorhandler(404)
def not_found(e):
    """Handle 404 errors"""
    if request.path.startswith('/api/') or request.is_json or request.accept_mimetypes.accept_json:
        return jsonify({'success': False, 'error': 'Endpoint not found (404)'}), 404
    return render_template('error.html', error='Page not found'), 404


@app.errorhandler(500)
def safe_console_print(*args, **kwargs):
    try:
        msg = " ".join(str(a) for a in args)
        print(msg.encode('ascii', 'replace').decode('ascii'), **kwargs)
    except Exception:
        pass


@app.errorhandler(404)
def not_found_error(e):
    """Handle 404 errors"""
    if request.path.startswith('/api/') or request.is_json or request.accept_mimetypes.accept_json:
        return jsonify({'success': False, 'error': 'Endpoint not found (404)'}), 404
    return render_template('error.html', error='Page not found'), 404


@app.errorhandler(405)
def method_not_allowed_error(e):
    """Handle 405 errors"""
    if request.path.startswith('/api/') or request.is_json or request.accept_mimetypes.accept_json:
        return jsonify({'success': False, 'error': 'Method not allowed (405)'}), 405
    return render_template('error.html', error='Method not allowed'), 405


@app.errorhandler(500)
def server_error(e):
    """Handle 500 errors"""
    try:
        tb = traceback.format_exc()
        safe_console_print("[ERROR 500] Internal Server Error:\n" + tb)
    except Exception:
        pass
    if request.path.startswith('/api/') or request.is_json or request.accept_mimetypes.accept_json:
        return jsonify({
            'success': False,
            'error': str(getattr(e, 'description', e))
        }), 500
    return render_template('error.html', error='Internal server error'), 500


@app.errorhandler(Exception)
def unhandled_exception(e):
    """Catch-all for unhandled exceptions"""
    try:
        tb = traceback.format_exc()
        safe_console_print("[ERROR EXCEPTION] Unhandled Exception:\n" + tb)
    except Exception:
        pass
    
    code = getattr(e, 'code', 500)
    if not isinstance(code, int):
        code = 500

    if request.path.startswith('/api/') or request.is_json or request.accept_mimetypes.accept_json:
        return jsonify({
            'success': False,
            'error': str(getattr(e, 'description', e))
        }), code
    return render_template('error.html', error=str(e)), code


def create_app():
    """Application factory"""
    # Connect to MongoDB
    MongoDB.connect()
    
    # Initialize database with default data
    init_db()
    
    return app


if __name__ == '__main__':
    print("="*60)
    print("[STARTING] Multi-User Bulk Email Sender")
    print("="*60)
    
    # Connect to MongoDB
    MongoDB.connect()
    
    # Initialize database
    init_db()
    
    print("\n[ROUTES] Application Routes:")
    print("   - Login: http://localhost:5003/login")
    print("   - Register: http://localhost:5003/register")
    print("   - Newsletter: http://localhost:5003/newsletter")
    print("   - Banner Builder: http://localhost:5003/banner-builder")
    print("   - Admin Panel: http://localhost:5003/admin")
    print("\n[CREDENTIALS] Default Admin Credentials:")
    print("   Username: admin")
    print("   Password: admin123")
    print("\n[URL] Open your browser and go to: http://localhost:5003")
    print("="*60)
    
    app.run(debug=True, host='0.0.0.0', port=5003, use_reloader=False)
