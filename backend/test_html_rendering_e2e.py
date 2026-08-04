import os
import sys

# Ensure backend directory is in path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from email_renderer import replace_placeholders, render_full_html_email, build_plain_signature, convert_text_to_html_paragraphs
from email_sender import EmailSender

def run_tests():
    print("🧪 ==================== TESTING EMAIL RENDERER & MIME ====================")
    
    # 1. Test Data
    recipient = {
        'email': 'student.john@harvard.edu',
        'name': 'John Doe',
        'institute': 'Harvard University',
        'Role': 'Research Scholar',
        'Batch': '2026'
    }
    
    signature_data = {
        'executive_name': 'Diksha Sharma',
        'position': 'Head of Outreach & Academic Relations',
        'company_name': 'EduTech Global Solutions',
        'company_email': 'contact@edutech.org',
        'company_phone': '+1 (555) 987-6543',
        'company_website': 'https://www.edutech.org'
    }
    
    raw_subject = "Exclusive Opportunity for {{name}} at {{institute}}"
    raw_body = """Dear {{name}},

Greetings from {{company_name}}!

We are pleased to inform you that {{institute}} has been selected for our upcoming National Faculty & Student Development Seminar.

As a valued {{Role}} of {{Batch}}, we would love to invite you to participate in our exclusive workshops:
- Advanced AI & Agentic Workflows
- Cloud Infrastructure & Scalability
- Interactive Laboratory Demonstrations

Please review the attached brochure for schedule and registration details.

Looking forward to your esteemed presence."""

    # 2. Step 1: Placeholder Replacement
    p_subject = replace_placeholders(raw_subject, recipient_data=recipient, signature_data=signature_data)
    p_body = replace_placeholders(raw_body, recipient_data=recipient, signature_data=signature_data)
    
    print("\n1️⃣ Placeholder Replacement Output:")
    print(f"   Processed Subject: {p_subject}")
    assert "{{name}}" not in p_subject, "Failed: {{name}} still in subject!"
    assert "{{institute}}" not in p_subject, "Failed: {{institute}} still in subject!"
    assert "John Doe" in p_subject
    assert "Harvard University" in p_subject
    assert "{{name}}" not in p_body
    assert "{{institute}}" not in p_body
    assert "{{company_name}}" not in p_body
    assert "EduTech Global Solutions" in p_body
    assert "Research Scholar" in p_body
    print("   ✅ Placeholder replacement verified successfully!")

    sender = EmailSender(
        email_accounts=[{
            'email': 'sender@test.com',
            'password': 'password123',
            'smtp_server': 'smtp.gmail.com',
            'smtp_port': 587
        }],
        batch_size=25
    )

    # 3. Step 2: HTML Email Rendering WITHOUT LOGO (Default)
    print("\n2️⃣ Testing Scenario A: HTML Email WITHOUT Logo (Default/No Upload)")
    html_output_no_logo = render_full_html_email(
        body_text_or_html=p_body,
        signature_data=signature_data,
        has_logo=False,
        subject=p_subject
    )
    assert "<!DOCTYPE html>" in html_output_no_logo
    assert 'cid:company_logo' not in html_output_no_logo, "Failed: cid:company_logo should not exist when has_logo=False"
    assert '<img' not in html_output_no_logo, "Failed: <img> tag should not exist when has_logo=False"
    assert 'Diksha Sharma' in html_output_no_logo
    print("   ✅ HTML without logo verified (no <img>, no CID reference)")

    msg_no_logo = sender.create_email_message(
        to_email=recipient['email'],
        subject=p_subject,
        body=html_output_no_logo,
        from_name="Diksha Sharma",
        is_html=True,
        custom_logo=None
    )
    print(f"   Root Content-Type (No Logo): {msg_no_logo.get_content_type()}")
    assert msg_no_logo.get_content_type() == 'multipart/alternative'
    # Ensure no image parts in payload
    payloads = msg_no_logo.get_payload()
    types = [p.get_content_type() for p in payloads]
    assert 'image/jpeg' not in types and 'image/png' not in types
    assert 'text/plain' in types and 'text/html' in types
    print("   ✅ MIME without logo is clean multipart/alternative (text/plain + text/html)")

    # 4. Step 3: HTML Email WITH CUSTOM LOGO
    print("\n3️⃣ Testing Scenario B: HTML Email WITH Custom Uploaded Logo")
    mock_logo_bytes = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4'
    custom_logo_dict = {
        'data': mock_logo_bytes,
        'filename': 'my_custom_logo.png',
        'mimetype': 'image/png'
    }
    html_output_with_logo = render_full_html_email(
        body_text_or_html=p_body,
        signature_data=signature_data,
        has_logo=True,
        subject=p_subject
    )
    assert 'cid:company_logo' in html_output_with_logo, "Failed: cid:company_logo must exist when has_logo=True"
    print("   ✅ HTML with logo verified (cid:company_logo present)")

    msg_with_logo = sender.create_email_message(
        to_email=recipient['email'],
        subject=p_subject,
        body=html_output_with_logo,
        from_name="Diksha Sharma",
        is_html=True,
        custom_logo=custom_logo_dict
    )
    print(f"   Root Content-Type (With Logo): {msg_with_logo.get_content_type()}")
    assert msg_with_logo.get_content_type() == 'multipart/related'
    print("   --- MIME Structure with Custom Logo ---")
    sender._print_mime_tree(msg_with_logo, indent=3)
    print("   ✅ MIME with custom logo is multipart/related with inline CID image")

    # 5. Step 4: Plain Text Email
    print("\n4️⃣ Testing Scenario C: Plain Text Email")
    plain_sig = build_plain_signature(signature_data)
    plain_body = p_body + '\n\n' + plain_sig
    msg_plain = sender.create_email_message(
        to_email=recipient['email'],
        subject=p_subject,
        body=plain_body,
        from_name="Diksha Sharma",
        is_html=False,
        custom_logo=None
    )
    print(f"   Root Content-Type (Plain): {msg_plain.get_content_type()}")
    assert msg_plain.get_content_type() == 'text/plain'
    print("   ✅ Plain text email verified")

    print("\n🎉 ALL E2E RENDERING & MIME TESTS PASSED PERFECTLY!")

if __name__ == '__main__':
    run_tests()
