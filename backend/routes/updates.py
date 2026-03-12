from flask import Blueprint, jsonify
import subprocess

updates_bp = Blueprint('updates', __name__, url_prefix='/api')

@updates_bp.route('/updates')
def get_updates():
    """Fetch top 10 recent git commits: title + date only."""
    try:
        # Simple commit log: subject | date
        result = subprocess.run([
            'git', 'log', '--pretty=format:%s|%ad', '-10',
            '--date=format:%Y-%m-%d %H:%M:%S'
        ], capture_output=True, text=True, cwd='d:/Users/admin/Email', check=True)
        
        commits = []
        for line in result.stdout.strip().split('\n'):
            if '|' in line:
                title, date_str = line.split('|', 1)
                commits.append({
                    'title': title.strip(),
                    'date': date_str.strip()
                })
        
        return jsonify({
            'success': True,
            'count': len(commits),
            'updates': commits  # newest first
        })
    
    except subprocess.CalledProcessError:
        return jsonify({'success': False, 'error': 'Git repo access failed'}), 500
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
