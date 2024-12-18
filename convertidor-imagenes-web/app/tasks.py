# tasks.py
import os
import logging
import uuid
from PIL import Image
import base64
from io import BytesIO
from rq import get_current_job
import redis

tasks_logger = logging.getLogger("tasks")

UPLOAD_FOLDER = os.getenv('UPLOAD_FOLDER', '/data/uploads')
TEMP_UPLOAD_FOLDER = os.getenv('TEMP_UPLOAD_FOLDER', '/data/temp-uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(TEMP_UPLOAD_FOLDER, exist_ok=True)

# Redis setup (updated for Kubernetes service)
redis_conn = redis.Redis(host='redis-service', port=6379)  # Use the service name

def process_image(output_format, quality, resolution_percentage, filename):
    job = get_current_job()
    job_id = job.id
    temp_file_path = redis_conn.get(f"job:{job_id}:temppath").decode()
    try:
        tasks_logger.info(f"Processing image: {filename} from temp path: {temp_file_path} in worker")
        image = Image.open(temp_file_path)

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
        tasks_logger.info(f"Returning output filename: {output_filename}")
        return output_filename

    except Exception as e:
        tasks_logger.error(f"Error processing image {filename} from path {temp_file_path}: {e}")
        raise e
    finally:
        # Cleanup: Remove the temporary file
        if os.path.exists(temp_file_path):
            try:
                os.remove(temp_file_path)
                tasks_logger.info(f"Removed temporary file: {temp_file_path}")
            except Exception as e:
                tasks_logger.error(f"Error removing temporary file {temp_file_path}: {e}")
        else:
            tasks_logger.warning(f"Temporary file not found: {temp_file_path}")