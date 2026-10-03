"""
Google Meet API v2 Service Integration
Handles domain-wide delegated space creation using Google Service Account credentials.
"""

import os
from google.apps import meet_v2
from google.oauth2 import service_account

# Scope required for direct meeting space creation
SCOPES = ['https://www.googleapis.com/auth/meetings.space.created']


def create_google_meet_space(impersonated_admin_email: str) -> dict:
    """
    Creates a new Google Meet space by impersonating a Workspace user/admin.

    :param impersonated_admin_email: Workspace host email (e.g., admin@yourdomain.com)
    :return: dict containing meetingUri, space name, and meetingCode
    """
    # Path to service account JSON key file from .env or default location
    credentials_path = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "credentials.json")

    if not os.path.isabs(credentials_path):
        # Resolve path relative to backend directory
        base_dir = os.path.dirname(os.path.abspath(__file__))
        credentials_path = os.path.join(base_dir, credentials_path)

    if not os.path.exists(credentials_path):
        return {
            "success": False,
            "error": f"Credentials file not found at: {credentials_path}"
        }

    try:
        # Load base credentials and delegate authority to host admin
        base_creds = service_account.Credentials.from_service_account_file(
            credentials_path,
            scopes=SCOPES
        )
        delegated_creds = base_creds.with_subject(impersonated_admin_email)

        # Initialize Meet Spaces client
        client = meet_v2.SpacesServiceClient(credentials=delegated_creds)

        # Send space creation request
        request = meet_v2.CreateSpaceRequest()
        response = client.create_space(request=request)

        return {
            "success": True,
            "space_name": response.name,           # e.g. "spaces/xyz-abc-def"
            "meeting_uri": response.meeting_uri,   # e.g. "https://meet.google.com/xyz-abc-def"
            "meeting_code": response.meeting_code  # e.g. "xyz-abc-def"
        }
    except Exception as exc:
        print(f"[X] Google Meet Creation Error: {exc}")
        return {
            "success": False,
            "error": str(exc)
        }