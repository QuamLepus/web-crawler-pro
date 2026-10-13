"""Crawler for handling pagination and unlimited page crawling"""

import re
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse
from src.config import get_config
from src.utils.logger import setup_logger
from src.crawlers.async_crawler import AsyncCrawler
from bs4 import BeautifulSoup
import asyncio

logger = setup_logger(__name__)
config = get_config()


class PaginationCrawler:
    """Crawler for automatically handling pagination"""
    
    def __init__(self):
        """Initialize pagination crawler"""
        self.config = config
        self.async_crawler = AsyncCrawler(max_concurrent=5)
        self.visited_urls = set()
    
    def generate_pagination_urls(self, base_url, pattern, max_pages=None):
        """
        Generate paginated URLs
        
        Args:
            base_url (str): Base URL
            pattern (str): Pattern like '?page={page}' or '/page/{page}'
            max_pages (int): Maximum pages or None for unlimited
            
        Returns:
            list: Generated URLs
        """
        urls = []
        page = 1
        
        while True:
            if max_pages and page > max_pages:
                break
            
            url = base_url + pattern.format(page=page)
            urls.append(url)
            page += 1
            
            if max_pages is None and page > 1000:  # Safety limit
                logger.warning("Reached safety limit of 1000 pages")
                break
        
        logger.info(f"Generated {len(urls)} pagination URLs")
        return urls
    
    def detect_next_page_url(self, soup, base_url):
        """
        Detect next page URL automatically
        
        Args:
            soup: BeautifulSoup object
            base_url (str): Base URL for relative links
            
        Returns:
            str: Next page URL or None
        """
        # Look for common next page indicators
        selectors = [
            ('a[rel="next"]', 'href'),
            ('a.next', 'href'),
            ('a:contains("Next")', 'href'),
            ('li.next a', 'href'),
            ('span.next a', 'href'),
        ]
        
        for selector, attr in selectors:
            try:
                # BeautifulSoup doesn't support :contains, so use text search
                for link in soup.find_all('a'):
                    href = link.get(attr)
                    text = link.get_text().lower()
                    
                    if href and ('next' in text or 'more' in text or '>' in text):
                        next_url = urljoin(base_url, href)
                        if next_url not in self.visited_urls:
                            return next_url
            except:
                continue
        
        return None
    
    def extract_page_data(self, html, selectors):
        """
        Extract data from page
        
        Args:
            html (str): HTML content
            selectors (dict): CSS selectors for data extraction
            
        Returns:
            list: Extracted data
        """
        soup = BeautifulSoup(html, 'html.parser')
        page_data = []
        
        # Find container elements (usually rows/items)
        container_selector = selectors.get('container', None)
        
        if container_selector:
            containers = soup.select(container_selector)
        else:
            # Try to find common container patterns
            containers = soup.select('div.item, div.product, tr, li')
        
        for container in containers:
            item = {}
            
            for field_name, field_selector in selectors.items():
                if field_name == 'container':
                    continue
                
                element = container.select_one(field_selector)
                if element:
                    item[field_name] = element.get_text(strip=True)
            
            if item:
                page_data.append(item)
        
        logger.info(f"Extracted {len(page_data)} items from page")
        return page_data
    
    async def crawl_all_pages_async(self, base_url, pattern, selectors, max_pages=None):
        """
        Asynchronously crawl all pages
        
        Args:
            base_url (str): Base URL
            pattern (str): Pagination pattern
            selectors (dict): Data extraction selectors
            max_pages (int): Maximum pages
            
        Returns:
            list: All extracted data
        """
        all_data = []
        page = 1
        
        while True:
            if max_pages and page > max_pages:
                break
            
            url = base_url + pattern.format(page=page)
            
            if url in self.visited_urls:
                logger.info(f"Page {page} already visited, stopping")
                break
            
            self.visited_urls.add(url)
            logger.info(f"Crawling page {page}: {url}")
            
            try:
                async with self.async_crawler.semaphore:
                    import aiohttp
                    async with aiohttp.ClientSession() as session:
                        async with session.get(url, timeout=aiohttp.ClientTimeout(seconds=30)) as response:
                            if response.status == 200:
                                html = await response.text()
                                page_data = self.extract_page_data(html, selectors)
                                all_data.extend(page_data)
                            else:
                                logger.warning(f"HTTP {response.status} for {url}")
                                break
            except Exception as e:
                logger.error(f"Error crawling page {page}: {str(e)}")
                break
            
            page += 1
            await asyncio.sleep(self.config.DELAY_BETWEEN_REQUESTS)
        
        return all_data
    
    def crawl_all_pages(self, base_url, pattern=None, selectors=None, max_pages=None):
        """
        Synchronous wrapper for crawling all pages
        
        Args:
            base_url (str): Base URL
            pattern (str): Pagination pattern
            selectors (dict): Data extraction selectors
            max_pages (int): Maximum pages
            
        Returns:
            list: All extracted data
        """
        if pattern is None:
            pattern = "?page={page}"
        
        if selectors is None:
            selectors = {}
        
        return asyncio.run(self.crawl_all_pages_async(base_url, pattern, selectors, max_pages))
