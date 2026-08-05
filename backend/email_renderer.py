"""
Email HTML & Placeholder Rendering Engine
Handles robust placeholder replacement, HTML styling, email container templates, and signature cards.
"""

import os
import re
import html
import mimetypes

def _extract_fallback_name(email_addr):
    """Extract a friendly clean name from an email address if name is absent."""
    if not email_addr or '@' not in email_addr:
        return "Valued Recipient"
    local_part = email_addr.split('@')[0]
    # Replace dots, underscores, dashes with space
    clean = re.sub(r'[\._\-\+0-9]+', ' ', local_part).strip()
    words = [w.capitalize() for w in clean.split() if len(w) > 1]
    if words:
        return " ".join(words)
    return "Valued Recipient"

def replace_placeholders(text, recipient_data=None, signature_data=None, extra_vars=None):
    """
    Replace placeholders like {{name}}, {{Name}}, {{institute}}, {{company}}, {{phone}}, etc.
    Supports case-insensitivity, whitespace variations, and rich aliasing.
    """
    if not text:
        return ""
    
    recipient = dict(recipient_data) if isinstance(recipient_data, dict) else {}
    signature = dict(signature_data) if isinstance(signature_data, dict) else {}
    extras = dict(extra_vars) if isinstance(extra_vars, dict) else {}

    # Build lowercased lookup maps
    rec_lookup = {str(k).lower().strip().replace(' ', '_'): str(v).strip() for k, v in recipient.items() if v is not None}
    sig_lookup = {str(k).lower().strip().replace(' ', '_'): str(v).strip() for k, v in signature.items() if v is not None}
    ext_lookup = {str(k).lower().strip().replace(' ', '_'): str(v).strip() for k, v in extras.items() if v is not None}

    # Helper to find value from lookup
    def get_var(*keys):
        for k in keys:
            norm_k = k.lower().strip().replace(' ', '_')
            if norm_k in rec_lookup and rec_lookup[norm_k] and rec_lookup[norm_k].lower() != 'nan':
                return rec_lookup[norm_k]
            if norm_k in ext_lookup and ext_lookup[norm_k]:
                return ext_lookup[norm_k]
            if norm_k in sig_lookup and sig_lookup[norm_k]:
                return sig_lookup[norm_k]
        return None

    # Name derivation
    raw_name = get_var('name', 'full_name', 'recipient_name', 'contact_name', 'student_name', 'customer_name', 'person_name')
    email_addr = get_var('email', 'email_address', 'recipient_email', 'mail')
    
    first_name = get_var('first_name')
    last_name = get_var('last_name')
    if raw_name:
        parts = raw_name.split()
        if not first_name and len(parts) > 0:
            first_name = parts[0]
        if not last_name and len(parts) > 1:
            last_name = " ".join(parts[1:])
    elif email_addr:
        raw_name = _extract_fallback_name(email_addr)
        first_name = raw_name.split()[0] if raw_name else "There"

    institute = get_var('institute', 'institution', 'college', 'university', 'school', 'academy') or \
                get_var('company', 'company_name', 'organization', 'org') or \
                "your institution"

    company = get_var('company', 'company_name', 'organization', 'org', 'business') or \
              get_var('institute', 'institution', 'college', 'university') or \
              "our organization"

    # Pre-built standard mapping dictionary
    mapping = {
        'name': raw_name or 'Valued Recipient',
        'full_name': raw_name or 'Valued Recipient',
        'recipient_name': raw_name or 'Valued Recipient',
        'first_name': first_name or (raw_name.split()[0] if raw_name else 'There'),
        'last_name': last_name or '',
        'email': email_addr or '',
        'recipient_email': email_addr or '',
        'institute': institute,
        'institution': institute,
        'college': institute,
        'university': institute,
        'company': company,
        'company_name': company,
        'organization': company,
        'designation': get_var('designation', 'position', 'role', 'title') or '',
        'position': get_var('position', 'designation', 'role', 'title') or '',
        'phone': get_var('phone', 'mobile', 'contact', 'telephone', 'company_phone') or '',
        'city': get_var('city', 'location') or '',
        'state': get_var('state') or '',
        'country': get_var('country') or '',
        'executive_name': get_var('executive_name') or '',
        'company_email': get_var('company_email') or '',
        'company_phone': get_var('company_phone') or '',
        'company_website': get_var('company_website') or '',
    }

    # Also include every original recipient and signature key directly
    for k, v in rec_lookup.items():
        if v and v.lower() != 'nan':
            mapping[k] = v
    for k, v in sig_lookup.items():
        if v:
            mapping[k] = v
    for k, v in ext_lookup.items():
        if v:
            mapping[k] = v

    # Regex replacement for {{ key }} or {{key}} or {{ KEY }}
    pattern = re.compile(r'\{\{\s*([a-zA-Z0-9_\s\-]+)\s*\}\}', re.IGNORECASE)

    def replacer(match):
        raw_key = match.group(1).strip()
        norm_key = raw_key.lower().replace(' ', '_')
        if norm_key in mapping:
            val = mapping[norm_key]
            # Match case if original key was capitalized
            if raw_key.isupper() and isinstance(val, str):
                return val.upper()
            if raw_key.istitle() and isinstance(val, str):
                return val.title()
            return str(val)
        
        # If not in mapping, check direct lookup
        direct = get_var(norm_key, raw_key)
        if direct is not None:
            return str(direct)
        
        # If still unmapped, fallback gracefully (e.g. empty string or clean fallback) rather than leaving ugly {{key}}
        if norm_key in ['name', 'recipient_name', 'first_name']:
            return "Valued Recipient"
        elif norm_key in ['institute', 'institution', 'college', 'university']:
            return "your esteemed institution"
        elif norm_key in ['company', 'company_name']:
            return "our organization"
        return ""

    result = pattern.sub(replacer, text)
    return result


def build_html_signature(signature_data=None, has_logo=False):
    """
    Build a clean, professional, email-client safe HTML signature block.
    Integrates the inline logo directly within the signature section only when has_logo=True.
    """
    sig = dict(signature_data) if isinstance(signature_data, dict) else {}
    
    exec_name = str(sig.get('executive_name') or '').strip()
    position = str(sig.get('position') or '').strip()
    company_name = str(sig.get('company_name') or '').strip()
    company_email = str(sig.get('company_email') or '').strip()
    company_phone = str(sig.get('company_phone') or '').strip()
    company_website = str(sig.get('company_website') or '').strip()

    # If all fields are blank, return minimal signature
    has_details = any([exec_name, position, company_name, company_email, company_phone, company_website])
    
    if not has_details and not has_logo:
        return ""

    lines = []
    lines.append('<div class="email-signature" style="margin-top: 28px; padding-top: 18px; border-top: 1px solid #e2e8f0; font-family: -apple-system, BlinkMacSystemFont, \'Segoe UI\', Roboto, Helvetica, Arial, sans-serif; color: #2d3748; line-height: 1.5;">')
    lines.append('  <p style="margin: 0 0 10px 0; font-weight: 600; color: #1a202c; font-size: 15px;">Best Regards,</p>')
    
    if exec_name or position or company_name:
        lines.append('  <div style="margin-bottom: 10px;">')
        if exec_name:
            lines.append(f'    <div style="font-size: 16px; font-weight: 700; color: #1a73e8; margin-bottom: 2px;">{html.escape(exec_name)}</div>')
        if position:
            lines.append(f'    <div style="font-size: 14px; color: #4a5568; margin-bottom: 2px;">{html.escape(position)}</div>')
        if company_name:
            lines.append(f'    <div style="font-size: 14px; font-weight: 600; color: #2d3748;">{html.escape(company_name)}</div>')
        lines.append('  </div>')

    contact_rows = []
    if company_phone:
        clean_phone = re.sub(r'[^\+0-9]', '', company_phone)
        contact_rows.append(f'<div style="margin-bottom: 4px;"><a href="tel:{clean_phone}" style="color: #4a5568; text-decoration: none; font-size: 13px;">📞 {html.escape(company_phone)}</a></div>')
    if company_email:
        contact_rows.append(f'<div style="margin-bottom: 4px;"><a href="mailto:{html.escape(company_email)}" style="color: #1a73e8; text-decoration: none; font-size: 13px;">✉️ {html.escape(company_email)}</a></div>')
    if company_website:
        web_url = company_website if company_website.startswith(('http://', 'https://')) else f'https://{company_website}'
        display_web = company_website.replace('https://', '').replace('http://', '')
        contact_rows.append(f'<div style="margin-bottom: 4px;"><a href="{html.escape(web_url)}" target="_blank" style="color: #1a73e8; text-decoration: none; font-size: 13px;">🌐 {html.escape(display_web)}</a></div>')

    if contact_rows:
        lines.append(f'  <div style="margin-bottom: 12px; font-size: 13px; color: #718096;">\n{"".join(contact_rows)}\n  </div>')

    # Inline logo embedded inside the signature block
    if has_logo:
        lines.append('  <div style="margin-top: 14px; padding-top: 6px;">')
        lines.append('    <img src="cid:company_logo" alt="Company Logo" style="max-height: 65px; max-width: 170px; width: auto; height: auto; display: block; border: 0;" />')
        lines.append('  </div>')

    lines.append('</div>')
    return "\n".join(lines)


def build_plain_signature(signature_data=None):
    """Build clean plain text signature."""
    sig = dict(signature_data) if isinstance(signature_data, dict) else {}
    exec_name = str(sig.get('executive_name') or '').strip()
    position = str(sig.get('position') or '').strip()
    company_name = str(sig.get('company_name') or '').strip()
    company_email = str(sig.get('company_email') or '').strip()
    company_phone = str(sig.get('company_phone') or '').strip()
    company_website = str(sig.get('company_website') or '').strip()

    parts = ["Best Regards,"]
    if exec_name:
        parts.append(exec_name)
    if position and company_name:
        parts.append(f"{position} | {company_name}")
    elif position:
        parts.append(position)
    elif company_name:
        parts.append(company_name)
    
    if company_phone:
        parts.append(f"Phone: {company_phone}")
    if company_email:
        parts.append(f"Email: {company_email}")
    if company_website:
        parts.append(f"Website: {company_website}")

    return "\n".join(parts)


def convert_text_to_html_paragraphs(body_text):
    """
    Converts plain text or markdown formatted body into styled HTML paragraphs.
    Handles **bold**, *italic*, lists (- / * / 1.), and line breaks.
    """
    if not body_text:
        return ""
    
    # If the content already contains rich HTML markup like <p>, <div>, <table>, preserve it
    if re.search(r'<(p|div|table|ul|ol|h[1-6])[\s>]', body_text, re.IGNORECASE):
        # Already structured HTML, convert markdown within text nodes if any
        body_text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', body_text)
        body_text = re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'<em>\1</em>', body_text)
        return body_text

    # Split into blocks by double newlines
    blocks = re.split(r'\n\s*\n', body_text.strip())
    html_blocks = []

    for block in blocks:
        block = block.strip()
        if not block:
            continue

        # Check if block is a bullet list
        lines = block.split('\n')
        if all(re.match(r'^\s*[\-\*•]\s+', line) for line in lines if line.strip()):
            list_items = []
            for line in lines:
                clean_line = re.sub(r'^\s*[\-\*•]\s+', '', line).strip()
                if clean_line:
                    # Markdown bold & italic
                    clean_line = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', clean_line)
                    clean_line = re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'<em>\1</em>', clean_line)
                    list_items.append(f'<li style="margin-bottom: 6px;">{clean_line}</li>')
            html_blocks.append(f'<ul style="margin: 0 0 16px 0; padding-left: 24px; color: #2d3748; line-height: 1.6;">\n{"".join(list_items)}\n</ul>')
            continue

        # Check if block is a numbered list
        if all(re.match(r'^\s*\d+[\.\)]\s+', line) for line in lines if line.strip()):
            list_items = []
            for line in lines:
                clean_line = re.sub(r'^\s*\d+[\.\)]\s+', '', line).strip()
                if clean_line:
                    clean_line = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', clean_line)
                    clean_line = re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'<em>\1</em>', clean_line)
                    list_items.append(f'<li style="margin-bottom: 6px;">{clean_line}</li>')
            html_blocks.append(f'<ol style="margin: 0 0 16px 0; padding-left: 24px; color: #2d3748; line-height: 1.6;">\n{"".join(list_items)}\n</ol>')
            continue

        # Normal paragraph
        # Markdown conversions
        formatted_block = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', block)
        formatted_block = re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'<em>\1</em>', formatted_block)
        
        # Auto-link raw URLs
        formatted_block = re.sub(
            r'(?<!href=["\'])(https?://[^\s<>"]+)',
            r'<a href="\1" target="_blank" style="color: #1a73e8; text-decoration: underline;">\1</a>',
            formatted_block
        )

        # Convert remaining single newlines to <br>
        formatted_block = formatted_block.replace('\n', '<br>')
        html_blocks.append(f'<p style="margin: 0 0 16px 0; line-height: 1.65; font-size: 15px; color: #2d3748;">{formatted_block}</p>')

    return "\n".join(html_blocks)


def render_full_html_email(body_text_or_html, signature_data=None, has_logo=False, subject=""):
    """
    Renders complete, responsive, email-client standard HTML document.
    Embeds body paragraphs, HTML signature with inline logo CID, and responsive card styling.
    """
    body_html = convert_text_to_html_paragraphs(body_text_or_html)
    signature_html = build_html_signature(signature_data, has_logo=has_logo)
    
    escaped_subject = html.escape(str(subject or 'Notification'))

    full_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta http-equiv="X-UA-Compatible" content="IE=edge">
  <title>{escaped_subject}</title>
  <!--[if mso]>
  <style type="text/css">
    body, table, td {{font-family: Arial, Helvetica, sans-serif !important;}}
  </style>
  <![endif]-->
</head>
<body style="margin: 0; padding: 0; background-color: #f4f6f8; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; -webkit-font-smoothing: antialiased; -webkit-text-size-adjust: 100%; color: #2d3748; line-height: 1.6;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color: #f4f6f8; width: 100%; margin: 0; padding: 28px 12px;">
    <tr>
      <td align="center" valign="top">
        <!-- Main Email Container Card -->
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width: 620px; width: 100%; background-color: #ffffff; border-radius: 8px; border: 1px solid #e2e8f0; box-shadow: 0 2px 10px rgba(0,0,0,0.04); overflow: hidden;">
          <tr>
            <td style="padding: 36px 32px 32px 32px;">
              <!-- Email Body Content -->
              <div style="font-size: 15px; line-height: 1.65; color: #2d3748;">
                {body_html}
              </div>
              
              <!-- Professional HTML Signature with Inline Logo -->
              {signature_html}
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""
    return full_html


def save_debug_html(html_content, filepath=None):
    """Save generated HTML to a file for local inspection."""
    if not filepath:
        upload_dir = os.path.join(os.path.dirname(__file__), 'uploads')
        os.makedirs(upload_dir, exist_ok=True)
        filepath = os.path.join(upload_dir, 'generated_email.html')
    
    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(html_content)
        print(f"📄 Saved generated email HTML to: {filepath}")
        return filepath
    except Exception as e:
        print(f"⚠️ Failed to save generated email HTML: {e}")
        return None
