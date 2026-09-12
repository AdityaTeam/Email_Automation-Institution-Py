import os
import sys

# Add backend directory to sys path so we can import properly
sys.path.insert(0, os.path.join(os.getcwd(), 'backend'))

from email_sender import EmailSender
from email_renderer import render_full_html_email

def run():
    sender = EmailSender(email_accounts=[{"email": "test@example.com", "smtp_server": "smtp.gmail.com", "smtp_port": 587, "password": "dummy"}])
    sender.connected_account_email = "test@example.com"
    
    body = """Hi Neha,
I hope you are doing well.
I’m reaching out from Hansraj Ventures Private Limited. We support businesses, startups, and agencies with affordable digital solutions, including website and application development, UI/UX design, and digital marketing services.

Our team works on custom requirements at flexible and budget-friendly costs, ensuring scalable and practical solutions based on your exact needs. Whether it’s a new website, application development, redesign, or marketing support, we are open to working with different project scopes and budgets.

If you have any requirements or would like to explore collaboration, I would be happy to connect and discuss further.
Looking forward to hearing from you."""

    signature = {
        'name': 'angshu Biswas',
        'designation': 'intern',
        'company': 'outlier, Freelancer',
        'phone': '+918101498972',
        'email': '404munna@gmail.com',
        'website': 'https://AB works'
    }
    
    html_body = render_full_html_email(body, signature, has_logo=False, subject="Cost-Effective Digital Development & Marketing Support")
    
    msg = sender.create_email_message(
        to_email="recipient@example.com",
        subject="Cost-Effective Digital Development & Marketing Support | Ref:20260805105232-0003",
        body=html_body,
        from_name="Angshu Biswas",
        is_html=True
    )
    
    raw_msg = msg.as_string()
    with open("raw_test_email.eml", "w", encoding="utf-8") as f:
        f.write(raw_msg)
        
    print("Saved raw email to raw_test_email.eml")

if __name__ == "__main__":
    run()
