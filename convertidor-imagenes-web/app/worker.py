import os
import logging
import redis
from rq import Worker, Queue, get_current_job

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
    worker = Worker([Queue(connection=conn)], connection=conn, worker_ttl=360, job_monitoring_interval=5)
    worker.work() # Inicia el worker a escuchar los trabajos

if __name__ == '__main__':
    run_worker() # Inicia el worker si el script se ejecuta directamente