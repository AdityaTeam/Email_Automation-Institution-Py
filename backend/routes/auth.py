"""
Authentication Routes
Handles user registration, login, and logout
"""

from flask import Blueprint, render_template, request, jsonify, session, redirect, url_for
from models import User
import os

auth_bp = Blueprint('auth', __name__)


@auth_bp.route('/')
def index():
    """Redirect to login or dashboard based on session"""
    if 'user_id' in session:
        if session.get('role') == 'admin':
            return redirect(url_for('admin.dashboard'))
        return redirect(url_for('user.dashboard'))
    return redirect(url_for('auth.login'))


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    """User login"""
    if 'user_id' in session:
        if session.get('role') == 'admin':
            return redirect(url_for('admin.dashboard'))
        return redirect(url_for('user.dashboard'))
    
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        
        if not username or not password:
            return render_template('login.html', error='Please enter username and password')
        
        user = User.authenticate(username, password)
        if user:
            session['user_id'] = str(user['_id'])
            session['username'] = user['username']
            session['role'] = user['role']
            
            if user['role'] == 'admin':
                return redirect(url_for('admin.dashboard'))
            return redirect(url_for('user.dashboard'))
        else:
            return render_template('login.html', error='Invalid username or password')
    
    return render_template('login.html')


@auth_bp.route('/register', methods=['GET', 'POST'])
def register():
    """User registration"""
    if 'user_id' in session:
        return redirect(url_for('user.dashboard'))
    
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')
        
        if not username or not password:
            return render_template('register.html', error='Please enter username and password')
        
        if password != confirm_password:
            return render_template('register.html', error='Passwords do not match')
        
        if len(password) < 4:
            return render_template('register.html', error='Password must be at least 4 characters')
        
        user = User.create(username, password, role='user')
        if user:
            # Auto-login after registration
            session['user_id'] = str(user['_id'])
            session['username'] = user['username']
            session['role'] = user['role']
            return redirect(url_for('user.dashboard'))
        else:
            return render_template('register.html', error='Username already exists')
    
    return render_template('register.html')


@auth_bp.route('/logout')
def logout():
    """User logout"""
    session.clear()
    return redirect(url_for('auth.login'))


@auth_bp.route('/api/check-auth')
def check_auth():
    """Check if user is authenticated"""
    if 'user_id' in session:
        return jsonify({
            'authenticated': True, 
            'username': session.get('username'),
            'role': session.get('role')
        })
    return jsonify({'authenticated': False})


@auth_bp.route('/api/register', methods=['POST'])
def api_register():
    """API endpoint for registration"""
    data = request.json
    username = data.get('username', '').strip()
    password = data.get('password', '')
    
    if not username or not password:
        return jsonify({'error': 'Please enter username and password'}), 400
    
    if len(password) < 4:
        return jsonify({'error': 'Password must be at least 4 characters'}), 400
    
    user = User.create(username, password, role='user')
    if user:
        return jsonify({'success': True, 'message': 'Registration successful'})
    else:
        return jsonify({'error': 'Username already exists'}), 400


@auth_bp.route('/api/login', methods=['POST'])
def api_login():
    """API endpoint for login"""
    data = request.json
    username = data.get('username', '').strip()
    password = data.get('password', '')
    
    if not username or not password:
        return jsonify({'error': 'Please enter username and password'}), 400
    
    user = User.authenticate(username, password)
    if user:
        session['user_id'] = str(user['_id'])
        session['username'] = user['username']
        session['role'] = user['role']
        return jsonify({
            'success': True, 
            'username': user['username'],
            'role': user['role']
        })
    else:
        return jsonify({'error': 'Invalid username or password'}), 401


@auth_bp.route('/api/logout', methods=['POST'])
def api_logout():
    """API endpoint for logout"""
    session.clear()
    return jsonify({'success': True})
