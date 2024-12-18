import logging
from flask import Flask, render_template, request, send_file, jsonify, make_response, abort, url_for, redirect
import os
from werkzeug.utils import secure_filename
from PIL import Image
from io import BytesIO
import redis
from rq import Queue, Retry
from rq.exceptions import NoSuchJobError
import redis.exceptions
import zipfile
from datetime import datetime, timedelta
import base64
import uuid
from threading import Thread, Lock
import time
import signal
import sys
import jwt
from functools import wraps
import psutil
from urllib.parse import unquote
from concurrent.futures import ThreadPoolExecutor
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
from opentelemetry.instrumentation.flask import FlaskInstrumentor
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.instrumentation.wsgi import collect_request_attributes

from tasks import process_image

app = Flask(__name__)

# Configure logging
handler = logging.StreamHandler()
handler.setLevel(logging.DEBUG)
app.logger.addHandler(handler)
app.logger.setLevel(logging.DEBUG)

# Set environment variables for paths (needed for tasks.py)
os.environ['UPLOAD_FOLDER'] = 'uploads'
os.environ['TEMP_UPLOAD_FOLDER'] = 'temp_uploads'

UPLOAD_FOLDER = 'uploads'
TEMP_UPLOAD_FOLDER = 'temp_uploads'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'tiff', 'ico', 'avif', 'svg', 'psd', 'raw'}
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB
MAX_PIXELS = 20000000  # 20 megapíxeles
MAX_FILES = 50
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(TEMP_UPLOAD_FOLDER, exist_ok=True)
CLEANUP_INTERVAL = 60  # 1 Minute
SECRET_KEY = os.getenv('SECRET_KEY', 'Arcueid')
RESERVED_RAM_MB = 256

# Redis setup (updated for Kubernetes service)
redis_conn = redis.Redis(host='redis-service', port=6379)  # Use the service name
q = Queue(connection=redis_conn, default_timeout=360)

# Kubernetes API client
config.load_incluster_config()  # Use incluster config when running inside Kubernetes
batch_v1 = client.BatchV1Api()

# OpenTelemetry Setup
resource = Resource.create(attributes={"service.name": "images-api-service"})

# Trace Provider
trace_provider = TracerProvider(resource=resource)
otlp_trace_exporter = OTLPSpanExporter(endpoint="opentelemetry-collector-service:4317", insecure=True)
trace_provider.add_span_processor(BatchSpanProcessor(otlp_trace_exporter))
trace.set_tracer_provider(trace_provider)
tracer = trace.get_tracer(__name__)

# Metrics Provider
metric_exporter = OTLPMetricExporter(endpoint="opentelemetry-collector-service:4318", insecure=True)
metric_reader = PeriodicExportingMetricReader(metric_exporter)
meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
metrics.set_meter_provider(meter_provider)
meter = metrics.get_meter(__name__)

# Instrumentations
FlaskInstrumentor().instrument_app(app, excluded_urls="health")  # Exclude health checks
RequestsInstrumentor().instrument()
RedisInstrumentor().instrument()

# Custom metric to count requests
request_counter = meter.create_counter(
    "requests_total",
    description="Total number of requests"
)

def check_resource_availability():
    """Checks if adding another worker would exceed resource limits."""
    available_ram_mb = psutil.virtual_memory().available / (1024 * 1024)
    app.logger.info(f"Available RAM: {available_ram_mb:.2f} MB")

    # Check if available RAM is less than the reserved amount plus a small buffer
    if available_ram_mb < RESERVED_RAM_MB + 32:  # 32 MB buffer
        app.logger.warning("Insufficient RAM available.")
        raise ResourceLimitExceeded(f"Insufficient RAM available. Available: {available_ram_mb:.2f} MB, Reserved: {RESERVED_RAM_MB} MB")

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def cleanup_old_files():
    while True:
        app.logger.info("Cleaning up old files")
        for folder in [UPLOAD_FOLDER, TEMP_UPLOAD_FOLDER]:
            for filename in os.listdir(folder):
                file_path = os.path.join(folder, filename)
                if os.path.isfile(file_path):
                    try:
                        # Check if file is being used by a worker (using lsof as psutil is not reliable)
                        is_in_use = os.system(f"lsof | grep {file_path} > /dev/null") == 0

                        if not is_in_use:
                            file_creation_time = os.path.getctime(file_path)
                            if time.time() - file_creation_time > CLEANUP_INTERVAL:
                                os.remove(file_path)
                                app.logger.info(f"Removed expired file: {filename}")
                        else:
                            app.logger.info(f"File {filename} is currently in use, skipping cleanup.")
                    except FileNotFoundError:
                        app.logger.info(f"File not found during cleanup: {filename}")
        time.sleep(CLEANUP_INTERVAL)

def clear_redis_queue():
    app.logger.info("Clearing Redis queue")
    q.empty()

def signal_handler(sig, frame):
    app.logger.info("Gracefully shutting down...")
    # Add any cleanup logic here
    sys.exit(0)

# Start the cleanup thread
cleanup_thread = Thread(target=cleanup_old_files, daemon=True)
cleanup_thread.start()
clear_redis_queue()

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# Error class
class ResourceLimitExceeded(Exception):
    pass

def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = None
        if 'Authorization' in request.headers:
            token = request.headers['Authorization'].split(" ")[1]
        if not token:
            token = request.args.get('token')
            if token:
                token = unquote(token)
        if not token:
            return jsonify({'message': 'Authentication Token is missing!', 'error': 'missing_token'}), 401
        try:
            data = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        except jwt.ExpiredSignatureError:
            return jsonify({'message': 'Token has expired!', 'error': 'expired_token'}), 401
        except jwt.InvalidTokenError:
            return jsonify({'message': 'Invalid token!', 'error': 'invalid_token'}), 401
        return f(*args, **kwargs)
    return decorated

@app.route('/', methods=['GET', 'POST'])
def index():
    return render_template('index.html')

@app.route('/convert', methods=['POST'])
@requires_auth
def convert():
    with tracer.start_as_current_span("convert-route"):
        request_counter.add(1, {"endpoint": "/convert"})
        if 'files' not in request.files:
            return jsonify({'error': 'No se han subido archivos.'}), 400

        files = request.files.getlist('files')
        if not files:
            return jsonify({'error': 'No se han subido archivos.'}), 400

        if len(files) > MAX_FILES:
            return jsonify({'error': f'Se ha excedido el número máximo de archivos ({MAX_FILES}).'}), 400

        request_id = str(uuid.uuid4())  # Generate a unique ID for this request
        job_ids = []
        uploaded_files_info = []
        
        try:
            check_resource_availability()  # Check resources before enqueuing
        except ResourceLimitExceeded as e:
             return jsonify({'error': str(e)}), 503
        
        for file in files:
            file_key = request.form.get('file_key')
            quality = int(request.form.get(f'quality-{file_key}', 95))
            resolution_percentage = float(request.form.get(f'resolution-{file_key}', 100)) / 100
            output_format = request.form.get(f'output_format-{file_key}', 'original').upper()

            if file and allowed_file(file.filename):
                if file.content_length > MAX_FILE_SIZE:
                   return jsonify({'error': f'El archivo {file.filename} excede el tamaño máximo de {MAX_FILE_SIZE / (1024 * 1024)} MB.'}), 400
                try:
                    # Using UUID for temporary file name, but storing original filename in Redis
                    unique_filename = str(uuid.uuid4()) + '.' + file.filename.rsplit('.', 1)[1].lower()
                    temp_path = os.path.join(TEMP_UPLOAD_FOLDER, unique_filename)
                    file.save(temp_path)
                    
                    image = Image.open(temp_path)
                    if image.width * image.height > MAX_PIXELS:
                        os.remove(temp_path)
                        return jsonify({'error': f'El archivo {file.filename} excede el tamaño máximo de {MAX_PIXELS / 1000000} MP.'}), 400

                    # Enqueue the job with retries
                    job = q.enqueue(process_image, args=(temp_path, output_format, quality, resolution_percentage, file.filename),
                                    job_timeout=360,
                                    result_ttl=360,
                                    failure_ttl=360,
                                    retry=Retry(max=3, interval=[10, 30, 60]))
                    job_id = job.id
                    job_ids.append(job_id)
                    app.logger.info(f"Enqueued job: {job_id} for file: {file.filename}")

                    # Store the mapping between job ID and original filename in Redis
                    redis_conn.set(f"job:{job_id}:filename", file.filename)

                    uploaded_files_info.append({
                        'key': file_key,
                        'name': file.filename,
                        'extension': file.filename.rsplit('.', 1)[1].lower(),
                        'resolution': f"{image.width}x{image.height}",
                        'size': file.content_length,
                    })
                except Exception as e:
                    app.logger.error(f"Error processing {file.filename}: {e}")
                    return jsonify({'error': f"Error processing {file.filename}: {e}"}), 500

        # Store job names and request ID in Redis for tracking
        redis_conn.set(f"request:{request_id}:jobs", ",".join(job_ids))
        redis_conn.set(f"request:{request_id}:total", len(job_ids))
        redis_conn.set(f"request:{request_id}:completed", 0)

        # Create a results dictionary that includes job IDs
        results = {job_id: {'status': 'processing'} for job_id in job_ids}

        app.logger.info(f"Request ID: {request_id}, Job Names: {job_ids}")
        return jsonify({'request_id': request_id, 'uploaded_files_info': uploaded_files_info, 'results': results})

@app.route('/download/<job_id>')
@requires_auth
def download_file(job_id):
    with tracer.start_as_current_span("download-file-route"):
        try:
            job = q.fetch_job(job_id)
            if job is None:
                abort(404, description="Job not found")
            if job.is_failed:
                abort(500, description=f"Job failed: {job.exc_info}")
            if not job.is_finished:
                abort(400, description="Job not finished yet")

            output_filename = job.result
            if not output_filename:
                abort(500, description="Output filename not found")

            file_path = os.path.join(app.config['UPLOAD_FOLDER'], output_filename)
            if not os.path.exists(file_path):
                abort(404, description=f"File not found: {output_filename}")

            response = send_file(file_path, as_attachment=True, download_name=output_filename)
            return response
        except NoSuchJobError:
            abort(404, description="Job not found")
        except Exception as e:
            app.logger.error(f"Error downloading file: {e}")
            abort(500, description=f"Error downloading file: {e}")

@app.route('/file/<job_id>')
@requires_auth
def get_file(job_id):
    with tracer.start_as_current_span("get-file-route"):
        try:
            job = q.fetch_job(job_id)
            if job is None:
                abort(404, description="Job not found")
            if job.is_failed:
                abort(500, description=f"Job failed: {job.exc_info}")
            if not job.is_finished:
                abort(400, description="Job not finished yet")

            output_filename = job.result
            if not output_filename:
                abort(500, description="Output filename not found")

            file_path = os.path.join(app.config['UPLOAD_FOLDER'], output_filename)

            if not os.path.exists(file_path):
                abort(404, description=f"File not found: {output_filename}")
            
            return send_file(file_path, mimetype='image/jpeg')
        except NoSuchJobError:
            abort(404, description="Job not found")
        except Exception as e:
            app.logger.error(f"Error getting file: {e}")
            abort(500, description=f"Error getting file: {e}")

@app.route('/progress/<request_id>')
@requires_auth
def progress(request_id):
    with tracer.start_as_current_span("progress-route"):
        try:
            job_ids_str = redis_conn.get(f"request:{request_id}:jobs")
            if job_ids_str is None:
                return jsonify({'error': 'Request ID not found.'}), 404

            job_ids = job_ids_str.decode().split(",")
            total_jobs = len(job_ids)
            completed_jobs = 0
            failed_jobs = 0
            results = {}

            for job_id in job_ids:
                try:
                    job = q.fetch_job(job_id)
                    if job is None:
                        failed_jobs += 1
                        results[job_id] = {'status': 'not found'}
                        continue

                    if job.is_finished:
                        completed_jobs += 1
                        # Get the original filename from Redis
                        original_filename = redis_conn.get(f"job:{job_id}:filename").decode()
                        results[job_id] = {'status': 'finished', 'result': job.result, 'original_filename': original_filename}
                    elif job.is_failed:
                        failed_jobs += 1
                        results[job_id] = {'status': 'failed', 'error': job.exc_info}
                    else:
                        results[job_id] = {'status': 'processing'}
                except NoSuchJobError:
                    failed_jobs += 1
                    results[job_id] = {'status': 'not found'}
                except redis.exceptions.ConnectionError as e:
                    app.logger.error(f"Redis connection error: {e}")
                    return jsonify({'error': 'Redis connection error'}), 500

            all_finished = completed_jobs + failed_jobs == total_jobs

            progress_data = {
                'total': total_jobs,
                'completed': completed_jobs,
                'failed': failed_jobs,
                'all_finished': all_finished,
                'results': results
            }

            return jsonify(progress_data)

        except Exception as e:
            app.logger.error(f"An unexpected error occurred: {e}")
            return jsonify({'error': 'An unexpected error occurred on the server.'}), 500

@app.errorhandler(Exception)
def handle_unexpected_error(error):
    app.logger.error(f"An unexpected error occurred: {error}")
    return jsonify({'error': 'An unexpected error occurred on the server.'}), 500

@app.route('/auth', methods=['POST'])
def authenticate():
    app.logger.info("Auth endpoint called")
    username = request.json.get('username')
    password = request.json.get('password')
    app.logger.info(f"Received credentials: {username}, {password}")
    if username == 'user' and password == 'pass':
        payload = {
            'sub': username,
            'iat': datetime.utcnow(),
            'exp': datetime.utcnow() + timedelta(hours=1)
        }
        token = jwt.encode(payload, SECRET_KEY, algorithm='HS256')
        return jsonify({'token': token})
    else:
        return jsonify({'error': 'Invalid credentials'}), 401

@app.route('/logout')
def logout():
    # Invalidate the token on the client-side (remove from local storage)
    response = make_response(jsonify({'message': 'Logout successful'}))
    return response

@app.route('/health')
def health_check():
    return jsonify({'status': 'ok'}), 200

if __name__ == '__main__':
    try:
        app.run(debug=True, host='0.0.0.0', port=5000)
    except Exception as e:
        app.logger.error(f"Failed to start server: {e}")