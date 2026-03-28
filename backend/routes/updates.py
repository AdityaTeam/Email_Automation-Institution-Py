from flask import Blueprint, jsonify
import subprocess
from datetime import datetime
from flask import render_template

updates_bp = Blueprint('updates', __name__)

@updates_bp.route('/updates')
def updates_page():
    return render_template('updates.html')

@updates_bp.route('/api/updates', methods=['GET'])
def get_updates():
    try:
        import os

        repo_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../"))

        print("Using repo path:", repo_path)

        if not os.path.exists(repo_path):
            return jsonify({"success": False, "error": "Repo path not found"})

        result = subprocess.check_output(
            ['git', 'log', '-10', '--pretty=format:%h|%ad|%s', '--date=iso'],
            cwd=repo_path,
            stderr=subprocess.STDOUT
        ).decode('utf-8')

        print("RAW GIT OUTPUT:\n", result)
        updates = []

        if not result.strip():
            return jsonify({"success": True, "updates": []})

        for line in result.split('\n'):
            parts = line.split('|')
            if len(parts) < 3:
                continue

            commit_hash, date_str, message = parts

            # Convert date properly
            try:
                date_obj = datetime.fromisoformat(date_str.strip())
                formatted_date = date_obj.strftime('%B %d, %Y %I:%M %p')
            except:
                formatted_date = date_str

            updates.append({
                "title": message,
                "date": formatted_date,
                "hash": commit_hash,
                "description": message,
                "changes": [message]
            })

        return jsonify({"success": True, "updates": updates})

    except Exception as e:
        return jsonify({"success": False, "error": str(e)})
