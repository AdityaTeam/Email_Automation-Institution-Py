# Project Enhancement Plan: Multi-User Bulk Email System

## Current Project Analysis

### Existing Features:
- Single-user Flask web application
- Hardcoded admin credentials (admin/admin123)
- Email templates stored in app.py
- Email accounts from config.py (shared)
- Excel/CSV file upload with recipient parsing
- Email sending with batch rotation (25 per account)

### Technology Stack:
- Backend: Flask (Python)
- Frontend: HTML, CSS, JavaScript
- Database: MongoDB (to be added)

---

## Comprehensive Plan

### Phase 1: Project Restructuring & MongoDB Setup

#### 1.1 Create New Folder Structure
```
/Email (project root)
├── backend/
│   ├── app.py              # Main Flask application
│   ├── config.py           # Configuration
│   ├── models.py           # MongoDB models
│   ├── routes/
│   │   ├── auth.py         # Authentication routes
│   │   ├── user.py         # User panel routes
│   │   └── admin.py        # Admin panel routes
│   ├── services/
│   │   ├── email_sender.py # Email sending service
│   │   └── mongodb.py      # MongoDB connection
│   └── utils/
│       └── helpers.py      # Helper functions
├── frontend/
│   ├── static/
│   │   ├── css/
│   │   │   └── style.css
│   │   └── js/
│   │       ├── main.js
│   │       ├── user.js
│   │       └── admin.js
│   └── templates/
│       ├── login.html
│       ├── register.html
│       ├── dashboard.html
│       ├── admin/
│       │   ├── index.html
│       │   ├── users.html
│       │   ├── templates.html
│       │   └── logs.html
│       └── user/
│           ├── email_ids.html
│           ├── upload.html
│           ├── compose.html
│           └── logs.html
```

### Phase 2: Database Schema (MongoDB)

#### 2.1 Collections

```
python
# Users Collection
{
    "_id": ObjectId,
    "username": String (unique),
    "password": String (hashed),
    "role": String ("user" | "admin"),
    "created_at": DateTime,
    "is_active": Boolean
}

# Email IDs Collection (per user)
{
    "_id": ObjectId,
    "user_id": ObjectId (ref: Users),
    "email": String,
    "password": String (app password),
    "smtp_server": String,
    "smtp_port": Integer,
    "use_tls": Boolean,
    "emails_sent": Integer (counter),
    "is_active": Boolean,
    "created_at": DateTime
}

# Excel Files Collection
{
    "_id": ObjectId,
    "user_id": ObjectId (ref: Users),
    "filename": String,
    "original_filename": String,
    "recipients_count": Integer,
    "recipients": Array,
    "uploaded_at": DateTime
}

# Requirements Collection
{
    "_id": ObjectId,
    "name": String,
    "is_active": Boolean,
    "created_at": DateTime
}

# Templates Collection
{
    "_id": ObjectId,
    "requirement_id": ObjectId (ref: Requirements),
    "name": String,
    "subject": String,
    "body": String,
    "is_active": Boolean,
    "updated_at": DateTime
}

# Email Logs Collection
{
    "_id": ObjectId,
    "user_id": ObjectId (ref: Users),
    "sender_email_id": ObjectId (ref: EmailIDs),
    "recipient_email": String,
    " subject": String,
    "status": String ("sent" | "failed"),
    "error_message": String (nullable),
    "sent_at": DateTime
}
```

### Phase 3: Backend Implementation

#### 3.1 Authentication (routes/auth.py)
- User registration (self-register)
- User login
- Admin login
- Logout
- Password hashing with bcrypt

#### 3.2 User Panel (routes/user.py)
- Dashboard
- Email IDs management (CRUD)
- Excel file upload with MongoDB storage
- Email composition with template selection
- Email sending with automatic rotation
- View own logs

#### 3.3 Admin Panel (routes/admin.py)
- Dashboard with system overview
- User management (view, delete)
- View any user's email IDs
- View any user's uploaded files
- View any user's email logs
- Template & Requirement management

### Phase 4: Frontend Implementation

#### 4.1 New Pages
- register.html - User registration
- admin/index.html - Admin dashboard
- admin/users.html - User management
- admin/templates.html - Template management
- user/email_ids.html - Email ID management
- user/upload.html - File upload
- user/compose.html - Email composition

#### 4.2 Updates to Existing Pages
- login.html - Add registration link
- dashboard.html - Enhanced with new features
- main.js - New functionality

### Phase 5: Email Sending Logic

#### 5.1 Rotation Algorithm
```
function get_next_sender(user_id):
    user_email_ids = get_user_email_ids(user_id)
    
    if selected_email_id:
        start_index = index of selected_email_id
    else:
        start_index = 0
    
    for i in range(len(user_email_ids)):
        index = (start_index + i) % len(user_email_ids)
        email_id = user_email_ids[index]
        
        if email_id.emails_sent < 25:
            return email_id
        else:
            # Reset counter if all exceeded (new cycle)
            if i == len(user_email_ids) - 1:
                reset_all_counters(user_email_ids)
                return user_email_ids[0]
    
    return None (all exhausted)
```

### Phase 6: Data Visibility Rules

| Data | User | Admin |
|------|------|-------|
| Own email IDs | ✅ | ✅ |
| Other users' email IDs | ❌ | ✅ |
| Own Excel files | ✅ | ✅ |
| Other users' Excel files | ❌ | ✅ |
| Templates | ✅ | ✅ |
| Requirements | ✅ | ✅ |
| Own logs | ✅ | ✅ |
| Other users' logs | ❌ | ✅ |

---

## Implementation Order

1. **Setup MongoDB connection**
2. **Create database models**
3. **Implement authentication**
4. **Build user panel**
5. **Build admin panel**
6. **Implement email sending with rotation**
7. **Update frontend**
8. **Testing**

---

## Dependencies to Add

```
flask==3.0.0
pymongo==4.6.0
dnspython==2.4.2
bcrypt==4.1.2
python-dotenv==1.0.0
```

---

## Admin Default Credentials
- Username: admin
- Password: admin123 (to be set on first run)
