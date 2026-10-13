"""Advanced web crawler with CSS selectors, XPath, and metadata extraction"""

import requests
from bs4 import BeautifulSoup
from lxml import etree
from src.config import get_config
from src.utils.logger import setup_logger
from src.crawlers.basic_crawler import BasicCrawler

logger = setup_logger(__name__)


class AdvancedCrawler(BasicCrawler):
    """Advanced crawler with additional extraction capabilities"""
    
    def scrape(self, url, selectors):
        """
        Scrape website with CSS selectors
        
        Args:
            url (str): Target URL
            selectors (dict): CSS selectors {name: selector}
            
        Returns:
            dict: Extracted data
        """
        try:
            soup = self.fetch(url)
            if not soup:
                return None
            
            data = {'url': url}
            
            for name, selector in selectors.items():
                elements = soup.select(selector)
                data[name] = [elem.get_text(strip=True) for elem in elements]
            
            logger.info(f"Successfully scraped {url} with {len(selectors)} selectors")
            return data
            
        except Exception as e:
            logger.error(f"Error scraping {url}: {str(e)}")
            return None
    
    def scrape_with_xpath(self, url, xpaths):
        """
        Scrape using XPath expressions
        
        Args:
            url (str): Target URL
            xpaths (dict): XPath expressions {name: xpath}
            
        Returns:
            dict: Extracted data
        """
        try:
            response = requests.get(url, headers=self.headers, timeout=self.config.REQUEST_TIMEOUT)
            response.raise_for_status()
            
            tree = etree.HTML(response.content)
            data = {'url': url}
            
            for name, xpath in xpaths.items():
                elements = tree.xpath(xpath)
                data[name] = elements
            
            logger.info(f"Successfully scraped {url} with XPath")
            return data
            
        except Exception as e:
            logger.error(f"Error scraping with XPath {url}: {str(e)}")
            return None
    
    def extract_metadata(self, url):
        """
        Extract SEO metadata from webpage
        
        Args:
            url (str): Target URL
            
        Returns:
            dict: Metadata
        """
        try:
            soup = self.fetch(url)
            if not soup:
                return None
            
            metadata = {
                'url': url,
                'title': soup.title.string if soup.title else None,
                'meta_description': None,
                'meta_keywords': None,
                'meta_author': None,
                'og_title': None,
                'og_description': None,
                'og_image': None,
            }
            
            # Extract meta tags
            for meta in soup.find_all('meta'):
                name = meta.get('name', '').lower()
                property_attr = meta.get('property', '').lower()
                content = meta.get('content')
                
                if name == 'description':
                    metadata['meta_description'] = content
                elif name == 'keywords':
                    metadata['meta_keywords'] = content
                elif name == 'author':
                    metadata['meta_author'] = content
                elif property_attr == 'og:title':
                    metadata['og_title'] = content
                elif property_attr == 'og:description':
                    metadata['og_description'] = content
                elif property_attr == 'og:image':
                    metadata['og_image'] = content
            
            # Extract headings
            h1_tags = [h1.get_text(strip=True) for h1 in soup.find_all('h1')]
            metadata['h1_headings'] = h1_tags
            
            logger.info(f"Successfully extracted metadata from {url}")
            return metadata
            
        except Exception as e:
            logger.error(f"Error extracting metadata from {url}: {str(e)}")
            return None
    
    def extract_structured_data(self, url):
        """
        Extract structured data (JSON-LD, microdata)
        
        Args:
            url (str): Target URL
            
        Returns:
            dict: Structured data
        """
        try:
            soup = self.fetch(url)
            if not soup:
                return None
            
            structured_data = {
                'url': url,
                'json_ld': [],
                'microdata': []
            }
            
            # Extract JSON-LD
            import json
            for script in soup.find_all('script', type='application/ld+json'):
                try:
                    data = json.loads(script.string)
                    structured_data['json_ld'].append(data)
                except:
                    pass
            
            logger.info(f"Successfully extracted structured data from {url}")
            return structured_data
            
        except Exception as e:
            logger.error(f"Error extracting structured data from {url}: {str(e)}")
            return None
