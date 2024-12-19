import os
import logging
import uuid
from PIL import Image
import base64
from io import BytesIO
from rq import get_current_job
import redis
from redis.exceptions import LockError

# Configuración del logger para las tareas
tasks_logger = logging.getLogger("tasks")

# Variables de entorno para las rutas de subida y temporales
UPLOAD_FOLDER = os.getenv('UPLOAD_FOLDER', '/data/uploads')
TEMP_UPLOAD_FOLDER = os.getenv('TEMP_UPLOAD_FOLDER', '/data/temp-uploads')
# Crea las carpetas si no existen
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(TEMP_UPLOAD_FOLDER, exist_ok=True)

# Configuración de Redis (actualizada para Kubernetes service)
redis_conn = redis.Redis(host='redis-service', port=6379)  # Usamos el nombre del servicio

# Tamaño máximo de píxeles permitidos
MAX_PIXELS = 20000000  # 20 megapíxeles

def process_image(output_format, quality, resolution_percentage, filename, temp_filename):
    """Función para procesar una imagen, redimensionarla y convertirla al formato deseado."""
    job = get_current_job()
    job_id = job.id

    # Construye la ruta completa al archivo temporal
    temp_file_path = os.path.join(TEMP_UPLOAD_FOLDER, temp_filename)

    # Usa un Redis lock para asegurar que solo un worker procese este trabajo
    lock = redis_conn.lock(f"lock:{job_id}", timeout=360)

    try:
        if lock.acquire(blocking=False):
            tasks_logger.info(f"Procesando imagen: {filename} desde la ruta temporal: {temp_file_path} en el worker")
            # Abre la imagen
            image = Image.open(temp_file_path)

            # Verifica el tamaño de la imagen
            if image.width * image.height > MAX_PIXELS:
                tasks_logger.error(f"La imagen {filename} excede el tamaño máximo de {MAX_PIXELS / 1000000} MP")
                raise ValueError(f"La imagen excede el tamaño máximo de {MAX_PIXELS / 1000000} MP")

            # Redimensiona si es necesario
            if resolution_percentage != 1:
                new_width = int(image.width * resolution_percentage)
                new_height = int(image.height * resolution_percentage)
                image = image.resize((new_width, new_height))

            # Si el formato de salida es 'ORIGINAL', usa el formato original de la imagen
            if output_format == 'ORIGINAL':
                output_format = image.format

            # Crea el nombre del archivo de salida usando el job ID
            output_filename = f"{job_id}.{output_format.lower()}"
            output_path = os.path.join(UPLOAD_FOLDER, output_filename)

            # Guarda la imagen con la calidad especificada
            if output_format.upper() in ('JPEG', 'WEBP'):
                image.save(output_path, format=output_format, quality=quality, optimize=True)
            else:
                image.save(output_path, format=output_format, optimize=True)

            # Guarda el nombre del archivo original en Redis
            redis_conn.set(f"job:{job_id}:original_filename", filename)

            tasks_logger.info(f"Imagen procesada guardada en {output_path}")
            tasks_logger.info(f"Devolviendo nombre del archivo de salida: {output_filename}")
            return output_filename
        else:
            tasks_logger.info(f"Job {job_id} is already being processed by another worker.")
            return None

    except LockError as e:
        tasks_logger.error(f"Error adquiriendo lock para el trabajo {job_id}: {e}")
        return None
    except Exception as e:
        tasks_logger.error(f"Error al procesar la imagen {filename} desde la ruta {temp_file_path}: {e}")
        tasks_logger.error(f"Traceback: {e.__traceback__}")
        return None
    finally:
        # Limpieza: elimina el archivo temporal solo si el lock está adquirido
        if lock.owned():
            if os.path.exists(temp_file_path):
                try:
                    os.remove(temp_file_path)
                    tasks_logger.info(f"Archivo temporal eliminado: {temp_file_path}")
                except Exception as e:
                    tasks_logger.error(f"Error al eliminar el archivo temporal {temp_file_path}: {e}")
            else:
                tasks_logger.warning(f"Archivo temporal no encontrado: {temp_file_path}")
            lock.release()