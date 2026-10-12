"""Rate limiting functionality"""

import asyncio
import time
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


class RateLimiter:
    """Rate limiter for API and web requests"""
    
    def __init__(self, requests_per_second=5, burst_size=1):
        """Initialize rate limiter"""
        self.requests_per_second = requests_per_second
        self.burst_size = burst_size
        self.min_interval = 1.0 / requests_per_second
        self.last_request_time = 0
        self.request_count = 0
    
    def wait_if_needed(self):
        """
        Check if we need to wait before making next request
        
        Returns:
            float: Time waited
        """
        current_time = time.time()
        time_since_last = current_time - self.last_request_time
        
        if time_since_last < self.min_interval:
            wait_time = self.min_interval - time_since_last
            time.sleep(wait_time)
            logger.debug(f"Rate limit: waited {wait_time:.3f} seconds")
            return wait_time
        
        return 0
    
    def record_request(self):
        """Record a request"""
        self.last_request_time = time.time()
        self.request_count += 1
    
    async def async_wait_if_needed(self):
        """
        Async version of wait_if_needed
        
        Returns:
            float: Time waited
        """
        current_time = time.time()
        time_since_last = current_time - self.last_request_time
        
        if time_since_last < self.min_interval:
            wait_time = self.min_interval - time_since_last
            await asyncio.sleep(wait_time)
            logger.debug(f"Rate limit: waited {wait_time:.3f} seconds")
            return wait_time
        
        return 0
