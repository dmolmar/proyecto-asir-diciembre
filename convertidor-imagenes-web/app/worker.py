import os
import logging
import redis
from rq import Worker, Queue, get_current_job, Retry

# Configuración básica del logger para el worker
logging.basicConfig(level=logging.DEBUG, handlers=[logging.StreamHandler()])
worker_logger = logging.getLogger("worker")

def run_worker():
    """Función para iniciar el worker de RQ, que procesa los trabajos de la cola de Redis."""
    # Obtiene la URL de Redis desde la variable de entorno
    redis_url = os.getenv('REDIS_URL', 'redis://redis-service:6379')
    conn = redis.from_url(redis_url)
    worker_logger.info("Worker conectado a Redis.")

    # Crea el worker con la conexión a Redis y la configuración deseada
    worker = Worker([Queue(connection=conn)], connection=conn, worker_ttl=360, job_monitoring_interval=5, exception_handlers=[retry_handler])
    worker.work() # Inicia el worker a escuchar los trabajos

def retry_handler(job, exc_type, exc_value, traceback):
    """Manejador de excepciones para reintentar trabajos fallidos."""
    MAX_RETRIES = 3  # Número máximo de reintentos

    # Inicializa el contador de reintentos si es la primera vez que falla
    if job.meta.get('retry_count', 0) == 0:
        job.meta['retry_count'] = 0

    # Incrementa el contador de reintentos
    job.meta['retry_count'] += 1

    # Verifica si se ha alcanzado el límite de reintentos
    if job.meta['retry_count'] >= MAX_RETRIES:
        worker_logger.error(f"Job {job.id} failed after max retries.")
        job.save() # Guarda el estado del trabajo
        return True  # Indica que el trabajo ha fallado definitivamente

    worker_logger.warning(f"Job {job.id} failed with error: {exc_value}. Retrying ({job.meta['retry_count']}/{MAX_RETRIES})...")

    # Reencola el trabajo para un nuevo intento
    job.requeue()

    return False  # Indica que el trabajo debe ser reencolado

if __name__ == '__main__':
    run_worker() # Inicia el worker si el script se ejecuta directamente