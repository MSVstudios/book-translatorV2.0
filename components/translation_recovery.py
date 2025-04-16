import sqlite3
from typing import List, Dict

class TranslationRecovery:
    def __init__(self, db_path: str):
        self.db_path = db_path
        
    def get_failed_translations(self) -> List[Dict]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute('''
                SELECT * FROM translations 
                WHERE status = 'error'
                ORDER BY created_at DESC
            ''')
            return [dict(row) for row in cur.fetchall()]
        
    def retry_translation(self, translation_id: int):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''
                UPDATE translations
                SET status = 'pending', progress = 0, error_message = NULL,
                    current_chunk = 0, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            ''', (translation_id,))
            
            conn.execute('''
                UPDATE chunks
                SET status = 'pending', error_message = NULL
                WHERE translation_id = ? AND status = 'error'
            ''', (translation_id,))
            
    def cleanup_failed_translations(self, days: int = 7):
        with sqlite3.connect(self.db_path) as conn:
            # Use direct string formatting for date arithmetic since SQLite's
            # datetime() function doesn't accept parameters for interval
            conn.execute(
                "DELETE FROM translations WHERE status = 'error' AND created_at < datetime('now', ?)",
                (f"-{days} days",)
            )