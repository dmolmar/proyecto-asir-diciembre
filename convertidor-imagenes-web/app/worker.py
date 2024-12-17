import logging
import os
import redis
from rq import Worker, Queue, get_current_job
from PIL import Image
from utils import resize_image
import uuid
import signal
import psutil
import time
import argparse
from kubernetes import client, config
from kubernetes.client.rest import ApiException

from opentelemetry import trace, metrics
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.instrumentation.redis import RedisInstrumentor

# Configure logging
logging.basicConfig(level=logging.DEBUG, handlers=[logging.StreamHandler()])
logging.getLogger('PIL').setLevel(logging.WARNING)

redis_url = os.getenv('REDIS_URL', 'redis://redis-service:6379')  # Get Redis URL from environment variable

UPLOAD_FOLDER = 'uploads'
TEMP_UPLOAD_FOLDER = 'temp_uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(TEMP_UPLOAD_FOLDER, exist_ok=True)

# Kubernetes API client (if needed for updating job status)
config.load_incluster_config()
batch_v1 = client.BatchV1Api()

# OpenTelemetry Setup
resource = Resource.create(attributes={"service.name": "worker-service"})

# Trace Provider
trace_provider = TracerProvider(resource=resource)
otlp_trace_exporter = OTLPSpanExporter(endpoint="opentelemetry-collector-service:4317", insecure=True)
trace_provider.add_span_processor(BatchSpanProcessor(otlp_trace_exporter))
trace.set_tracer_provider(trace_provider)
tracer = trace.get_tracer(__name__)

# Metrics Provider
metric_exporter = OTLPMetricExporter(endpoint="opentelemetry-collector-service:4317", insecure=True)
metric_reader = PeriodicExportingMetricReader(metric_exporter)
meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
metrics.set_meter_provider(meter_provider)
meter = metrics.get_meter(__name__)

# Instrumentations
RedisInstrumentor().instrument()

# Custom Metrics
jobs_counter = meter.create_counter(
    "jobs_processed_total",
    description="Total number of jobs processed"
)

jobs_duration_histogram = meter.create_histogram(
    "job_duration_seconds",
    description="Duration of job processing in seconds"
)

def process_image(file_path, output_format, quality, resolution_percentage, filename):
    with tracer.start_as_current_span("process_image") as span:
        job = get_current_job()
        if job:
            logging.info(f"Job {job.id}: Processing image: {filename}, formato: {output_format}, calidad: {quality}, resolucion: {resolution_percentage}")
            span.set_attribute("job.id", job.id)
        else:
            logging.info(f"Processing image: {filename}, formato: {output_format}, calidad: {quality}, resolucion: {resolution_percentage}")

        span.set_attribute("filename", filename)

        start_time = time.time()
        try:
            # Check if the file exists before processing
            if not os.path.exists(file_path):
                logging.error(f"Job: File not found: {file_path}")
                jobs_counter.add(1, {"status": "failure", "filename": filename})
                return {'success': False, 'error': 'File not found', 'filename': filename}

            image = Image.open(file_path)
            resized_image = resize_image(image, quality, resolution_percentage)
            if resized_image is None:
                logging.error(f"Job: Error during resizing for: {filename}")
                jobs_counter.add(1, {"status": "failure", "filename": filename})
                return {'success': False, 'error': 'Error during resizing', 'filename': filename}
            unique_id = str(uuid.uuid4())
            output_filename = f"converted_{unique_id}_{filename.split('.')[0]}.{output_format.lower() if output_format != 'ORIGINAL' else image.format.lower()}"
            output_path = os.path.join(UPLOAD_FOLDER, output_filename)
            save_format = output_format if output_format != 'ORIGINAL' else image.format.upper()
            if save_format == "ORIGINAL":
                save_format = "JPEG"
            if save_format not in ["JPEG", "PNG", "WEBP", "AVIF", "BMP", "TIFF", "ICO"]:
                save_format = "JPEG"
            resized_image.save(output_path, format=save_format, quality=quality)
            # Clean up temporary file
            os.remove(file_path)
            logging.info(f"Job: Finished processing image: {filename}")

            # Update Kubernetes Job metadata (optional, for tracking output in the job)
            if job:
                try:
                    job_from_cluster = batch_v1.read_namespaced_job(job.id, "default")  # Use appropriate namespace
                    if job_from_cluster:
                        job_from_cluster.metadata.annotations = {"output_filename": output_filename}
                        batch_v1.patch_namespaced_job(job.id, "default", job_from_cluster)
                except ApiException as e:
                    logging.error(f"Error updating job metadata: {e}")

            jobs_counter.add(1, {"status": "success", "filename": filename})
            return {'success': True, 'output_path': output_path, 'output_filename': output_filename}
        except Exception as e:
            logging.error(f"Job: Error processing image {filename}: {e}")
            jobs_counter.add(1, {"status": "failure", "filename": filename})
            return {'success': False, 'error': str(e), 'filename': filename}
        finally:
            duration = time.time() - start_time
            jobs_duration_histogram.record(duration, {"filename": filename})

def sigterm_handler(signum, frame):
    """Handles SIGTERM signal to gracefully shut down the worker."""
    logging.info("Worker received SIGTERM, shutting down gracefully...")
    raise SystemExit

def worker_init():
    """Initializes each worker process."""
    # Register the SIGTERM handler
    signal.signal(signal.SIGTERM, sigterm_handler)
    logging.info(f"Worker process (PID {os.getpid()}) initialized.")

def run_worker():
    """Sets up and starts an RQ worker."""
    worker_init()
    conn = redis.Redis.from_url(redis_url)
    queue = Queue(connection=conn)
    worker = Worker([queue], connection=conn)
    worker.work()

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Image Conversion Worker')
    parser.add_argument('file_path', type=str, help='Path to the input image file')
    parser.add_argument('output_format', type=str, help='Output format (e.g., JPEG, PNG)')
    parser.add_argument('quality', type=int, help='Image quality (1-100)')
    parser.add_argument('resolution_percentage', type=float, help='Resolution percentage (e.g., 0.5 for 50%)')
    parser.add_argument('filename', type=str, help='Original filename')

    args = parser.parse_args()

    # Call process_image directly with command-line arguments
    process_image(args.file_path, args.output_format, args.quality, args.resolution_percentage, args.filename)