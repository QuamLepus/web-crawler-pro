"""Unit tests for basic crawler"""

import pytest
from unittest.mock import patch, MagicMock
from src.crawlers.basic_crawler import BasicCrawler


class TestBasicCrawler:
    """Test cases for BasicCrawler class"""
    
    @pytest.fixture
    def crawler(self):
        """Create a crawler instance for testing"""
        return BasicCrawler()
    
    def test_crawler_initialization(self, crawler):
        """Test crawler initialization"""
        assert crawler is not None
        assert crawler.session is not None
        assert crawler.headers is not None
    
    @patch('src.crawlers.basic_crawler.requests.Session.get')
    def test_fetch_success(self, mock_get, crawler):
        """Test successful fetch operation"""
        mock_response = MagicMock()
        mock_response.content = b'<html><body>Test</body></html>'
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response
        
        result = crawler.fetch('https://example.com')
        assert result is not None
    
    @patch('src.crawlers.basic_crawler.requests.Session.get')
    def test_fetch_failure(self, mock_get, crawler):
        """Test fetch operation with error"""
        mock_get.side_effect = Exception("Connection error")
        
        result = crawler.fetch('https://example.com')
        assert result is None
    
    def test_extract_text(self, crawler):
        """Test text extraction from HTML"""
        from bs4 import BeautifulSoup
        html = '<html><body><p>Hello World</p></body></html>'
        soup = BeautifulSoup(html, 'html.parser')
        
        text = crawler.extract_text(soup)
        assert 'Hello World' in text
    
    def test_extract_links(self, crawler):
        """Test link extraction from HTML"""
        from bs4 import BeautifulSoup
        html = '<html><body><a href="https://example.com">Example</a></body></html>'
        soup = BeautifulSoup(html, 'html.parser')
        
        links = crawler.extract_links(soup)
        assert len(links) == 1
        assert links[0]['url'] == 'https://example.com'
        assert links[0]['text'] == 'Example'


if __name__ == '__main__':
    pytest.main([__file__])
