import os
import requests
import pandas as pd

from docx import Document
from PyPDF2 import PdfReader

OLLAMA_URL = "http://localhost:11434/api/generate"

VALID_CATEGORIES = [
    "Doctor",
    "Industry",
    "Play School",
    "General"
]


# ==========================================
# EXTRACT FILE CONTENT
# ==========================================

def extract_content(file_path):

    ext = os.path.splitext(file_path)[1].lower()

    try:

        # CSV
        if ext == ".csv":

            df = pd.read_csv(file_path)

            return df.head(50).to_string()

        # Excel
        elif ext in [".xlsx", ".xls"]:

            df = pd.read_excel(file_path)

            return df.head(50).to_string()

        # TXT
        elif ext == ".txt":

            with open(
                file_path,
                "r",
                encoding="utf-8",
                errors="ignore"
            ) as f:

                return f.read(5000)

        # DOCX
        elif ext == ".docx":

            doc = Document(file_path)

            text = []

            for para in doc.paragraphs:
                if para.text.strip():
                    text.append(para.text)

            return "\n".join(text)[:5000]

        # PDF
        elif ext == ".pdf":

            reader = PdfReader(file_path)

            text = ""

            for page in reader.pages:

                page_text = page.extract_text()

                if page_text:
                    text += page_text + "\n"

            return text[:5000]

        else:

            print(f"⚠ Unsupported file type: {ext}")

            return ""

    except Exception as e:

        print("❌ Extraction Error:", e)

        return ""


# ==========================================
# CLASSIFY FILE
# ==========================================

def classify_file(file_path):

    content = extract_content(file_path)

    print("\n" + "=" * 80)
    print(f"📄 FILE: {file_path}")
    print("\n📑 EXTRACTED CONTENT:\n")

    if content:
        print(content[:1500])
    else:
        print("❌ No content extracted")

    print("=" * 80)

    if not content:
        return "General"

    content_lower = content.lower()

    # ======================================
    # DOCTOR
    # ======================================

    doctor_keywords = [
        "doctor",
        "dr",
        "hospital",
        "clinic",
        "physician",
        "medical",
        "medicine",
        "healthcare",
        "surgeon",
        "dentist",
        "cardiologist",
        "neurologist",
        "orthopedic",
        "mbbs",
        "md"
    ]

    if any(word in content_lower for word in doctor_keywords):

        print("✅ Keyword Match -> Doctor")

        return "Doctor"

    # ======================================
    # INDUSTRY
    # ======================================

    industry_keywords = [
        "industry",
        "industrial",
        "manufacturing",
        "factory",
        "production",
        "machinery",
        "engineering",
        "plant",
        "equipment",
        "automation",
        "supply chain",
        "assembly"
    ]

    if any(word in content_lower for word in industry_keywords):

        print("✅ Keyword Match -> Industry")

        return "Industry"

    # ======================================
    # PLAY SCHOOL
    # ======================================

    school_keywords = [
        "play school",
        "playschool",
        "preschool",
        "pre school",
        "kindergarten",
        "nursery",
        "daycare",
        "day care",
        "children",
        "kids",
        "admission",
        "early education",
        "child care"
    ]

    if any(word in content_lower for word in school_keywords):

        print("✅ Keyword Match -> Play School")

        return "Play School"

    # ======================================
    # OLLAMA FALLBACK
    # ======================================

    prompt = f"""
You are a file classifier.

Possible categories:

Doctor
Industry
Play School
General

Analyze the document content.

Return ONLY one category.

Document:

{content[:4000]}
"""

    try:

        print("🤖 Sending to Ollama...")

        response = requests.post(
            OLLAMA_URL,
            json={
                "model": "llama3",
                "prompt": prompt,
                "stream": False
            },
            timeout=120
        )

        result = response.json()

        category = result.get(
            "response",
            ""
        ).strip()

        category = category.replace("\n", "").strip()

        print(f"🤖 Ollama Returned: {category}")

        if category not in VALID_CATEGORIES:

            print("⚠ Invalid Category Returned")
            category = "General"

        return category

    except Exception as e:

        print("❌ Ollama Error:", e)

        return "General"


# ==========================================
# BACKGROUND CLASSIFICATION
# ==========================================

def classify_file_background(file_path, filename):

    try:

        print("\n" + "=" * 80)
        print("🚀 BACKGROUND CLASSIFICATION STARTED")
        print(f"📄 Filename: {filename}")
        print(f"📂 Path: {file_path}")

        category = classify_file(file_path)

        print(f"🎯 Category Returned: {category}")
        print("=" * 80)

        return category

    except Exception as e:

        print(f"❌ Classification Error: {e}")
        return "General"