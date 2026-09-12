"""
Banner Image Rendering Engine
Converts structured content blocks (Image, Heading, Text, Divider, Button, Image+Text)
into a single high-quality visual PNG representation using Python Pillow (PIL).
"""

import os
import io
import re
import urllib.request
from PIL import Image, ImageDraw, ImageFont

# Set up upload folder for rendered banners
BANNER_UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), 'uploads', 'banners')
os.makedirs(BANNER_UPLOAD_FOLDER, exist_ok=True)


def get_font(size=16, is_bold=False):
    """Attempt to load a clean TTF font, falling back to PIL default font"""
    font_paths = [
        "C:\\Windows\\Fonts\\Nirmala.ttf",
        "C:\\Windows\\Fonts\\segoeui.ttf",
        "C:\\Windows\\Fonts\\arial.ttf",
        "C:\\Windows\\Fonts\\calibri.ttf",
        "arial.ttf", "arialbd.ttf" if is_bold else "arial.ttf",
        "DejaVuSans.ttf", "DejaVuSans-Bold.ttf" if is_bold else "DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    ]
    for path in font_paths:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def hex_to_rgb(hex_str, default=(51, 51, 51)):
    """Convert hex color string to RGB tuple"""
    if not hex_str or not isinstance(hex_str, str):
        return default
    hex_clean = hex_str.lstrip('#')
    if len(hex_clean) == 3:
        hex_clean = ''.join([c * 2 for c in hex_clean])
    if len(hex_clean) == 6:
        try:
            return (int(hex_clean[0:2], 16), int(hex_clean[2:4], 16), int(hex_clean[4:6], 16))
        except ValueError:
            return default
    return default


def create_placeholder_image(w, h, label="Image Placeholder"):
    """Create a clean placeholder image canvas when no custom image is loaded"""
    w = max(int(w), 50)
    h = max(int(h), 50)
    img = Image.new('RGBA', (w, h), (240, 244, 248, 255))
    draw = ImageDraw.Draw(img)
    draw.rectangle([(0, 0), (w - 1, h - 1)], outline=(200, 208, 218), width=2)
    font = get_font(13, is_bold=True)
    try:
        bbox = draw.textbbox((0, 0), label, font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
    except Exception:
        tw, th = len(label) * 8, 14
    tx = max(0, (w - tw) // 2)
    ty = max(0, (h - th) // 2)
    draw.text((tx, ty), label, font=font, fill=(130, 140, 155))
    return img


def resolve_image_path(src):
    """Resolve browser image URL or relative path to absolute file system path for Pillow"""
    if not src or not isinstance(src, str):
        return None
    src = src.strip()
    if not src:
        return None
    
    # 1. Base64 Data URL
    if src.startswith('data:image'):
        try:
            header, base64_data = src.split(',', 1)
            import base64
            img_data = base64.b64decode(base64_data)
            return Image.open(io.BytesIO(img_data))
        except Exception as e:
            print(f"[WARNING] Failed to decode base64 image data: {e}")
            return None

    # 2. Absolute HTTP/HTTPS URL
    if src.startswith(('http://', 'https://')):
        try:
            req = urllib.request.Request(src, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=10) as resp:
                return Image.open(io.BytesIO(resp.read()))
        except Exception as e:
            print(f"[WARNING] Failed to fetch remote image URL '{src}': {e}")
            return None

    # 3. Local filesystem / uploads path resolution
    clean_filename = os.path.basename(src.split('?')[0])
    BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
    
    possible_paths = [
        os.path.join(BANNER_UPLOAD_FOLDER, clean_filename),
        os.path.join(os.path.dirname(__file__), 'uploads', 'banners', clean_filename),
        os.path.join(BASE_DIR, 'uploads', 'banners', clean_filename),
        os.path.join(BASE_DIR, 'backend', 'uploads', 'banners', clean_filename),
        src.lstrip('/\\'),
        os.path.join(os.path.dirname(__file__), src.lstrip('/\\')),
        os.path.join(BASE_DIR, src.lstrip('/\\')),
        os.path.abspath(src)
    ]
    
    for p in possible_paths:
        if os.path.isfile(p):
            try:
                img_obj = Image.open(p)
                img_obj.load()  # verify image data can be loaded
                return img_obj
            except Exception as load_err:
                print(f"[WARNING] Image open error for path '{p}': {load_err}")
                continue

    print(f"[WARNING] Could not resolve image path for '{src}'")
    return None


def paste_image_to_canvas(canvas, img_obj, box):
    """Safely paste any PIL image (RGBA/RGB/L/P) onto RGB canvas without Pillow mask exceptions"""
    try:
        x, y = box
        w, h = img_obj.size
        img_rgba = img_obj.convert('RGBA')
        bg_patch = Image.new('RGBA', (w, h), (255, 255, 255, 255))
        composite_patch = Image.alpha_composite(bg_patch, img_rgba).convert('RGB')
        canvas.paste(composite_patch, (x, y))
    except Exception as e:
        print(f"[WARNING] Safe image paste fallback error: {e}")
        try:
            canvas.paste(img_obj.convert('RGB'), box)
        except Exception:
            pass


def wrap_text(text, font, max_width, draw):
    """Wrap text to fit within specified max_width"""
    if not text:
        return []
    lines = []
    paragraphs = str(text).split('\n')
    
    for para in paragraphs:
        if not para.strip():
            lines.append("")
            continue
        words = para.split(' ')
        current_line = []
        
        for word in words:
            test_line = ' '.join(current_line + [word])
            try:
                bbox = draw.textbbox((0, 0), test_line, font=font)
                line_width = bbox[2] - bbox[0]
            except Exception:
                line_width = len(test_line) * 10
            
            if line_width <= max_width or not current_line:
                current_line.append(word)
            else:
                lines.append(' '.join(current_line))
                current_line = [word]
        if current_line:
            lines.append(' '.join(current_line))
            
    return lines


def render_banner_to_image(banner_data, output_filename=None):
    """
    Renders structured JSON block data into a single PNG image.
    Supports: heading, text, image, image_text (side-by-side), divider, button.
    """
    canvas_width = int(banner_data.get('width', 650))
    bg_color_hex = banner_data.get('bgColor', '#ffffff')
    bg_rgb = hex_to_rgb(bg_color_hex, (255, 255, 255))
    padding_x = 30
    content_max_width = canvas_width - (padding_x * 2)

    blocks = banner_data.get('blocks', [])

    print(f"[BANNER RENDER] Starting render for campaign '{banner_data.get('title')}' with {len(blocks)} blocks:")

    # First pass: calculate total height needed
    dummy_img = Image.new('RGB', (canvas_width, 100), bg_rgb)
    dummy_draw = ImageDraw.Draw(dummy_img)

    computed_blocks = []
    total_height = 40  # top and bottom padding

    for idx, block in enumerate(blocks, 1):
        raw_type = str(block.get('type', 'text')).lower().strip()
        align = str(block.get('align', 'left')).lower().strip()
        if align not in ['left', 'center', 'right']:
            align = 'left'

        clean_type = raw_type.replace('-', '_').replace(' ', '_').replace('+', '_')
        if ('image' in clean_type and 'text' in clean_type) or ('img' in clean_type and 'text' in clean_type) or clean_type in ['image_text', 'image_text_block', 'text_image', 'side_by_side', 'imagetext', 'image_and_text']:
            b_type = 'image_text'
        elif 'heading' in clean_type or clean_type in ['h1', 'h2', 'h3']:
            b_type = 'heading'
        elif 'text' in clean_type or clean_type in ['paragraph', 'p']:
            b_type = 'text'
        elif 'image' in clean_type or 'img' in clean_type:
            b_type = 'image'
        elif 'divider' in clean_type or clean_type in ['hr', 'line']:
            b_type = 'divider'
        elif 'button' in clean_type or clean_type in ['cta', 'link']:
            b_type = 'button'
        else:
            b_type = 'text'

        print(f"[BANNER RENDER DEBUG #{idx}] raw='{block.get('type')}' -> clean='{clean_type}' -> normalized='{b_type}' | align={align} | layout={block.get('layout')}")

        if b_type == 'heading':
            level = str(block.get('size', 'h1')).lower().strip()
            font_size = 28 if level == 'h1' else (22 if level == 'h2' else 18)
            font = get_font(font_size, is_bold=True)
            text = str(block.get('content', '') or '').strip()
            color = hex_to_rgb(block.get('color', '#1a202c'))
            
            lines = wrap_text(text, font, content_max_width, dummy_draw)
            line_height = font_size + 6
            block_h = len(lines) * line_height + 20
            
            computed_blocks.append({
                'type': 'heading',
                'lines': lines,
                'font': font,
                'font_size': font_size,
                'line_height': line_height,
                'color': color,
                'align': align,
                'height': block_h
            })
            total_height += block_h

        elif b_type == 'text':
            font_size = 15
            font = get_font(font_size, is_bold=False)
            text = str(block.get('content', '') or '').strip()
            color = hex_to_rgb(block.get('color', '#2d3748'))
            
            lines = wrap_text(text, font, content_max_width, dummy_draw)
            line_height = font_size + 6
            block_h = len(lines) * line_height + 20
            
            computed_blocks.append({
                'type': 'text',
                'lines': lines,
                'font': font,
                'font_size': font_size,
                'line_height': line_height,
                'color': color,
                'align': align,
                'height': block_h
            })
            total_height += block_h

        elif b_type == 'image':
            src = str(block.get('source', '') or block.get('image', '') or '').strip()
            img_obj = resolve_image_path(src)

            if not img_obj:
                img_obj = create_placeholder_image(content_max_width, 160, "Image Placeholder")

            orig_w, orig_h = img_obj.size
            if orig_w > content_max_width:
                ratio = content_max_width / float(orig_w)
                target_w = content_max_width
                target_h = int(orig_h * ratio)
            else:
                target_w = orig_w
                target_h = orig_h
            
            block_h = target_h + 20
            computed_blocks.append({
                'type': 'image',
                'image': img_obj,
                'width': target_w,
                'height': target_h,
                'block_height': block_h,
                'align': align
            })
            total_height += block_h

        elif b_type == 'image_text':
            layout_raw = str(block.get('layout', 'image_left')).lower().strip()
            is_image_right = ('right' in layout_raw and 'image' in layout_raw) or ('left' in layout_raw and 'text' in layout_raw) or layout_raw in ['image_right', 'text_left_image_right', 'right']
            layout = 'image_right' if is_image_right else 'image_left'

            text = str(block.get('content', '') or block.get('text', '') or 'Side text content alongside image...').strip()
            src = str(block.get('source', '') or block.get('image', '') or '').strip()
            color = hex_to_rgb(block.get('color', '#2d3748'))
            font_size = 15
            font = get_font(font_size, is_bold=False)

            img_obj = resolve_image_path(src)

            col_gap = 20
            img_target_w = int(content_max_width * 0.4)
            text_target_w = content_max_width - img_target_w - col_gap

            if not img_obj:
                img_obj = create_placeholder_image(img_target_w, 140, "Image Side")

            orig_w, orig_h = img_obj.size
            ratio = img_target_w / float(orig_w) if orig_w else 1.0
            img_w = img_target_w
            img_h = int(orig_h * ratio)
            if img_h > 260:
                ratio_h = 260.0 / float(img_h)
                img_h = 260
                img_w = int(img_w * ratio_h)

            text_lines = wrap_text(text, font, text_target_w, dummy_draw) if text else []
            line_height = font_size + 6
            text_h = len(text_lines) * line_height

            block_h = max(img_h, text_h, 40) + 20
            computed_blocks.append({
                'type': 'image_text',
                'layout': layout,
                'image': img_obj,
                'img_w': img_w,
                'img_h': img_h,
                'lines': text_lines,
                'font': font,
                'line_height': line_height,
                'text_w': text_target_w,
                'color': color,
                'align': align,
                'col_gap': col_gap,
                'height': block_h
            })
            total_height += block_h

        elif b_type == 'divider':
            color = hex_to_rgb(block.get('color', '#e2e8f0'))
            block_h = 24
            computed_blocks.append({
                'type': 'divider',
                'color': color,
                'height': block_h
            })
            total_height += block_h

        elif b_type == 'button':
            text = str(block.get('content', 'Click Here') or 'Click Here').strip()
            bg_b_color = hex_to_rgb(block.get('bgColor', '#1a73e8'))
            text_color = hex_to_rgb(block.get('textColor', '#ffffff'))
            font = get_font(16, is_bold=True)
            
            try:
                bbox = dummy_draw.textbbox((0, 0), text, font=font)
                btn_text_w = bbox[2] - bbox[0]
            except Exception:
                btn_text_w = len(text) * 10
            btn_w = btn_text_w + 40
            btn_h = 44
            block_h = btn_h + 30

            computed_blocks.append({
                'type': 'button',
                'text': text,
                'bg_color': bg_b_color,
                'text_color': text_color,
                'font': font,
                'btn_width': btn_w,
                'btn_height': btn_h,
                'align': align,
                'height': block_h
            })
            total_height += block_h

    # Ensure minimum height
    total_height = max(total_height, 100)

    print(f"[BANNER RENDER PIPELINE] Total canvas height calculated: {total_height}px across {len(computed_blocks)} blocks")

    # Render final canvas
    canvas = Image.new('RGB', (canvas_width, total_height), bg_rgb)
    draw = ImageDraw.Draw(canvas)

    current_y = 20

    for idx, cb in enumerate(computed_blocks, 1):
        b_type = cb['type']
        print(f"  [DRAWING BLOCK #{idx}] type={b_type} | y={current_y} | h={cb['height']}")

        if b_type in ['heading', 'text']:
            font = cb['font']
            line_height = cb['line_height']
            color = cb['color']
            align = cb['align']

            for line in cb['lines']:
                try:
                    bbox = draw.textbbox((0, 0), line, font=font)
                    line_w = bbox[2] - bbox[0]
                except Exception:
                    line_w = len(line) * 9
                
                if align == 'center':
                    x = padding_x + (content_max_width - line_w) // 2
                elif align == 'right':
                    x = padding_x + content_max_width - line_w
                else:
                    x = padding_x
                
                draw.text((x, current_y), line, font=font, fill=color)
                current_y += line_height
            current_y += 10

        elif b_type == 'image':
            align = cb['align']
            target_w = cb['width']
            target_h = cb['height']

            if align == 'center':
                x = padding_x + (content_max_width - target_w) // 2
            elif align == 'right':
                x = padding_x + content_max_width - target_w
            else:
                x = padding_x

            resized_img = cb['image'].resize((target_w, target_h), Image.Resampling.LANCZOS)
            paste_image_to_canvas(canvas, resized_img, (x, current_y))
            current_y += cb['block_height']

        elif b_type == 'image_text':
            layout = cb['layout']
            img_obj = cb['image']
            img_w = cb['img_w']
            img_h = cb['img_h']
            lines = cb['lines']
            font = cb['font']
            line_height = cb['line_height']
            color = cb['color']
            col_gap = cb['col_gap']
            align = cb['align']

            if layout == 'image_left':  # Image Left + Text Right
                img_x = padding_x
                text_x_base = padding_x + img_w + col_gap
            else:  # Text Left + Image Right
                text_x_base = padding_x
                img_x = padding_x + content_max_width - img_w

            if img_obj and img_w > 0 and img_h > 0:
                resized_img = img_obj.resize((img_w, img_h), Image.Resampling.LANCZOS)
                paste_image_to_canvas(canvas, resized_img, (img_x, current_y))

            text_y = current_y
            for line in lines:
                try:
                    bbox = draw.textbbox((0, 0), line, font=font)
                    line_w = bbox[2] - bbox[0]
                except Exception:
                    line_w = len(line) * 8
                
                if align == 'center':
                    x = text_x_base + max(0, (cb['text_w'] - line_w) // 2)
                elif align == 'right':
                    x = text_x_base + max(0, cb['text_w'] - line_w)
                else:
                    x = text_x_base
                draw.text((x, text_y), line, font=font, fill=color)
                text_y += line_height

            current_y += cb['height']

        elif b_type == 'divider':
            line_y = current_y + 12
            draw.line([(padding_x, line_y), (padding_x + content_max_width, line_y)], fill=cb['color'], width=2)
            current_y += cb['height']

        elif b_type == 'button':
            btn_w = cb['btn_width']
            btn_h = cb['btn_height']
            align = cb['align']

            if align == 'center':
                x1 = padding_x + (content_max_width - btn_w) // 2
            elif align == 'right':
                x1 = padding_x + content_max_width - btn_w
            else:
                x1 = padding_x

            y1 = current_y + 5
            x2 = x1 + btn_w
            y2 = y1 + btn_h

            draw.rounded_rectangle([x1, y1, x2, y2], radius=6, fill=cb['bg_color'])
            
            try:
                bbox = draw.textbbox((0, 0), cb['text'], font=cb['font'])
                text_w = bbox[2] - bbox[0]
                text_h = bbox[3] - bbox[1]
            except Exception:
                text_w, text_h = len(cb['text']) * 9, 16
            
            text_x = x1 + (btn_w - text_w) // 2
            text_y = y1 + (btn_h - text_h) // 2 - 2

            draw.text((text_x, text_y), cb['text'], font=cb['font'], fill=cb['text_color'])
            current_y += cb['height']

    if not output_filename:
        import uuid
        output_filename = f"rendered_banner_{uuid.uuid4().hex[:10]}.png"

    full_output_path = os.path.join(BANNER_UPLOAD_FOLDER, output_filename)
    canvas.save(full_output_path, 'PNG', quality=95)
    print(f"[SUCCESS] Banner image rendered and saved: {full_output_path} ({canvas_width}x{total_height}px)")
    
    return {
        'filepath': full_output_path,
        'filename': output_filename,
        'url': f"/uploads/banners/{output_filename}",
        'width': canvas_width,
        'height': total_height
    }
