"""Logging setup for Web Crawler Pro"""

import logging
import sys

from src.config import get_config

_CONFIGURED_LOGGERS = set()


def setup_logger(name: str = "web_crawler_pro") -> logging.Logger:
    """
    Create (or retrieve) a configured logger.

    Args:
        name: Logger name, typically __name__ of the calling module.

    Returns:
        logging.Logger: Configured logger instance.
    """
    config = get_config()
    logger = logging.getLogger(name)

    # Avoid adding duplicate handlers if called multiple times for same name
    if name in _CONFIGURED_LOGGERS:
        return logger

    logger.setLevel(getattr(logging, config.LOG_LEVEL.upper(), logging.INFO))

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    if config.LOG_FILE:
        file_handler = logging.FileHandler(config.LOG_FILE)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    logger.propagate = False
    _CONFIGURED_LOGGERS.add(name)
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
