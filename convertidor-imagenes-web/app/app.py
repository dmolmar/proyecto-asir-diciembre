import logging
from flask import Flask, render_template, request, send_file, jsonify, make_response, abort, url_for, redirect
import os
from werkzeug.utils import secure_filename
from PIL import Image
from io import BytesIO
import redis
from rq import Queue, Retry
from rq.exceptions import NoSuchJobError
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

app = Flask(__name__)

# Configure logging
handler = logging.StreamHandler()
handler.setLevel(logging.DEBUG)
app.logger.addHandler(handler)
app.logger.setLevel(logging.DEBUG)

UPLOAD_FOLDER = 'uploads'
TEMP_UPLOAD_FOLDER = 'temp_uploads'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'tiff', 'ico', 'avif', 'svg', 'psd', 'raw'}
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB
MAX_PIXELS = 10000 * 10000  # 100 megapíxeles
MAX_FILES = 50
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(TEMP_UPLOAD_FOLDER, exist_ok=True)
CLEANUP_INTERVAL = 60  # 1 Minute
SECRET_KEY = os.getenv('SECRET_KEY', 'Arcueid')
RESERVED_RAM_MB = 1024

# Redis setup (updated for Kubernetes service)
redis_conn = redis.Redis(host='redis-service', port=6379)  # Use the service name
q = Queue(connection=redis_conn, default_timeout=360)

# Thread pool executor for handling file uploads and initial processing
executor = ThreadPoolExecutor(max_workers=4)  # Adjust max_workers based on your system

# Mutex to ensure only one worker is active at a time
worker_lock = Lock()

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
metric_exporter = OTLPMetricExporter(endpoint="opentelemetry-collector-service:4317", insecure=True)
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

def create_kubernetes_job(file_path, output_format, quality, resolution_percentage, filename, request_id):
    job_name = f"image-job-{uuid.uuid4()}"
    job = client.V1Job(
        api_version="batch/v1",
        kind="Job",
        metadata=client.V1ObjectMeta(name=job_name, labels={"request-id": request_id}),
        spec=client.V1JobSpec(
            template=client.V1PodTemplateSpec(
                spec=client.V1PodSpec(
                    containers=[
                        client.V1Container(
                            name="worker-container",
                            image="your-docker-registry/images-api:latest",  # Replace with your image
                            command=["python", "-u", "worker.py"],
                            args=[file_path, output_format, str(quality), str(resolution_percentage), filename],
                            env=[
                                client.V1EnvVar(
                                    name="SECRET_KEY",
                                    value_from=client.V1EnvVarSource(
                                        secret_key_ref=client.V1SecretKeySelector(
                                            name="app-secrets",
                                            key="secret-key"
                                        )
                                    )
                                ),
                                # Add REDIS_URL environment variable for the worker
                                client.V1EnvVar(
                                    name="REDIS_URL",
                                    value="redis://redis-service:6379"
                                ),
                            ],
                            resources=client.V1ResourceRequirements(
                                requests={"cpu": "800m", "memory": "512Mi"},
                                limits={"cpu": "900m", "memory": "1Gi"},
                            ),
                            volume_mounts=[
                                client.V1VolumeMount(name="uploads", mount_path="/app/uploads"),
                                client.V1VolumeMount(name="temp-uploads", mount_path="/app/temp_uploads"),
                            ],
                        )
                    ],
                    restart_policy="Never",
                    volumes=[
                        client.V1Volume(name="uploads", persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(claim_name="uploads-pvc")),
                        client.V1Volume(name="temp-uploads", persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(claim_name="temp-uploads-pvc")),
                    ],
                )
            ),
            backoff_limit=3,  # Number of retries before the job fails
            ttl_seconds_after_finished=60,  # Optional: Delete completed jobs after 60 seconds
        )
    )
    try:
        batch_v1.create_namespaced_job(namespace="default", body=job)  # Use appropriate namespace
        return job_name
    except ApiException as e:
        app.logger.error(f"Error creating Kubernetes Job: {e}")
        return None

def process_uploaded_file(file, file_key, quality, resolution_percentage, output_format, request_id):
    if file and allowed_file(file.filename):
        if file.content_length > MAX_FILE_SIZE:
            return {'error': f'El archivo {file.filename} excede el tamaño máximo de {MAX_FILE_SIZE / (1024 * 1024)} MB.'}
        try:
            unique_filename = str(uuid.uuid4()) + '.' + file.filename.rsplit('.', 1)[1].lower()
            temp_path = os.path.join(TEMP_UPLOAD_FOLDER, unique_filename)
            file.save(temp_path)
            image = Image.open(temp_path)
            if image.width * image.height > MAX_PIXELS:
                os.remove(temp_path)
                return {'error': f'El archivo {file.filename} excede el tamaño máximo de {MAX_PIXELS / 1000000} MP.'}

            # Create Kubernetes Job instead of enqueuing in RQ
            job_name = create_kubernetes_job(temp_path, output_format, quality, resolution_percentage, file.filename, request_id)
            if job_name:
                return {
                    'job_name': job_name,
                    'file_info': {
                        'key': file_key,
                        'name': file.filename,
                        'extension': file.filename.rsplit('.', 1)[1].lower(),
                        'resolution': f"{image.width}x{image.height}",
                        'size': file.content_length,
                    }
                }
            else:
                return {'error': f"Error creating job for {file.filename}"}
        except Exception as e:
            app.logger.error(f"Error processing {file.filename}: {e}")
            return {'error': f"Error processing {file.filename}: {e}"}
    else:
        return {'error': f'Archivo {file.filename} no es permitido o no se pudo procesar.'}

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
        job_names = []
        uploaded_files_info = []

        try:
            check_resource_availability()  # Check resources before enqueuing
        except ResourceLimitExceeded as e:
            return jsonify({'error': str(e)}), 503  # 503 Service Unavailable

        for file in files:
            file_key = request.form.get('file_key')
            quality = int(request.form.get(f'quality-{file_key}', 95))
            resolution_percentage = float(request.form.get(f'resolution-{file_key}', 100)) / 100
            output_format = request.form.get(f'output_format-{file_key}', 'original').upper()

            # Submit the file processing to the executor
            future = executor.submit(process_uploaded_file, file, file_key, quality, resolution_percentage, output_format, request_id)
            result = future.result()  # Get the result from the future (blocking)

            if result and 'error' not in result:
                job_names.append(result['job_name'])
                uploaded_files_info.append(result['file_info'])
            elif result and 'error' in result:
                return jsonify({'error': result['error']}), 500

        # Store job names and request ID in Redis for tracking
        redis_conn.set(f"request:{request_id}:jobs", ",".join(job_names))
        redis_conn.set(f"request:{request_id}:total", len(job_names))
        redis_conn.set(f"request:{request_id}:completed", 0)

        app.logger.info(f"Request ID: {request_id}, Job Names: {job_names}")
        return jsonify({'request_id': request_id, 'uploaded_files_info': uploaded_files_info})

@app.route('/download/<job_name>')
@requires_auth
def download_file(job_name):
    try:
        # Get job details from Kubernetes (alternative to fetching from RQ)
        job = batch_v1.read_namespaced_job(job_name, "default")  # Replace "default" with the correct namespace
        if job.status.succeeded is not None and job.status.succeeded > 0:
            # Extract output path from job (you might need to adjust this based on how you store it in worker.py)
            output_filename = job.metadata.annotations.get("output_filename")
            if output_filename:
                output_path = os.path.join(UPLOAD_FOLDER, output_filename)
                if os.path.exists(output_path):
                    return send_file(output_path, download_name=output_filename, as_attachment=True)
                else:
                    return "File not found", 404
            else:
                return "Output filename not found in job metadata", 500
        else:
            return "Job not successful", 404
    except ApiException as e:
        app.logger.error(f"Error getting job status for download: {e}")
        return "Error retrieving job information", 500
    except Exception as e:
        app.logger.error(f"Error handling job {job_name}: {e}")
        return "Error interno del servidor", 500

@app.route('/file/<job_name>')
@requires_auth
def get_file(job_name):
    try:
        token = request.args.get('token')
        if token:
            try:
                token = unquote(token)
                jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
            except jwt.ExpiredSignatureError:
                return jsonify({'message': 'Token has expired!', 'error': 'expired_token'}), 401
            except jwt.InvalidTokenError:
                return jsonify({'message': 'Invalid token!', 'error': 'invalid_token'}), 401

        # Get job details from Kubernetes
        job = batch_v1.read_namespaced_job(job_name, "default")
        if job.status.succeeded is not None and job.status.succeeded > 0:
            # Extract output path from job metadata
            output_filename = job.metadata.annotations.get("output_filename")
            if output_filename:
                output_path = os.path.join(UPLOAD_FOLDER, output_filename)
                if os.path.exists(output_path):
                    return send_file(output_path, mimetype='image/jpeg', as_attachment=False, download_name=output_filename)
                else:
                    return "File not found", 404
            else:
                return "Output filename not found in job metadata", 500
        else:
            return "Job not successful", 404
    except ApiException as e:
        app.logger.error(f"Error getting job status for file retrieval: {e}")
        return jsonify({'status': 'error', 'result': False, 'message': 'Error retrieving job information'}), 500
    except Exception as e:
        app.logger.error(f"Error handling job {job_name}: {e}")
        return jsonify({'status': 'error', 'result': False, 'message': 'Internal server error'}), 500

@app.route('/progress/<request_id>')
@requires_auth
def progress(request_id):
    job_names_str = redis_conn.get(f"request:{request_id}:jobs")
    if not job_names_str:
        return jsonify({'status': 'not found'}), 404

    job_names = job_names_str.decode().split(",")
    completed_count = 0
    failed_count = 0
    statuses = []

    for job_name in job_names:
        try:
            job = batch_v1.read_namespaced_job_status(job_name, "default")  # Use appropriate namespace
            status = job.status
            if status.succeeded is not None and status.succeeded > 0:
                completed_count += 1
                statuses.append("succeeded")
            elif status.failed is not None and status.failed > 0:
                failed_count += 1
                statuses.append("failed")
            else:
                statuses.append("running")
        except ApiException as e:
            app.logger.error(f"Error getting job status: {e}")
            statuses.append("error")

    total_jobs = len(job_names)
    completed_percentage = int((completed_count / total_jobs) * 100) if total_jobs > 0 else 0

    # Update progress in Redis (optional, for more detailed tracking)
    redis_conn.set(f"request:{request_id}:completed", completed_count)

    return jsonify({
        'total_jobs': total_jobs,
        'completed_jobs': completed_count,
        'failed_jobs': failed_count,
        'statuses': statuses,
        'progress': completed_percentage,
        'status': 'finished' if completed_count + failed_count == total_jobs else 'processing'
    })

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