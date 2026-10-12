"""Configuration management for Web Crawler Pro"""

import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()


class Config:
    """Base configuration class"""
    
    # Request Settings
    BASE_URL = os.getenv("BASE_URL", "https://example.com")
    USER_AGENT = os.getenv(
        "USER_AGENT",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    )
    REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", 30))
    RETRY_ATTEMPTS = int(os.getenv("RETRY_ATTEMPTS", 3))
    DELAY_BETWEEN_REQUESTS = float(os.getenv("DELAY_BETWEEN_REQUESTS", 1))
    
    # Database
    DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///crawler.db")
    
    # Logging
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
    LOG_FILE = os.getenv("LOG_FILE", "logs/crawler.log")
    
    # Proxy
    USE_PROXY = os.getenv("USE_PROXY", "False").lower() == "true"
    PROXY_URL = os.getenv("PROXY_URL", "")
    
    # API Keys
    API_KEY = os.getenv("API_KEY", "")


class DevelopmentConfig(Config):
    """Development configuration"""
    DEBUG = True
    LOG_LEVEL = "DEBUG"


class ProductionConfig(Config):
    """Production configuration"""
    DEBUG = False
    LOG_LEVEL = "INFO"


class TestingConfig(Config):
    """Testing configuration"""
    DEBUG = True
    DATABASE_URL = "sqlite:///:memory:"
    LOG_LEVEL = "DEBUG"


def get_config(env=None):
    """Get configuration based on environment"""
    if env is None:
        env = os.getenv("ENVIRONMENT", "development")
    
    configs = {
        "development": DevelopmentConfig,
        "production": ProductionConfig,
        "testing": TestingConfig,
    }
    
    return configs.get(env, DevelopmentConfig)
