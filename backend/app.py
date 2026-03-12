"""
Flask Web Application for Multi-User Bulk Email Sender System
With MongoDB integration and role-based access control
"""

import os
from flask import Flask, render_template
from database import MongoDB, init_db
from routes.auth import auth_bp
from routes.user import user_bp
from routes.admin import admin_bp
from routes.updates import updates_bp

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


@app.route('/')
def index():
    """Root route - redirect based on authentication"""
    from flask import session, redirect, url_for
    if 'user_id' in session:
        if session.get('role') == 'admin':
            return redirect(url_for('admin.dashboard'))
        return redirect(url_for('user.dashboard'))
    return redirect(url_for('auth.login'))


@app.errorhandler(404)
def not_found(e):
    """Handle 404 errors"""
    return render_template('error.html', error='Page not found'), 404


@app.errorhandler(500)
def server_error(e):
    """Handle 500 errors"""
    return render_template('error.html', error='Internal server error'), 500


def create_app():
    """Application factory"""
    # Connect to MongoDB
    MongoDB.connect()
    
    # Initialize database with default data
    init_db()
    
    return app


if __name__ == '__main__':
    print("="*60)
    print("🚀 Starting Multi-User Bulk Email Sender")
    print("="*60)
    
    # Connect to MongoDB
    MongoDB.connect()
    
    # Initialize database
    init_db()
    
    print("\n📧 Application Routes:")
    print("   - Login: http://localhost:5000/login")
    print("   - Register: http://localhost:5000/register")
    print("   - Admin Panel: http://localhost:5000/admin")
    print("\n🔑 Default Admin Credentials:")
    print("   Username: admin")
    print("   Password: admin123")
    print("\n🌐 Open your browser and go to: http://localhost:5000")
    print("="*60)
    
    app.run(debug=True, host='0.0.0.0', port=5000, use_reloader=False)
