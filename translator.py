import json
import requests
import time
from typing import List, Dict, Optional, Tuple, Callable
from tqdm import tqdm
import os
import sqlite3
from datetime import datetime, timedelta
import logging
from logging.handlers import RotatingFileHandler
import hashlib
import traceback
import psutil
import threading
from collections import deque
from dataclasses import dataclass, field
from functools import wraps
from flask import Flask, request, jsonify, Response, send_file, send_from_directory
from flask_cors import CORS
from werkzeug.utils import secure_filename
from deep_translator import GoogleTranslator

# init FLASK
app = Flask(__name__) 
CORS(app)

# Folders setup
UPLOAD_FOLDER = 'uploads'
TRANSLATIONS_FOLDER = 'translations'
STATIC_FOLDER = 'static'
LOG_FOLDER = 'logs'
DB_FOLDER = 'db'
DB_PATH = DB_FOLDER + '/translations.db'
CACHE_DB_PATH = DB_FOLDER + '/cache.db'

# Create necessary directories
for folder in [UPLOAD_FOLDER, TRANSLATIONS_FOLDER, STATIC_FOLDER, LOG_FOLDER, DB_FOLDER]:
    os.makedirs(folder, exist_ok=True)

# Logger setup
class AppLogger:
    def __init__(self, log_dir='logs'):
        self.log_dir = log_dir
        os.makedirs(log_dir, exist_ok=True)
        
        self.app_logger = self._setup_logger(
            'app_logger',
            os.path.join(log_dir, 'app.log')
        )
        
        self.translation_logger = self._setup_logger(
            'translation_logger',
            os.path.join(log_dir, 'translations.log')
        )
        
        self.api_logger = self._setup_logger(
            'api_logger',
            os.path.join(log_dir, 'api.log')
        )

    def _setup_logger(self, name, log_file):
        logger = logging.getLogger(name)
        logger.setLevel(logging.INFO)
        
        handler = RotatingFileHandler(
            log_file,
            maxBytes=10*1024*1024,
            backupCount=5,
            encoding='utf-8'
        )
        
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        handler.setFormatter(formatter)
        
        logger.addHandler(handler)
        return logger

# Initialize logger
logger = AppLogger()

# Monitoring setup
@dataclass
class TranslationMetrics:
    total_requests: int = 0
    successful_translations: int = 0
    failed_translations: int = 0
    average_translation_time: float = 0
    translation_times: deque = field(default_factory=lambda: deque(maxlen=100))

class AppMonitor:
    def __init__(self):
        self.metrics = TranslationMetrics()
        self._lock = threading.Lock()
        self.start_time = time.time()
        
    def record_translation_attempt(self, success: bool, translation_time: float):
        with self._lock:
            self.metrics.total_requests += 1
            if success:
                self.metrics.successful_translations += 1
                self.metrics.translation_times.append(translation_time)
                self.metrics.average_translation_time = (
                    sum(self.metrics.translation_times) / len(self.metrics.translation_times)
                )
            else:
                self.metrics.failed_translations += 1
    
    def get_system_metrics(self) -> Dict:
        return {
            'cpu_percent': psutil.cpu_percent(),
            'memory_percent': psutil.virtual_memory().percent,
            'disk_usage': psutil.disk_usage('/').percent,
            'uptime': time.time() - self.start_time
        }
    
    def get_metrics(self) -> Dict:
        with self._lock:
            metrics_data = {
                'translation_metrics': {
                    'total_requests': self.metrics.total_requests,
                    'successful_translations': self.metrics.successful_translations,
                    'failed_translations': self.metrics.failed_translations,
                    'average_translation_time': self.metrics.average_translation_time
                },
                'system_metrics': self.get_system_metrics()
            }
            
            # Calculate success rate only if there are requests
            if self.metrics.total_requests > 0:
                metrics_data['translation_metrics']['success_rate'] = (
                    self.metrics.successful_translations / self.metrics.total_requests * 100
                )
            else:
                metrics_data['translation_metrics']['success_rate'] = 0
                
            return metrics_data

# Initialize monitor
monitor = AppMonitor()

# Translation cache setup
class TranslationCache:
    def __init__(self, db_path: str = CACHE_DB_PATH):
        self.db_path = db_path
        self._init_cache_db()
    
    def _init_cache_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS translation_cache (
                    hash_key TEXT PRIMARY KEY,
                    source_lang TEXT,
                    target_lang TEXT,
                    original_text TEXT,
                    translated_text TEXT,
                    machine_translation TEXT,
                    created_at TIMESTAMP,
                    last_used TIMESTAMP
                )
            ''')

    def _generate_hash(self, text: str, source_lang: str, target_lang: str) -> str:
        key = f"{text}:{source_lang}:{target_lang}".encode('utf-8')
        return hashlib.sha256(key).hexdigest()
    
    def get_cached_translation(self, text: str, source_lang: str, target_lang: str) -> Optional[Dict[str, str]]:
        hash_key = self._generate_hash(text, source_lang, target_lang)
        
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute('''
                SELECT translated_text, machine_translation
                FROM translation_cache
                WHERE hash_key = ?
            ''', (hash_key,))
            
            result = cur.fetchone()
            if result:
                conn.execute('''
                    UPDATE translation_cache
                    SET last_used = CURRENT_TIMESTAMP
                    WHERE hash_key = ?
                ''', (hash_key,))
                return {
                    'translated_text': result[0],
                    'machine_translation': result[1]
                }
        
        return None
    
    def cache_translation(self, text: str, translated_text: str, machine_translation: str, 
                         source_lang: str, target_lang: str):
        hash_key = self._generate_hash(text, source_lang, target_lang)
        
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''
                INSERT OR REPLACE INTO translation_cache
                (hash_key, source_lang, target_lang, original_text, translated_text, 
                 machine_translation, created_at, last_used)
                VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ''', (hash_key, source_lang, target_lang, text, translated_text, machine_translation))
    
    def cleanup_old_entries(self, days: int = 30):
        with sqlite3.connect(self.db_path) as conn:
            # Use direct string formatting for date arithmetic since SQLite's 
            # datetime() function doesn't accept parameters for interval
            conn.execute(
                "DELETE FROM translation_cache WHERE last_used < datetime('now', ?)",
                (f"-{days} days",)
            )

# Initialize cache
cache = TranslationCache()

# Error handling setup
class TranslationError(Exception):
    pass

def with_error_handling(f: Callable):
    @wraps(f)
    def wrapper(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except requests.Timeout as e:
            logger.app_logger.error(f"Timeout error: {str(e)}")
            raise TranslationError("Translation service timeout")
        except requests.RequestException as e:
            logger.app_logger.error(f"Request error: {str(e)}")
            raise TranslationError("Translation service unavailable")
        except sqlite3.Error as e:
            logger.app_logger.error(f"Database error: {str(e)}")
            raise TranslationError("Database error occurred")
        except Exception as e:
            logger.app_logger.error(f"Unexpected error: {str(e)}\n{traceback.format_exc()}")
            raise TranslationError("An unexpected error occurred")
    return wrapper

# Initialize database
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        # Drop existing tables if they exist and recreate them
        conn.executescript('''
            DROP TABLE IF EXISTS chunks;
            DROP TABLE IF EXISTS translations;
        
            CREATE TABLE translations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT NOT NULL,
                source_lang TEXT NOT NULL,
                target_lang TEXT NOT NULL,
                model TEXT NOT NULL,
                status TEXT NOT NULL,
                progress REAL DEFAULT 0,
                current_chunk INTEGER DEFAULT 0,
                total_chunks INTEGER DEFAULT 0,
                original_text TEXT,
                machine_translation TEXT,
                translated_text TEXT,
                detected_language TEXT,
                genre TEXT DEFAULT 'unknown',  -- Added genre with default value
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                error_message TEXT,
                llm_refine BOOLEAN DEFAULT TRUE  -- ADDED llm_refine
            );
        
            CREATE TABLE chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                translation_id INTEGER,
                chunk_number INTEGER,
                original_text TEXT,
                machine_translation TEXT,
                translated_text TEXT,
                status TEXT,
                error_message TEXT,
                attempts INTEGER DEFAULT 0,
                FOREIGN KEY (translation_id) REFERENCES translations (id)
            );
        ''')

init_db()

class BookTranslator:
    def __init__(self, model_name: str = "aya-expanse:32b", chunk_size: int = 1000, llm_refine: bool = True):
        self.model_name = model_name
        self.api_url = "http://localhost:11434/api/generate"
        self.chunk_size = chunk_size
        self.session = requests.Session()
        self.session.mount('http://', requests.adapters.HTTPAdapter(
            max_retries=3,
            pool_connections=10,
            pool_maxsize=10
        ))
        self.llm_refine = llm_refine # add llm_refine

    def split_into_chunks(self, text: str) -> list:
        """Split text into smaller chunks for translation."""
        MAX_LENGTH = 4500  # Google Translate limit
        paragraphs = text.split('\n\n')
        chunks = []
        current_chunk = []
        current_length = 0
        
        for paragraph in paragraphs:
            if len(paragraph) + current_length > MAX_LENGTH:
                if current_chunk:
                    chunks.append('\n\n'.join(current_chunk))
                    current_chunk = []
                    current_length = 0
                
                # Split long paragraphs if needed
                if len(paragraph) > MAX_LENGTH:
                    sentences = paragraph.split('. ')
                    temp_chunk = []
                    temp_length = 0
                    
                    for sentence in sentences:
                        if temp_length + len(sentence) > MAX_LENGTH:
                            if temp_chunk:
                                chunks.append('. '.join(temp_chunk) + '.')
                                temp_chunk = []
                                temp_length = 0
                        temp_chunk.append(sentence)
                        temp_length += len(sentence) + 2  # +2 for '. '
                        
                    if temp_chunk:
                        chunks.append('. '.join(temp_chunk) + '.')
                else:
                    current_chunk.append(paragraph)
                    current_length = len(paragraph)
            else:
                current_chunk.append(paragraph)
                current_length += len(paragraph) + 2  # +2 for '\n\n'
                
        if current_chunk:
            chunks.append('\n\n'.join(current_chunk))
            
        return chunks

    def translate_text(self, text: str, source_lang: str, target_lang: str, translation_id: int):
        start_time = time.time()
        success = False
        
        try:
            chunks = self.split_into_chunks(text)
            total_chunks = len(chunks)
            translated_chunks = []
            machine_translations = []
            
            logger.translation_logger.info(f"Starting translation {translation_id} with {total_chunks} chunks")
            
            # Initialize Google translator
            translator = GoogleTranslator(
                source=source_lang if source_lang != 'auto' else 'auto',
                target=target_lang
            )

            
            # Update database with total chunks
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute('''
                    UPDATE translations 
                    SET total_chunks = ?, status = 'in_progress'
                    WHERE id = ?
                ''', (total_chunks * 2, translation_id))
            
            for i, chunk in enumerate(chunks, 1):
                try:
                    # Check cache first
                    cached_result = cache.get_cached_translation(chunk, source_lang, target_lang)
                    if cached_result:
                        machine_translations.append(cached_result['machine_translation'])
                        translated_chunks.append(cached_result['translated_text'])
                        logger.translation_logger.info(f"Cache hit for chunk {i}")
                    else:
                        # Stage 1: Google Translate
                        logger.translation_logger.info(f"Translating chunk {i}/{total_chunks}")
                        google_translation = translator.translate(chunk)

                        logger.translation_logger.info(f"Google translation for chunk {i}: {google_translation}")

                        machine_translations.append(google_translation)
                        
                        progress = (i / (total_chunks * 2)) * 100
                        yield {
                            'progress': progress,
                            'stage': 'machine_translation',
                            'machine_translation': '\n\n'.join(machine_translations),
                            'current_chunk': i,
                            'total_chunks': total_chunks * 2
                        }
                        
                        # Stage 2: Literary refinement
                        logger.translation_logger.info(f"Refining chunk {i}/{total_chunks}")
                        if self.llm_refine:
                            logger.translation_logger.info(f"Refining translation for chunk {i}")
                            refined_translation = self.refine_translation(google_translation, target_lang)
                        else:
                            logger.translation_logger.info(f"No refinement for chunk {i}")
                            refined_translation = google_translation

                            
                        translated_chunks.append(refined_translation)
                        
                        # Cache the results
                        cache.cache_translation(
                            chunk, refined_translation, google_translation,
                            source_lang, target_lang
                        )
                    
                    progress = ((i + total_chunks) / (total_chunks * 2)) * 100
                    with sqlite3.connect(DB_PATH) as conn:
                        conn.execute('''
                            UPDATE translations 
                            SET progress = ?,
                                translated_text = ?,
                                machine_translation = ?,
                                current_chunk = ?,
                                updated_at = CURRENT_TIMESTAMP
                            WHERE id = ?
                        ''', (
                            progress,
                            '\n\n'.join(translated_chunks),
                            '\n\n'.join(machine_translations),
                            i + total_chunks,
                            translation_id
                        ))
                    
                    yield {
                        'progress': progress,
                        'stage': 'literary_refinement',
                        'machine_translation': '\n\n'.join(machine_translations),
                        'translated_text': '\n\n'.join(translated_chunks),
                        'current_chunk': i + total_chunks,
                        'total_chunks': total_chunks * 2
                    }
                    
                except Exception as e:
                    error_msg = f"Error processing chunk {i}: {str(e)}"
                    logger.translation_logger.error(error_msg)
                    logger.translation_logger.error(traceback.format_exc())
                    raise Exception(error_msg)
                    
                time.sleep(1)  # Rate limiting
                
            # Mark translation as completed
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute('''
                    UPDATE translations 
                    SET status = 'completed',
                        progress = 100,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                ''', (translation_id,))
                
            success = True
            yield {
                'progress': 100,
                'machine_translation': '\n\n'.join(machine_translations),
                'translated_text': '\n\n'.join(translated_chunks),
                'status': 'completed'
            }
            
        except Exception as e:
            error_msg = f"Translation failed: {str(e)}"
            logger.translation_logger.error(error_msg)
            logger.translation_logger.error(traceback.format_exc())
            
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute('''
                    UPDATE translations 
                    SET status = 'error',
                        error_message = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                ''', (str(e), translation_id))
            raise
        finally:
            translation_time = time.time() - start_time
            monitor.record_translation_attempt(success, translation_time)
    
    def refine_translation(self, text: str, target_lang: str) -> str:
        """
        Refine the machine translation strictly in the target language.
        
        Args:
            text (str): The machine-translated text to refine
            target_lang (str): The target language code (e.g., 'en', 'es', 'fr')
        
        Returns:
            str: The refined translation
        """
        # Промпты для каждого языка
        prompts = {
            'en': 'Improve this text to sound more natural in English. Return only the improved text:',
            'es': 'Mejora este texto para que suene más natural en español. Devuelve solo el texto mejorado:',
            'fr': 'Améliorez ce texte pour qu\'il sonne plus naturel en français. Retournez uniquement le texte amélioré :',
            'de': 'Verbessern Sie diesen Text, damit er auf Deutsch natürlicher klingt. Geben Sie nur den verbesserten Text zurück:',
            'it': 'Migliora questo testo per renderlo più naturale in italiano. Restituisci solo il testo migliorato:',
            'pt': 'Melhore este texto para soar mais natural em português. Retorne apenas o texto melhorado:',
            'ru': 'Улучшите этот текст, чтобы он звучал более естественно на русском языке. Верните только улучшенный текст:',
            'zh': '改善这段文字，使其在中文中更加自然。仅返回改善后的文字：',
            'ja': 'この文章を日本語としてより自然に聞こえるように改善してください。改善されたテキストのみを返してください：',
            'ko': '이 텍스트를 한국어로 더 자연스럽게 들리도록 개선하십시오. 개선된 텍스트만 반환하십시오:'
        }
        
        # Получаем промпт для выбранного языка или используем английский как запасной вариант
        prompt_text = prompts.get(target_lang.lower(), prompts['en'])
        
        prompt = f"""{prompt_text}
    
        {text}"""
        
        payload = {
            "model": self.model_name,
            "prompt": prompt,
            "stream": False
        }
        
        response = self.session.post(
            self.api_url,
            json=payload,
            timeout=(1800, 1800)
        )
        response.raise_for_status()
        result = response.json()
        return result['response'].strip()
    
    def get_available_models(self) -> List[str]:
        response = self.session.get(
            "http://localhost:11434/api/tags",
            timeout=(5, 5)
        )
        response.raise_for_status()
        models = response.json()
        return [model['name'] for model in models['models']]

# Translation Recovery
class TranslationRecovery:
    def __init__(self, db_path: str = DB_PATH):
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

recovery = TranslationRecovery()

# Health checking middleware
@app.before_request
def check_ollama():
    if request.endpoint != 'health_check':
        try:
            response = requests.get("http://localhost:11434/api/tags", timeout=5)
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            logger.app_logger.error(f"Ollama health check failed: {str(e)}")
            return jsonify({
                'error': 'Translation service is not available'
            }), 503

# Flask routes
@app.route('/')
def serve_frontend():
    return send_from_directory('static', 'index.html')

@app.route('/<path:path>')
def serve_static(path):
    return send_from_directory('static', path)

@app.route('/models', methods=['GET'])
@with_error_handling
def get_models():
    translator = BookTranslator()
    available_models = translator.get_available_models()
    models = []
    for model_name in available_models:
        models.append({
            'name': model_name,
            'size': 'Unknown',
            'modified': 'Unknown'
        })
    return jsonify({'models': models})

@app.route('/translations', methods=['GET'])
@with_error_handling
def get_translations():
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute('''
            SELECT id, filename, source_lang, target_lang, model,
                   status, progress, detected_language, created_at, 
                   updated_at, error_message
            FROM translations
            ORDER BY created_at DESC
        ''')
        translations = [dict(row) for row in cur.fetchall()]
    return jsonify({'translations': translations})

@app.route('/translations/<int:translation_id>', methods=['GET'])
@with_error_handling
def get_translation(translation_id):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute('SELECT * FROM translations WHERE id = ?', (translation_id,))
        translation = cur.fetchone()
        if translation:
            return jsonify(dict(translation))
        return jsonify({'error': 'Translation not found'}), 404

@app.route('/translate', methods=['POST'])
@with_error_handling
def translate():
    if 'file' not in request.files:
        return jsonify({'error': 'No file part'}), 400

    try:
        file = request.files['file']
        source_lang = request.form.get('sourceLanguage')
        target_lang = request.form.get('targetLanguage')
        model_name = request.form.get('model')
        llm_refine = request.form.get('llmRefine') == 'true' # Get llmRefine from form

        if not all([file, source_lang, target_lang, model_name]):
            return jsonify({'error': 'Missing required parameters'}), 400

        if file.filename == '':
            return jsonify({'error': 'No selected file'}), 400

        filename = secure_filename(file.filename)
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        file.save(filepath)

        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                text = f.read()
        except UnicodeDecodeError:
            with open(filepath, 'r', encoding='cp1251') as f:
                text = f.read()

        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.execute('''
                INSERT INTO translations (
                    filename, source_lang, target_lang, model,
                    status, original_text, genre, llm_refine  -- Included llm_refine
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (filename, source_lang, target_lang, model_name,
                  'in_progress', text, 'unknown', llm_refine))  # Set genre to 'unknown'
            translation_id = cur.lastrowid

        translator = BookTranslator(model_name=model_name)
        translator.llm_refine = llm_refine # Set llm_refine attribute

        def generate():
            try:
                for update in translator.translate_text(text, source_lang, target_lang, translation_id):
                    yield f"data: {json.dumps(update, ensure_ascii=False)}\n\n"
            except Exception as e:
                error_message = str(e)
                logger.translation_logger.error(f"Translation error: {error_message}")
                logger.translation_logger.error(traceback.format_exc())
                yield f"data: {json.dumps({'error': error_message})}\n\n"

        return Response(generate(), mimetype='text/event-stream')

    except Exception as e:
        logger.app_logger.error(f"Translation request error: {str(e)}")
        logger.app_logger.error(traceback.format_exc())
        return jsonify({'error': str(e)}), 500
    finally:
        try:
            if 'filepath' in locals():
                os.remove(filepath)
        except Exception as e:
            logger.app_logger.error(f"Failed to cleanup uploaded file: {str(e)}")

@app.route('/download/<int:translation_id>', methods=['GET'])
@with_error_handling
def download_translation(translation_id):
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute('''
            SELECT filename, translated_text
            FROM translations
            WHERE id = ? AND status = 'completed'
        ''', (translation_id,))
        result = cur.fetchone()
        
        if not result:
            return jsonify({'error': 'Translation not found or not completed'}), 404
        
        filename, translated_text = result
        
        # Create download file with raw text
        download_path = os.path.join(TRANSLATIONS_FOLDER, f'translated_{filename}')
        with open(download_path, 'w', encoding='utf-8') as f:
            f.write(translated_text)
            
        return send_file(
            download_path,
            as_attachment=True,
            download_name=f'translated_{filename}'
        )

@app.route('/failed-translations', methods=['GET'])
@with_error_handling
def get_failed_translations():
    return jsonify(recovery.get_failed_translations())

@app.route('/retry-translation/<int:translation_id>', methods=['POST'])
@with_error_handling
def retry_failed_translation(translation_id):
    recovery.retry_translation(translation_id)
    return jsonify({'status': 'success'})

@app.route('/metrics', methods=['GET'])
def get_metrics():
    return jsonify(monitor.get_metrics())

@app.route('/health', methods=['GET'])
def health_check():
    try:
        response = requests.get("http://localhost:11434/api/tags", timeout=5)
        response.raise_for_status()
        
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute('SELECT 1')
            
        disk_usage = psutil.disk_usage('/')
        if disk_usage.percent > 90:
            logger.app_logger.warning("Low disk space")
            
        return jsonify({
            'status': 'healthy',
            'ollama': 'connected',
            'database': 'connected',
            'disk_usage': f"{disk_usage.percent}%"
        })
    except Exception as e:
        logger.app_logger.error(f"Health check failed: {str(e)}")
        return jsonify({
            'status': 'unhealthy',
            'error': str(e)
        }), 503

def cleanup_old_data():
    while True:
        try:
            logger.app_logger.info("Running cleanup task")
            try:
                cache.cleanup_old_entries()
                logger.app_logger.info("Cache cleanup completed")
            except Exception as e:
                logger.app_logger.error(f"Cache cleanup error: {str(e)}")

            try:
                recovery.cleanup_failed_translations()
                logger.app_logger.info("Failed translations cleanup completed")
            except Exception as e:
                logger.app_logger.error(f"Failed translations cleanup error: {str(e)}")

            time.sleep(24 * 60 * 60)  # Run daily
        except Exception as e:
            logger.app_logger.error(f"Cleanup task error: {str(e)}")
            time.sleep(60 * 60)  # Retry in an hour

# Start cleanup thread
cleanup_thread = threading.Thread(target=cleanup_old_data, daemon=True)
cleanup_thread.start()

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5001, debug=True)
    
