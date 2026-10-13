"""Main entry point for Web Crawler Pro"""

from src.crawlers.basic_crawler import BasicCrawler
from src.utils.logger import setup_logger
from src.config import get_config

logger = setup_logger(__name__)


def main():
    """Main function"""
    logger.info("Starting Web Crawler Pro...")
    
    # Initialize crawler
    crawler = BasicCrawler()
    
    # Example usage
    url = "https://example.com"
    logger.info(f"Crawling: {url}")
    
    result = crawler.crawl(url)
    if result:
        logger.info("Crawl completed successfully")
        print(f"URL: {result['url']}")
        print(f"Links found: {len(result['links'])}")
    else:
        logger.error("Crawl failed")


if __name__ == "__main__":
    main()
