import json
import requests
import time
import os
import sqlite3
import logging
import hashlib
import traceback
import psutil
import threading
from datetime import datetime
from functools import wraps
from typing import List, Dict, Callable
from flask import Flask, request, jsonify, Response, send_file, send_from_directory
from flask_cors import CORS

# Import the classes from the components package
from components.app_logger import AppLogger
from components.app_monitor import AppMonitor, TranslationMetrics
from components.translation_cache import TranslationCache
from components.book_translator import BookTranslator
from components.translation_recovery import TranslationRecovery

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

# Initialize logger
logger = AppLogger()

# Initialize monitor
monitor = AppMonitor()

# Initialize cache
cache = TranslationCache(db_path=CACHE_DB_PATH)

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

recovery = TranslationRecovery(db_path=DB_PATH)

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
                for update in translator.translate_text(text, source_lang, target_lang, translation_id, logger, cache, monitor, DB_PATH):
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