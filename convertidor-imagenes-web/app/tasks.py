import os
from PIL import Image
import logging
import uuid
from multiprocessing import cpu_count

logging.basicConfig(level=logging.DEBUG, handlers=[logging.StreamHandler()])
tasks_logger = logging.getLogger("tasks")

UPLOAD_FOLDER = os.getenv('UPLOAD_FOLDER', 'uploads')
TEMP_UPLOAD_FOLDER = os.getenv('TEMP_UPLOAD_FOLDER', 'temp_uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(TEMP_UPLOAD_FOLDER, exist_ok=True)

def process_image(file_path, output_format, quality, resolution_percentage, filename):
    tasks_logger.info(f"Processing image: {filename}")
    try:
        image = Image.open(file_path)
        width = int(image.width * resolution_percentage)
        height = int(image.height * resolution_percentage)

        # Use a context manager to automatically close the image
        with image:
            # Determine the number of processes to use
            total_cpus = cpu_count()

            # If using default settings (100% resolution), use half of the available cores
            if resolution_percentage == 1.0:
                num_processes = max(1, int(total_cpus * 0.5))
            else:
                # Otherwise, use the percentage of cores based on the resolution setting
                num_processes = max(1, int(total_cpus * resolution_percentage))

            # Set the OMP_NUM_THREADS environment variable to limit the number of threads per process
            os.environ["OMP_NUM_THREADS"] = str(num_processes)

            # Perform the resize operation
            resized_image = image.resize((width, height), Image.Resampling.LANCZOS)

        unique_id = str(uuid.uuid4())
        
        # Correctly handle "original" format
        if output_format.upper() == 'ORIGINAL':
            output_filename = f"converted_{unique_id}_{filename}"
        else:
            output_filename = f"converted_{unique_id}_{filename.split('.')[0]}.{output_format.lower()}"
        
        output_path = os.path.join(UPLOAD_FOLDER, output_filename)
        
        # If output format is "original", use the original format, otherwise use the specified format
        if output_format.upper() == 'ORIGINAL':
            resized_image.save(output_path, quality=quality) # Save with original format and quality
        else:
            resized_image.save(output_path, format=output_format, quality=quality)
            
        tasks_logger.info(f"Finished processing image: {filename}")
        return {'success': True, 'output_path': output_path, 'output_filename': output_filename}
    except Exception as e:
        tasks_logger.error(f"Error processing image {filename}: {e}")
        return {'success': False, 'error': str(e), 'filename': filename}
    finally:
        # Ensure temporary file is removed even if an error occurs
        if os.path.exists(file_path):
            os.remove(file_path)