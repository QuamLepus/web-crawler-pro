"""Logging configuration for Web Crawler Pro"""

import logging
import os
from src.config import get_config

config = get_config()

# Create logs directory if it doesn't exist
os.makedirs(os.path.dirname(config.LOG_FILE), exist_ok=True)


def setup_logger(name):
    """Setup logger with file and console handlers"""
    
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, config.LOG_LEVEL))
    
    # Console Handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(getattr(logging, config.LOG_LEVEL))
    
    # File Handler
    file_handler = logging.FileHandler(config.LOG_FILE)
    file_handler.setLevel(getattr(logging, config.LOG_LEVEL))
    
    # Formatter
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    console_handler.setFormatter(formatter)
    file_handler.setFormatter(formatter)
    
    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    
    return logger
