"""High-speed asynchronous web crawler"""

import asyncio
import aiohttp
from bs4 import BeautifulSoup
from src.config import get_config
from src.utils.logger import setup_logger

logger = setup_logger(__name__)
config = get_config()


class AsyncCrawler:
    """High-performance async crawler for concurrent requests"""
    
    def __init__(self, max_concurrent=10):
        """Initialize async crawler"""
        self.max_concurrent = max_concurrent
        self.config = config
        self.headers = {
            'User-Agent': self.config.USER_AGENT
        }
        self.semaphore = asyncio.Semaphore(max_concurrent)
    
    async def fetch_url(self, session, url):
        """
        Fetch a single URL asynchronously
        
        Args:
            session: aiohttp ClientSession
            url (str): URL to fetch
            
        Returns:
            dict: URL and content
        """
        async with self.semaphore:
            try:
                async with session.get(
                    url,
                    headers=self.headers,
                    timeout=aiohttp.ClientTimeout(seconds=self.config.REQUEST_TIMEOUT)
                ) as response:
                    if response.status == 200:
                        content = await response.text()
                        logger.info(f"Successfully fetched: {url}")
                        return {'url': url, 'content': content, 'status': response.status}
                    else:
                        logger.warning(f"HTTP {response.status} for {url}")
                        return {'url': url, 'content': None, 'status': response.status}
            
            except asyncio.TimeoutError:
                logger.error(f"Timeout fetching {url}")
                return {'url': url, 'content': None, 'status': None, 'error': 'Timeout'}
            
            except Exception as e:
                logger.error(f"Error fetching {url}: {str(e)}")
                return {'url': url, 'content': None, 'status': None, 'error': str(e)}
    
    async def crawl_multiple(self, urls):
        """
        Crawl multiple URLs concurrently
        
        Args:
            urls (list): List of URLs to crawl
            
        Returns:
            list: Results for all URLs
        """
        logger.info(f"Starting async crawl for {len(urls)} URLs")
        
        async with aiohttp.ClientSession() as session:
            tasks = [self.fetch_url(session, url) for url in urls]
            results = await asyncio.gather(*tasks)
        
        logger.info(f"Completed async crawl for {len(urls)} URLs")
        return results
    
    async def crawl_and_parse(self, urls):
        """
        Crawl URLs and parse HTML
        
        Args:
            urls (list): List of URLs
            
        Returns:
            list: Parsed content
        """
        results = await self.crawl_multiple(urls)
        parsed_results = []
        
        for result in results:
            if result['content']:
                try:
                    soup = BeautifulSoup(result['content'], 'html.parser')
                    parsed_results.append({
                        'url': result['url'],
                        'title': soup.title.string if soup.title else None,
                        'links': [a.get('href') for a in soup.find_all('a', href=True)],
                        'text': soup.get_text(strip=True)[:500]
                    })
                except Exception as e:
                    logger.error(f"Error parsing {result['url']}: {str(e)}")
        
        return parsed_results
    
    async def batch_crawl(self, urls, batch_size=50):
        """
        Crawl URLs in batches
        
        Args:
            urls (list): All URLs
            batch_size (int): URLs per batch
            
        Returns:
            list: All results
        """
        all_results = []
        
        for i in range(0, len(urls), batch_size):
            batch = urls[i:i+batch_size]
            logger.info(f"Processing batch {i//batch_size + 1}")
            results = await self.crawl_multiple(batch)
            all_results.extend(results)
        
        return all_results
    
    def run(self, urls):
        """
        Run async crawler (wrapper for synchronous code)
        
        Args:
            urls (list): URLs to crawl
            
        Returns:
            list: Results
        """
        return asyncio.run(self.crawl_multiple(urls))
