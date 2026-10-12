"""Image extraction and downloading functionality"""

import os
import requests
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


class ImageParser:
    """Extract and download images from web pages"""
    
    def __init__(self, output_dir="data/images/"):
        """Initialize image parser"""
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)
    
    def extract_images(self, html, base_url):
        """
        Extract image URLs from HTML
        
        Args:
            html (str): HTML content
            base_url (str): Base URL for relative links
            
        Returns:
            list: Image information
        """
        soup = BeautifulSoup(html, 'html.parser')
        images = []
        
        for img in soup.find_all('img'):
            img_url = img.get('src')
            if img_url:
                # Convert relative URLs to absolute
                absolute_url = urljoin(base_url, img_url)
                
                images.append({
                    'url': absolute_url,
                    'alt': img.get('alt', ''),
                    'title': img.get('title', ''),
                    'width': img.get('width'),
                    'height': img.get('height'),
                })
        
        logger.info(f"Found {len(images)} images")
        return images
    
    def download_images(self, url, min_width=0, min_height=0, max_images=None):
        """
        Download images from webpage
        
        Args:
            url (str): Webpage URL
            min_width (int): Minimum image width
            min_height (int): Minimum image height
            max_images (int): Maximum images to download
            
        Returns:
            list: Downloaded image info
        """
        try:
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            
            images = self.extract_images(response.text, url)
            downloaded = []
            
            for i, img in enumerate(images):
                if max_images and i >= max_images:
                    break
                
                try:
                    img_response = requests.get(img['url'], timeout=10, stream=True)
                    
                    # Check image size from headers if available
                    if 'content-length' in img_response.headers:
                        size = int(img_response.headers['content-length'])
                        logger.debug(f"Image size: {size} bytes")
                    
                    # Generate filename
                    filename = os.path.basename(img['url'].split('?')[0])
                    if not filename:
                        filename = f"image_{i}.jpg"
                    
                    filepath = os.path.join(self.output_dir, filename)
                    
                    # Download and save
                    with open(filepath, 'wb') as f:
                        for chunk in img_response.iter_content(chunk_size=8192):
                            f.write(chunk)
                    
                    downloaded.append({
                        'url': img['url'],
                        'filepath': filepath,
                        'alt': img['alt']
                    })
                    
                    logger.info(f"Downloaded image: {filename}")
                
                except Exception as e:
                    logger.warning(f"Failed to download image {img['url']}: {str(e)}")
            
            logger.info(f"Downloaded {len(downloaded)} images")
            return downloaded
        
        except Exception as e:
            logger.error(f"Error downloading images from {url}: {str(e)}")
            return []
