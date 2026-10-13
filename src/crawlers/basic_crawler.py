"""Basic web crawler implementation using requests and BeautifulSoup"""

import time
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
from src.config import get_config
from src.utils.logger import setup_logger

logger = setup_logger(__name__)
config = get_config()


class BasicCrawler:
    """A basic web crawler with retry logic and error handling"""
    
    def __init__(self, config=None):
        """Initialize the crawler with configuration"""
        self.config = config or get_config()
        self.session = self._create_session()
        self.headers = {
            'User-Agent': self.config.USER_AGENT
        }
    
    def _create_session(self):
        """Create a requests session with retry strategy"""
        session = requests.Session()
        
        retry_strategy = Retry(
            total=self.config.RETRY_ATTEMPTS,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["HEAD", "GET", "OPTIONS"]
        )
        
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        
        return session
    
    def fetch(self, url):
        """
        Fetch content from a URL
        
        Args:
            url (str): URL to fetch
            
        Returns:
            BeautifulSoup: Parsed HTML content, or None on failure
        """
        try:
            logger.info(f"Fetching: {url}")
            
            response = self.session.get(
                url,
                headers=self.headers,
                timeout=self.config.REQUEST_TIMEOUT
            )
            response.raise_for_status()
            
            logger.info(f"Successfully fetched: {url}")
            return BeautifulSoup(response.content, 'html.parser')
            
        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching {url}: {str(e)}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error fetching {url}: {str(e)}")
            return None
    
    def extract_text(self, soup, selector=None):
        """
        Extract text from parsed HTML
        
        Args:
            soup (BeautifulSoup): Parsed HTML content
            selector (str): CSS selector to target specific elements
            
        Returns:
            str or list: Extracted text content
        """
        try:
            if selector:
                elements = soup.select(selector)
                return [elem.get_text(strip=True) for elem in elements]
            else:
                return soup.get_text(strip=True)
        except Exception as e:
            logger.error(f"Error extracting text: {str(e)}")
            return None
    
    def extract_links(self, soup, base_url=None):
        """
        Extract all links from parsed HTML
        
        Args:
            soup (BeautifulSoup): Parsed HTML content
            base_url (str): Base URL for relative links
            
        Returns:
            list: List of extracted links
        """
        try:
            links = []
            for link in soup.find_all('a', href=True):
                href = link['href']
                links.append({
                    'url': href,
                    'text': link.get_text(strip=True)
                })
            return links
        except Exception as e:
            logger.error(f"Error extracting links: {str(e)}")
            return []
    
    def crawl(self, url, selector=None):
        """
        Complete crawl operation: fetch and extract data
        
        Args:
            url (str): URL to crawl
            selector (str): CSS selector for data extraction
            
        Returns:
            dict: Crawled data or None on failure
        """
        soup = self.fetch(url)
        if not soup:
            return None
        
        return {
            'url': url,
            'text': self.extract_text(soup, selector),
            'links': self.extract_links(soup)
        }
