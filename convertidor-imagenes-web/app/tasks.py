# convertidor-imagenes-web/app/tasks.py
import os
import logging
import uuid
from PIL import Image
from io import BytesIO

tasks_logger = logging.getLogger("tasks")

UPLOAD_FOLDER = os.getenv('UPLOAD_FOLDER', 'uploads')
TEMP_UPLOAD_FOLDER = os.getenv('TEMP_UPLOAD_FOLDER', 'temp_uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(TEMP_UPLOAD_FOLDER, exist_ok=True)

def process_image(file_path, output_format, quality, resolution_percentage, filename):
    try:
        tasks_logger.info(f"Processing image: {filename} in worker")
        image = Image.open(file_path)

        # Resize if needed
        if resolution_percentage != 1:
            new_width = int(image.width * resolution_percentage)
            new_height = int(image.height * resolution_percentage)
            image = image.resize((new_width, new_height))

        if output_format == 'ORIGINAL':
            output_format = image.format

        output_filename = os.path.splitext(filename)[0] + '.' + output_format.lower()
        output_path = os.path.join(UPLOAD_FOLDER, output_filename)

        # Save with specified quality
        if output_format.upper() in ('JPEG', 'WEBP'):
            image.save(output_path, format=output_format, quality=quality, optimize=True)
        else:
            image.save(output_path, format=output_format, optimize=True)
        
        tasks_logger.info(f"Saved processed image to {output_path}")
        return output_filename  # Return the filename of the processed image

    except Exception as e:
        tasks_logger.error(f"Error processing image {filename}: {e}")
        raise e
    finally:
        os.remove(file_path)