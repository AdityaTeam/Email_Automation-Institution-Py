"""
Database Models
Defines data models and helper functions for MongoDB collections
SIMPLIFIED VERSION - No encryption, plain text passwords
"""

from datetime import datetime
from unittest import result
from bson import ObjectId
from django import db
from database import MongoDB, Collections


class User:
    """User model and helper functions - Plain text passwords"""
    
    @staticmethod
    def create(username, password, role='user'):
        """Create a new user with plain text password"""
        db = MongoDB.get_db()
        if db is None:
            return None
        
        # Check if username exists
        if db[Collections.USERS].find_one({'username': username}):
            return None
        
        # Store password in plain text (as requested)
        user_data = {
            'username': username,
            'password': password,  # Plain text - as requested
            'role': role,
            'is_active': True,
            'created_at': datetime.utcnow()
        }
        result = db[Collections.USERS].insert_one(user_data)
        user_data['_id'] = result.inserted_id
        return user_data
    
    @staticmethod
    def authenticate(username, password):
        db = MongoDB.get_db()
        if db is None:
            return None
    
        user = db[Collections.USERS].find_one({'username': username, 'is_active': True})
    
        # Plain password check
        if user and password == user['password']:
            return user
        return None
    
    @staticmethod
    def get_by_id(user_id):
        """Get user by ID"""
        db = MongoDB.get_db()
        if db is None:
            return None
        return db[Collections.USERS].find_one({'_id': ObjectId(user_id)})
    
    @staticmethod
    def get_by_username(username):
        """Get user by username"""
        db = MongoDB.get_db()
        if db is None:
            return None
        return db[Collections.USERS].find_one({'username': username})
    
    @staticmethod
    def get_all():
        """Get all users"""
        db = MongoDB.get_db()
        if db is None:
            return []
        return list(db[Collections.USERS].find({'role': 'user'}).sort('created_at', -1))
    
    @staticmethod
    def delete(user_id):
        """Delete a user"""
        db = MongoDB.get_db()
        if db is None:
            return False
        # Delete user's email IDs
        db[Collections.EMAIL_IDS].delete_many({'user_id': ObjectId(user_id)})
        # Delete user's excel files
        db[Collections.EXCEL_FILES].delete_many({'user_id': ObjectId(user_id)})
        # Delete user's logs
        db[Collections.EMAIL_LOGS].delete_many({'user_id': ObjectId(user_id)})
        # Delete user
        result = db[Collections.USERS].delete_one({'_id': ObjectId(user_id)})
        return result.deleted_count > 0
    
    @staticmethod
    def reset_password(user_id, new_password):
        """Reset user password (admin function) - plain text"""
        db = MongoDB.get_db()
        if db is None:
            return False
        
        result = db[Collections.USERS].update_one(
            {'_id': ObjectId(user_id)},
            {'$set': {'password': new_password}}
        )
        return result.modified_count > 0
    
    @staticmethod
    def get_user_with_password(user_id):
        """Get user with password (for admin view only)"""
        db = MongoDB.get_db()
        if db is None:
            return None
        return db[Collections.USERS].find_one({'_id': ObjectId(user_id)})


class EmailID:
    """Email ID model and helper functions - Plain text passwords, no encryption"""
    
    @staticmethod
    def create(user_id, email_data):
        """Create a new email ID for a user - store password as plain text"""
        db = MongoDB.get_db()
        if db is None:
            return None
        
        # Store password as plain text (no encryption)
        email_id_data = {
            'user_id': ObjectId(user_id),
            'email': email_data['email'],
            'password': email_data.get('password', ''),  # Plain text
            'smtp_server': email_data.get('smtp_server', 'smtp.gmail.com'),
            'smtp_port': int(email_data.get('smtp_port', 587)),
            'use_tls': email_data.get('use_tls', True),
            'use_ssl': email_data.get('use_ssl', False),
            'emails_sent': 0,
            'is_active': True,
            'created_at': datetime.utcnow()
        }
        result = db[Collections.EMAIL_IDS].insert_one(email_id_data)
        email_id_data['_id'] = result.inserted_id
        return email_id_data
    
    @staticmethod
    def get_by_user(user_id):
        """Get all email IDs for a user (without password)"""
        db = MongoDB.get_db()
        if db is None:
            return []
        return list(db[Collections.EMAIL_IDS].find({
            'user_id': ObjectId(user_id),
            'is_active': True
        }).sort('created_at', 1))
    
    @staticmethod
    def get_by_id(email_id):
        """Get email ID by ID"""
        db = MongoDB.get_db()
        if db is None:
            return None
        return db[Collections.EMAIL_IDS].find_one({'_id': ObjectId(email_id)})
    
    @staticmethod
    def get_by_id_with_password(email_id):
        """Get email ID by ID with password (for sending/admin)"""
        db = MongoDB.get_db()
        if db is None:
            return None
        return db[Collections.EMAIL_IDS].find_one({'_id': ObjectId(email_id)})
    
    @staticmethod
    def get_by_user_with_passwords(user_id):
        """Get all email IDs for a user with passwords (for admin only)"""
        db = MongoDB.get_db()
        if db is None:
            return []
        return list(db[Collections.EMAIL_IDS].find({
            'user_id': ObjectId(user_id),
            'is_active': True
        }).sort('created_at', 1))
    
    @staticmethod
    def increment_sent_count(email_id):
        """Increment the sent count for an email ID"""
        db = MongoDB.get_db()
        if db is None:
            return False
        db[Collections.EMAIL_IDS].update_one(
            {'_id': ObjectId(email_id)},
            {'$inc': {'emails_sent': 1}}
        )
        return True
    
    @staticmethod
    @staticmethod
    def reset_counts(user_id):
        db = MongoDB.get_db()

        if db is None:
            return False

        if not isinstance(user_id, ObjectId):
            user_id = ObjectId(user_id)

        result = db[Collections.EMAIL_IDS].update_many(
            {"user_id": user_id},
            {"$set": {"emails_sent": 0}}
        )

        print("Reset:", result.modified_count)

        return True
    
    @staticmethod
    def delete(email_id):
        """Delete an email ID"""
        db = MongoDB.get_db()
        if db is None:
            return False
        result = db[Collections.EMAIL_IDS].delete_one({'_id': ObjectId(email_id)})
        return result.deleted_count > 0
    
    @staticmethod
    def update_password(email_id, new_password):
        """Update email ID password (plain text)"""
        db = MongoDB.get_db()
        if db is None:
            return False
        result = db[Collections.EMAIL_IDS].update_one(
            {'_id': ObjectId(email_id)},
            {'$set': {'password': new_password}}
        )
        return result.modified_count > 0
    
    @staticmethod
    def get_next_available(user_id, start_email_id=None):
        """Get the next available email ID based on rotation logic"""
        db = MongoDB.get_db()
        if db is None:
            return None
        
        email_ids = list(db[Collections.EMAIL_IDS].find({
            'user_id': ObjectId(user_id),
            'is_active': True
        }).sort('created_at', 1))
        
        if not email_ids:
            return None
        
        # Find starting index
        start_index = 0
        if start_email_id:
            for i, eid in enumerate(email_ids):
                if str(eid['_id']) == start_email_id:
                    start_index = i
                    break
        
        BATCH_SIZE = 25
        
        # Try to find an email ID that hasn't exceeded the limit
        for i in range(len(email_ids)):
            index = (start_index + i) % len(email_ids)
            email_id = email_ids[index]
            
            if email_id['emails_sent'] < BATCH_SIZE:
                return email_id
        
        # All email IDs have exceeded limit, reset and return first
        EmailID.reset_counts(user_id)
        return email_ids[0]


class ExcelFile:
    """Excel File model and helper functions"""
    
    @staticmethod
    def create(user_id, filename, original_filename, recipients):
        """Create a new excel file record"""
        db = MongoDB.get_db()
        if db is None:
            return None
        
        file_data = {
            'user_id': ObjectId(user_id),
            'filename': filename,
            'original_filename': original_filename,
            'recipients_count': len(recipients),
            'recipients': recipients,
            'uploaded_at': datetime.utcnow()
        }
        result = db[Collections.EXCEL_FILES].insert_one(file_data)
        file_data['_id'] = result.inserted_id
        return file_data
    
    @staticmethod
    def get_by_user(user_id):
        """Get all excel files for a user"""
        db = MongoDB.get_db()
        if db is None:
            return []
        return list(db[Collections.EXCEL_FILES].find({
            'user_id': ObjectId(user_id)
        }).sort('uploaded_at', -1))
    
    @staticmethod
    def get_by_id(file_id):
        """Get excel file by ID"""
        db = MongoDB.get_db()
        if db is None:
            return None
        return db[Collections.EXCEL_FILES].find_one({'_id': ObjectId(file_id)})
    
    @staticmethod
    def delete(file_id):
        """Delete an excel file"""
        db = MongoDB.get_db()
        if db is None:
            return False
        result = db[Collections.EXCEL_FILES].delete_one({'_id': ObjectId(file_id)})
        return result.deleted_count > 0


class Requirement:
    """Requirement model and helper functions"""
    
    @staticmethod
    def get_all():
        """Get all requirements"""
        db = MongoDB.get_db()
        if db is None:
            return []
        return list(db[Collections.REQUIREMENTS].find({'is_active': True}))
    
    @staticmethod
    def create(name):
        """Create a new requirement"""
        db = MongoDB.get_db()
        if db is None:
            return None
        
        req_data = {
            'name': name,
            'is_active': True
        }
        result = db[Collections.REQUIREMENTS].insert_one(req_data)
        req_data['_id'] = result.inserted_id
        return req_data
    
    @staticmethod
    def update(req_id, name):
        """Update a requirement"""
        db = MongoDB.get_db()
        if db is None:
            return False
        result = db[Collections.REQUIREMENTS].update_one(
            {'_id': ObjectId(req_id)},
            {'$set': {'name': name}}
        )
        return result.modified_count > 0
    
    @staticmethod
    def delete(req_id):
        """Delete a requirement"""
        db = MongoDB.get_db()
        if db is None:
            return False
        # Also delete associated templates
        db[Collections.TEMPLATES].delete_many({'requirement_id': ObjectId(req_id)})
        result = db[Collections.REQUIREMENTS].delete_one({'_id': ObjectId(req_id)})
        return result.deleted_count > 0


class Template:
    """Template model and helper functions"""
    
    # Default signature format - fixed structure
    DEFAULT_SIGNATURE_FORMAT = """Best Regards,
{{executive_name}}
{{position}}
{{company_name}}
{{company_phone}}
{{company_email}}
{{company_website}}"""
    
    @staticmethod
    def get_all():
        """Get all templates"""
        db = MongoDB.get_db()
        if db is None:
            return []
        return list(db[Collections.TEMPLATES].find({'is_active': True}).sort('updated_at', -1))
    
    @staticmethod
    def get_by_requirement(requirement_id):
        """Get templates by requirement"""
        db = MongoDB.get_db()
        if db is None:
            return []
        return list(db[Collections.TEMPLATES].find({
            'requirement_id': ObjectId(requirement_id),
            'is_active': True
            }).sort('updated_at', -1)
        )
    
    @staticmethod
    def get_by_id(template_id):
        """Get template by ID"""
        db = MongoDB.get_db()
        if db is None:
            return None
        template = db[Collections.TEMPLATES].find_one({'_id': ObjectId(template_id)})
        if template and 'signature_format' not in template:
            template['signature_format'] = Template.DEFAULT_SIGNATURE_FORMAT
        if template and 'attachments' not in template:
            template['attachments'] = []
        return template
    
    @staticmethod
    def create(template_data):
        """Create a new template"""
        db = MongoDB.get_db()
        if db is None:
            return None
        
        template = {
            'requirement_id': ObjectId(template_data['requirement_id']),
            'name': template_data['name'],
            'subject': template_data['subject'],
            'body': template_data['body'],
            'attachments': template_data.get('attachments', []),
            'signature_format': template_data.get('signature_format', Template.DEFAULT_SIGNATURE_FORMAT),
            'is_active': True,
            'updated_at': datetime.utcnow()
        }
        result = db[Collections.TEMPLATES].insert_one(template)
        template['_id'] = result.inserted_id
        return template
    
    @staticmethod
    def update(template_id, template_data):
        """Update a template"""
        db = MongoDB.get_db()
        if db is None:
            return False
        
        update_data = {
            'name': template_data.get('name'),
            'subject': template_data.get('subject'),
            'body': template_data.get('body'),
            'attachments': template_data.get('attachments', []),
            'signature_format': template_data.get('signature_format', Template.DEFAULT_SIGNATURE_FORMAT),
            'updated_at': datetime.utcnow()
        }
        
        result = db[Collections.TEMPLATES].update_one(
            {'_id': ObjectId(template_id)},
            {'$set': update_data}
        )
        return result.modified_count > 0
    
    @staticmethod
    def delete(template_id):
        """Delete a template"""
        db = MongoDB.get_db()
        if db is None:
            return False
        result = db[Collections.TEMPLATES].delete_one({'_id': ObjectId(template_id)})
        return result.deleted_count > 0
    
    @staticmethod
    def build_signature(signature_data):
        """Build signature from user-provided data"""
        signature = Template.DEFAULT_SIGNATURE_FORMAT
        signature = signature.replace('{{executive_name}}', signature_data.get('executive_name', ''))
        signature = signature.replace('{{position}}', signature_data.get('position', ''))
        signature = signature.replace('{{company_name}}', signature_data.get('company_name', ''))
        signature = signature.replace('{{company_email}}', signature_data.get('company_email', ''))
        signature = signature.replace('{{company_phone}}', signature_data.get('company_phone', ''))
        signature = signature.replace('{{company_website}}', signature_data.get('company_website', ''))
        return signature
    
    @staticmethod
    def process_body(body):
        """
        Process template body to convert markdown-like formatting to HTML
        Converts **text** to <strong>text</strong>
        Converts *text* to <em>text</em>
        """
        import re
        
        # Convert **text** to <strong>text</strong>
        body = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', body)
        
        # Convert *text* to <em>text</em> (but not **)
        body = re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'<em>\1</em>', body)
        
        # Convert line breaks to <br>
        body = body.replace('\n', '<br>')
        
        return body


class EmailLog:
    """Email Log model and helper functions"""
    
    @staticmethod
    def create(user_id, sender_email_id, recipient_email, subject, status, error_message=None):
        """Create a new email log"""
        db = MongoDB.get_db()
        if db is None:
            return None
        
        log_data = {
            'user_id': ObjectId(user_id),
            'sender_email_id': ObjectId(sender_email_id),
            'recipient_email': recipient_email,
            'subject': subject,
            'status': status,
            'error_message': error_message,
            'sent_at': datetime.utcnow()
        }
        result = db[Collections.EMAIL_LOGS].insert_one(log_data)
        log_data['_id'] = result.inserted_id
        return log_data
    
    @staticmethod
    def get_by_user(user_id, limit=100):
        """Get email logs for a user"""
        db = MongoDB.get_db()
        if db is None:
            return []
        return list(db[Collections.EMAIL_LOGS].find({
            'user_id': ObjectId(user_id)
        }).sort('sent_at', -1).limit(limit))
    
    @staticmethod
    def get_all(limit=100):
        """Get all email logs (admin)"""
        db = MongoDB.get_db()
        if db is None:
            return []
        return list(db[Collections.EMAIL_LOGS].find().sort('sent_at', -1).limit(limit))
    
    @staticmethod
    def get_stats(user_id=None):
        """Get email statistics"""
        db = MongoDB.get_db()
        if db is None:
            return {'sent': 0, 'failed': 0}
        
        query = {}
        if user_id:
            query['user_id'] = ObjectId(user_id)
        
        sent = db[Collections.EMAIL_LOGS].count_documents({**query, 'status': 'sent'})
        failed = db[Collections.EMAIL_LOGS].count_documents({**query, 'status': 'failed'})
        
        return {'sent': sent, 'failed': failed}
