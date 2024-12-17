import os
import logging
import redis
from rq import Worker, Queue

from tasks import process_image

logging.basicConfig(level=logging.DEBUG, handlers=[logging.StreamHandler()])
worker_logger = logging.getLogger("worker")

def run_worker():
    redis_url = os.getenv('REDIS_URL', 'redis://redis-service:6379')
    conn = redis.from_url(redis_url)
    worker_logger.info("Worker connected to Redis.")

    # Provide connection to Queue as well
    worker = Worker([Queue(connection=conn)], connection=conn)
    worker.work()

if __name__ == '__main__':
    run_worker()