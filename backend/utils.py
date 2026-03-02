"""
Encryption Utility
Handles encryption/decryption for sensitive data like email passwords
"""

import os
from cryptography.fernet import Fernet
from dotenv import load_dotenv

load_dotenv()

# Generate or load encryption key
def get_encryption_key():
    """Get or generate encryption key"""
    key = os.getenv('ENCRYPTION_KEY')
    if not key:
        key = Fernet.generate_key()
        # In production, store this in environment variable
        # For now, we'll generate a new key each time (not persistent)
        # In production, add ENCRYPTION_KEY=your_key to .env
    return key if isinstance(key, bytes) else key.encode()


# Create Fernet cipher
try:
    _cipher = Fernet(get_encryption_key())
except Exception:
    # Generate new key if invalid
    _cipher = Fernet(Fernet.generate_key())


def encrypt_password(password):
    """Encrypt a password"""
    if not password:
        return ""
    return _cipher.encrypt(password.encode()).decode()


def decrypt_password(encrypted_password):
    """Decrypt a password"""
    if not encrypted_password:
        return ""
    try:
        return _cipher.decrypt(encrypted_password.encode()).decode()
    except Exception:
        # Return original if decryption fails (for backwards compatibility)
        return encrypted_password


def generate_secure_password(length=12):
    """Generate a secure random password"""
    import random
    import string
    chars = string.ascii_letters + string.digits + string.punctuation
    return ''.join(random.choice(chars) for _ in range(length))
