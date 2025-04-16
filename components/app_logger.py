import logging
import os
from logging.handlers import RotatingFileHandler

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
