"""
New Isolated Models for Newsletter, Banner, and Independent Email Logging
Keeps existing models.py completely untouched.
"""

from datetime import datetime
from bson import ObjectId
from database import MongoDB, Collections


class NewsletterModel:
    """Newsletter model helper functions"""

    @staticmethod
    def create(user_id, title, subject, content, blocks=None):
        db = MongoDB.get_db()
        if db is None:
            return None
        doc = {
            'user_id': ObjectId(user_id),
            'title': title,
            'subject': subject,
            'content': content,
            'blocks': blocks or [],
            'created_at': datetime.utcnow(),
            'updated_at': datetime.utcnow()
        }
        res = db[Collections.NEWSLETTERS].insert_one(doc)
        doc['_id'] = str(res.inserted_id)
        doc['user_id'] = str(doc['user_id'])
        return doc

    @staticmethod
    def update(newsletter_id, user_id, title, subject, content, blocks=None):
        db = MongoDB.get_db()
        if db is None:
            return False
        res = db[Collections.NEWSLETTERS].update_one(
            {'_id': ObjectId(newsletter_id), 'user_id': ObjectId(user_id)},
            {'$set': {
                'title': title,
                'subject': subject,
                'content': content,
                'blocks': blocks or [],
                'updated_at': datetime.utcnow()
            }}
        )
        return res.modified_count > 0

    @staticmethod
    def get_by_user(user_id):
        db = MongoDB.get_db()
        if db is None:
            return []
        items = list(db[Collections.NEWSLETTERS].find({'user_id': ObjectId(user_id)}).sort('updated_at', -1))
        for item in items:
            item['_id'] = str(item['_id'])
            item['user_id'] = str(item['user_id'])
            item['created_at'] = item['created_at'].isoformat() if item.get('created_at') else None
            item['updated_at'] = item['updated_at'].isoformat() if item.get('updated_at') else None
        return items

    @staticmethod
    def get_by_id(newsletter_id):
        db = MongoDB.get_db()
        if db is None:
            return None
        if not ObjectId.is_valid(str(newsletter_id)):
            return None
        item = db[Collections.NEWSLETTERS].find_one({'_id': ObjectId(newsletter_id)})
        if item:
            item['_id'] = str(item['_id'])
            item['user_id'] = str(item['user_id'])
        return item

    @staticmethod
    def delete(newsletter_id, user_id):
        db = MongoDB.get_db()
        if db is None:
            return False
        if not ObjectId.is_valid(str(newsletter_id)):
            return False
        res = db[Collections.NEWSLETTERS].delete_one({'_id': ObjectId(newsletter_id), 'user_id': ObjectId(user_id)})
        return res.deleted_count > 0


class BannerModel:
    """Banner with Content model helper functions"""

    @staticmethod
    def create(user_id, title, blocks, bg_color='#ffffff', width=650):
        db = MongoDB.get_db()
        if db is None:
            return None
        doc = {
            'user_id': ObjectId(user_id),
            'title': title,
            'blocks': blocks or [],
            'bgColor': bg_color,
            'width': width,
            'rendered_image_url': None,
            'created_at': datetime.utcnow(),
            'updated_at': datetime.utcnow()
        }
        res = db[Collections.BANNERS].insert_one(doc)
        doc['_id'] = str(res.inserted_id)
        doc['user_id'] = str(doc['user_id'])
        return doc

    @staticmethod
    def update(banner_id, user_id, title, blocks, bg_color='#ffffff', width=650, rendered_image_url=None):
        db = MongoDB.get_db()
        if db is None:
            return False
        update_fields = {
            'title': title,
            'blocks': blocks or [],
            'bgColor': bg_color,
            'width': width,
            'updated_at': datetime.utcnow()
        }
        if rendered_image_url:
            update_fields['rendered_image_url'] = rendered_image_url

        res = db[Collections.BANNERS].update_one(
            {'_id': ObjectId(banner_id), 'user_id': ObjectId(user_id)},
            {'$set': update_fields}
        )
        return res.modified_count > 0

    @staticmethod
    def get_by_user(user_id):
        db = MongoDB.get_db()
        if db is None:
            return []
        items = list(db[Collections.BANNERS].find({'user_id': ObjectId(user_id)}).sort('updated_at', -1))
        for item in items:
            item['_id'] = str(item['_id'])
            item['user_id'] = str(item['user_id'])
            item['created_at'] = item['created_at'].isoformat() if item.get('created_at') else None
            item['updated_at'] = item['updated_at'].isoformat() if item.get('updated_at') else None
        return items

    @staticmethod
    def get_by_id(banner_id):
        db = MongoDB.get_db()
        if db is None:
            return None
        if not ObjectId.is_valid(str(banner_id)):
            return None
        item = db[Collections.BANNERS].find_one({'_id': ObjectId(banner_id)})
        if item:
            item['_id'] = str(item['_id'])
            item['user_id'] = str(item['user_id'])
        return item

    @staticmethod
    def delete(banner_id, user_id):
        db = MongoDB.get_db()
        if db is None:
            return False
        if not ObjectId.is_valid(str(banner_id)):
            return False
        res = db[Collections.BANNERS].delete_one({'_id': ObjectId(banner_id), 'user_id': ObjectId(user_id)})
        return res.deleted_count > 0


class NewEmailLogModel:
    """Independent logging model for Newsletter and Banner email dispatches"""

    @staticmethod
    def create(user_id, feature_type, sender_email_id, recipient, subject, status, error=None):
        db = MongoDB.get_db()
        if db is None:
            return None
        log_doc = {
            'user_id': ObjectId(user_id),
            'feature_type': feature_type,  # 'newsletter' or 'banner'
            'sender_email_id': str(sender_email_id) if sender_email_id else '',
            'recipient': recipient,
            'subject': subject,
            'status': status,  # 'sent' or 'failed'
            'error': error,
            'sent_at': datetime.utcnow()
        }
        res = db[Collections.NEW_EMAIL_LOGS].insert_one(log_doc)
        log_doc['_id'] = str(res.inserted_id)
        log_doc['user_id'] = str(log_doc['user_id'])
        return log_doc

    @staticmethod
    def get_by_user(user_id, feature_type=None, limit=100):
        db = MongoDB.get_db()
        if db is None:
            return []
        query = {'user_id': ObjectId(user_id)}
        if feature_type:
            query['feature_type'] = feature_type

        items = list(db[Collections.NEW_EMAIL_LOGS].find(query).sort('sent_at', -1).limit(limit))
        for item in items:
            item['_id'] = str(item['_id'])
            item['user_id'] = str(item['user_id'])
            item['sent_at'] = item['sent_at'].isoformat() if item.get('sent_at') else None
        return items
