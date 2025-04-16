import json
import requests
import time
import sqlite3
import traceback
from typing import List, Dict, Callable
from functools import wraps
from deep_translator import GoogleTranslator

class BookTranslator:
    def __init__(self, model_name: str = "aya-expanse:32b", chunk_size: int = 1000, llm_refine: bool = True, api_url: str = "http://localhost:11434/api/generate"):
        self.model_name = model_name
        self.api_url = api_url
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

    def translate_text(self, text: str, source_lang: str, target_lang: str, translation_id: int, logger, cache, monitor, DB_PATH):
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