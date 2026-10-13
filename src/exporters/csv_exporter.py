"""CSV data export functionality"""

import csv
import os
from datetime import datetime
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


class CSVExporter:
    """Export scraped data to CSV format"""
    
    def __init__(self, output_path="data/exports/csv/"):
        """Initialize CSV exporter"""
        self.output_path = output_path
        os.makedirs(output_path, exist_ok=True)
    
    def export(self, data, filename=None):
        """
        Export data to CSV file
        
        Args:
            data (list): List of dictionaries
            filename (str): Output filename
            
        Returns:
            str: Output file path
        """
        if not data:
            logger.warning("No data to export")
            return None
        
        if filename is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"export_{timestamp}.csv"
        
        filepath = os.path.join(self.output_path, filename)
        
        try:
            # Get headers from first record
            if isinstance(data, list) and len(data) > 0:
                headers = data[0].keys()
            else:
                logger.error("Invalid data format")
                return None
            
            with open(filepath, 'w', newline='', encoding='utf-8') as csvfile:
                writer = csv.DictWriter(csvfile, fieldnames=headers)
                writer.writeheader()
                
                for row in data:
                    # Handle list/dict fields
                    cleaned_row = {}
                    for key, value in row.items():
                        if isinstance(value, (list, dict)):
                            cleaned_row[key] = str(value)
                        else:
                            cleaned_row[key] = value
                    
                    writer.writerow(cleaned_row)
            
            logger.info(f"Exported {len(data)} records to {filepath}")
            return filepath
        
        except Exception as e:
            logger.error(f"Error exporting to CSV: {str(e)}")
            return None
    
    def export_batch(self, data_list, batch_filename):
        """
        Export multiple data batches
        
        Args:
            data_list (list): List of data lists
            batch_filename (str): Base filename
            
        Returns:
            list: Output file paths
        """
        output_files = []
        
        for i, data in enumerate(data_list):
            filename = f"{batch_filename}_batch_{i+1}.csv"
            filepath = self.export(data, filename)
            if filepath:
                output_files.append(filepath)
        
        logger.info(f"Exported {len(output_files)} batch files")
        return output_files
