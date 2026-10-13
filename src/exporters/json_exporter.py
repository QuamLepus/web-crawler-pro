"""JSON data export functionality"""

import json
import os
from datetime import datetime
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


class JSONExporter:
    """Export scraped data to JSON format"""
    
    def __init__(self, output_path="data/exports/json/"):
        """Initialize JSON exporter"""
        self.output_path = output_path
        os.makedirs(output_path, exist_ok=True)
    
    def export(self, data, filename=None, pretty=True):
        """
        Export data to JSON file
        
        Args:
            data: Data to export (dict or list)
            filename (str): Output filename
            pretty (bool): Pretty print JSON
            
        Returns:
            str: Output file path
        """
        if not data:
            logger.warning("No data to export")
            return None
        
        if filename is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"export_{timestamp}.json"
        
        filepath = os.path.join(self.output_path, filename)
        
        try:
            with open(filepath, 'w', encoding='utf-8') as jsonfile:
                if pretty:
                    json.dump(data, jsonfile, indent=2, ensure_ascii=False)
                else:
                    json.dump(data, jsonfile, ensure_ascii=False)
            
            file_size = os.path.getsize(filepath) / 1024  # KB
            logger.info(f"Exported data to {filepath} ({file_size:.2f} KB)")
            return filepath
        
        except Exception as e:
            logger.error(f"Error exporting to JSON: {str(e)}")
            return None
    
    def export_ndjson(self, data, filename=None):
        """
        Export data as newline-delimited JSON
        
        Args:
            data (list): List of records
            filename (str): Output filename
            
        Returns:
            str: Output file path
        """
        if not isinstance(data, list):
            logger.error("NDJSON export requires list of records")
            return None
        
        if filename is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"export_{timestamp}.ndjson"
        
        filepath = os.path.join(self.output_path, filename)
        
        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                for record in data:
                    f.write(json.dumps(record, ensure_ascii=False) + '\n')
            
            logger.info(f"Exported {len(data)} records to {filepath}")
            return filepath
        
        except Exception as e:
            logger.error(f"Error exporting to NDJSON: {str(e)}")
            return None
