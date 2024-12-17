from PIL import Image
from io import BytesIO
import psutil
import os
import logging

# Configure logging for utils
logging.basicConfig(level=logging.DEBUG, handlers=[logging.StreamHandler()])
utils_logger = logging.getLogger("utils")

def resize_image(image, quality, resolution_percentage):
    utils_logger.info("Iniciando resize_image")
    try:
        width = int(image.width * resolution_percentage)
        height = int(image.height * resolution_percentage)

        resized_image = image.resize((width, height), Image.Resampling.LANCZOS)
        utils_logger.info("Finalizando resize_image")
        return resized_image
    except Exception as e:
        utils_logger.error(f"Error in resize_image: {e}")
        return None