"""
MongoDB Connection Module
Handles connection to MongoDB and database operations
"""

from pymongo import MongoClient
from pymongo.errors import ConnectionFailure
import os
from dotenv import load_dotenv

load_dotenv()

# MongoDB Configuration
MONGO_URI = os.getenv('MONGO_URI', 'mongodb://localhost:27017/')
DATABASE_NAME = os.getenv('DATABASE_NAME', 'bulk_email_sender')

class MongoDB:
    """MongoDB connection manager"""
    
    _client = None
    _db = None
    
    @classmethod
    def connect(cls):
        """Connect to MongoDB"""
        try:
            cls._client = MongoClient(MONGO_URI)
            cls._db = cls._client[DATABASE_NAME]
            # Test connection
            cls._client.admin.command('ping')
            print(f"✅ Connected to MongoDB: {DATABASE_NAME}")
            return cls._db
        except ConnectionFailure as e:
            print(f"❌ Failed to connect to MongoDB: {e}")
            return None
    
    @classmethod
    def get_db(cls):
        """Get database instance"""
        if cls._db is None:
            cls.connect()
        return cls._db
    
    @classmethod
    def get_collection(cls, name):
        """Get a specific collection"""
        db = cls.get_db()
        if db:
            return db[name]
        return None
    
    @classmethod
    def close(cls):
        """Close MongoDB connection"""
        if cls._client:
            cls._client.close()
            print("🔌 MongoDB connection closed")


# Database Collections
class Collections:
    """Collection names"""
    USERS = 'users'
    EMAIL_IDS = 'email_ids'
    EXCEL_FILES = 'excel_files'
    REQUIREMENTS = 'requirements'
    TEMPLATES = 'templates'
    EMAIL_LOGS = 'email_logs'


# Initialize database with default data
def init_db():
    """Initialize database with default admin and templates"""
    db = MongoDB.get_db()
    if db is None:
        return False
    
    # Create indexes
    db[Collections.USERS].create_index('username', unique=True)
    db[Collections.EMAIL_IDS].create_index('user_id')
    db[Collections.EXCEL_FILES].create_index('user_id')
    db[Collections.EMAIL_LOGS].create_index('user_id')
    db[Collections.EMAIL_LOGS].create_index('sent_at')
    
    # Check if admin exists
    admin = db[Collections.USERS].find_one({'role': 'admin'})
    if not admin:
        # Plain text password - as requested
        admin_data = {
            'username': 'admin',
            'password': 'admin123',  # Plain text
            'role': 'admin',
            'is_active': True
        }
        db[Collections.USERS].insert_one(admin_data)
        print("✅ Default admin created (username: admin, password: admin123)")
    
    # Check if requirements exist
    if db[Collections.REQUIREMENTS].count_documents({}) == 0:
        default_requirements = [
            {'name': 'Admission', 'is_active': True},
            {'name': 'Event', 'is_active': True},
            {'name': 'Newsletter', 'is_active': True}
        ]
        db[Collections.REQUIREMENTS].insert_many(default_requirements)
        print("✅ Default requirements created")
    
    # Check if templates exist
    if db[Collections.TEMPLATES].count_documents({}) == 0:
        requirements = list(db[Collections.REQUIREMENTS].find())
        default_templates = []
        
        for req in requirements:
            if req['name'] == 'Admission':
                default_templates.append({
                    'requirement_id': req['_id'],
                    'name': 'Admission Inquiry',
                    'subject': 'Admission Open for 2024 - {{institute}}',
                    'body': '''Dear {{name}},

We are pleased to inform you that admissions are now open for the upcoming academic year at {{institute}}.

Our institution offers a wide range of courses with excellent faculty and infrastructure.

Key Highlights:
- Expert Faculty
- Modern Infrastructure
- Placement Assistance
- Hostel Facilities

For more information, please visit our website or contact us.

Best regards,
Admissions Team
{{institute}}''',
                    'is_active': True
                })
            elif req['name'] == 'Event':
                default_templates.append({
                    'requirement_id': req['_id'],
                    'name': 'Event Invitation',
                    'subject': "You're Invited! - {{institute}}",
                    'body': '''Dear {{name}},

We are excited to invite you to our upcoming event!

Event Details:
- Date: Coming Soon
- Venue: {{institute}}

This is a great opportunity to explore our campus and interact with our faculty.

We look forward to seeing you there!

Warm regards,
Event Coordinator
{{institute}}''',
                    'is_active': True
                })
            elif req['name'] == 'Newsletter':
                default_templates.append({
                    'requirement_id': req['_id'],
                    'name': 'Monthly Newsletter',
                    'subject': 'Newsletter - {{institute}}',
                    'body': '''Dear {{name}},

Welcome to our monthly newsletter from {{institute}}!

This Month's Highlights:
- Academic achievements
- Upcoming events
- Student activities
- Campus news

Stay connected with us for more updates!

Best wishes,
{{institute}} Team''',
                    'is_active': True
                })
        
        # Add custom template
        custom_req = db[Collections.REQUIREMENTS].find_one({'name': 'Custom'})
        if not custom_req:
            custom_req = {'name': 'Custom', 'is_active': True}
            result = db[Collections.REQUIREMENTS].insert_one(custom_req)
            custom_req['_id'] = result.inserted_id
        
        default_templates.append({
            'requirement_id': custom_req['_id'],
            'name': 'Custom Email',
            'subject': 'Regarding Your Inquiry',
            'body': '''Dear {{name}},

Thank you for your interest in {{institute}}.

We would like to inform you that our team is ready to assist you with all your queries.

Please feel free to reach out to us for any further information.

Best regards,
{{institute}}''',
            'is_active': True
        })
        
        if default_templates:
            db[Collections.TEMPLATES].insert_many(default_templates)
            print("✅ Default templates created")
    
    print("✅ Database initialized successfully")
    return True


if __name__ == '__main__':
    # Test connection
    db = MongoDB.connect()
    if db:
        init_db()
        MongoDB.close()
