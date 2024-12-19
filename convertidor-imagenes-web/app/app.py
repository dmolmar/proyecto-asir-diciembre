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
import sqlite3

from tasks import process_image

# Inicialización de la aplicación Flask
app = Flask(__name__)

# Configuración del logging
handler = logging.StreamHandler()
handler.setLevel(logging.DEBUG)
app.logger.addHandler(handler)
app.logger.setLevel(logging.DEBUG)

# Variables de entorno para las rutas de subida y temporales
UPLOAD_FOLDER = '/data/uploads'
TEMP_UPLOAD_FOLDER = '/data/temp-uploads'
os.environ['UPLOAD_FOLDER'] = UPLOAD_FOLDER
os.environ['TEMP_UPLOAD_FOLDER'] = TEMP_UPLOAD_FOLDER

# Extensiones permitidas para los archivos de imagen
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'tiff', 'ico', 'avif', 'svg', 'psd', 'raw'}
# Tamaño máximo permitido para los archivos en bytes
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB
# Número máximo de píxeles permitidos para una imagen
MAX_PIXELS = 20000000  # 20 megapíxeles
# Número máximo de archivos permitidos por solicitud
MAX_FILES = 20
# Configuración de la carpeta de subida
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
# Crea las carpetas de subida y temporales si no existen
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(TEMP_UPLOAD_FOLDER, exist_ok=True)
# Intervalo de limpieza de archivos antiguos (en segundos)
CLEANUP_INTERVAL = 120  # 1 Minute
# Clave secreta para JWT, obtenida de variables de entorno o una clave por defecto
SECRET_KEY = os.getenv('SECRET_KEY', 'Arcueid')
# Cantidad de RAM reservada en MB para evitar sobrecargas
RESERVED_RAM_MB = 256

# Configuración de la base de datos SQLite
DATABASE_FILE = '/data/sqlite/images.db'
DATABASE_URL = f'sqlite:///{DATABASE_FILE}'

# Conexión global a la base de datos
db_conn = None

# Configuración de Redis (actualizado para Kubernetes service)
redis_conn = redis.Redis(host='redis-service', port=6379)
# Cola de trabajos para el procesamiento de imágenes
q = Queue(connection=redis_conn, default_timeout=360)

# Cliente de la API de Kubernetes
config.load_incluster_config()
batch_v1 = client.BatchV1Api()

def get_db_connection():
    """Establece y devuelve una conexión a la base de datos SQLite."""
    global db_conn
    if db_conn is None:
        try:
            # Conecta a la base de datos y permite el uso de la conexión entre threads
            db_conn = sqlite3.connect(DATABASE_FILE, check_same_thread=False)
            # Crea la tabla de usuarios si no existe
            db_conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL,
                    password TEXT NOT NULL
                )
            """)
            db_conn.commit()
            app.logger.info("Conexión a la base de datos establecida.")
        except Exception as e:
            app.logger.error(f"Fallo en la conexión a la base de datos: {e}")
            raise e
    return db_conn

def close_db_connection(signal, frame):
    """Cierra la conexión a la base de datos y finaliza la aplicación."""
    global db_conn
    if db_conn:
        db_conn.close()
        app.logger.info("Conexión a la base de datos cerrada.")
    sys.exit(0)

def check_resource_availability():
    """Verifica si hay suficientes recursos (RAM) disponibles antes de encolar un nuevo trabajo."""
    available_ram_mb = psutil.virtual_memory().available / (1024 * 1024)
    app.logger.info(f"RAM disponible: {available_ram_mb:.2f} MB")

    # Verifica si la RAM disponible es menor que la cantidad reservada más un pequeño buffer
    if available_ram_mb < RESERVED_RAM_MB + 32:  # 32 MB buffer
        app.logger.warning("RAM disponible insuficiente.")
        raise ResourceLimitExceeded(f"RAM disponible insuficiente. Disponible: {available_ram_mb:.2f} MB, Reservada: {RESERVED_RAM_MB} MB")

def allowed_file(filename):
    """Verifica si un archivo tiene una extensión permitida."""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def cleanup_old_files():
    """Limpia periódicamente los archivos temporales antiguos y los archivos procesados."""
    while True:
        app.logger.info("Limpiando archivos antiguos")
        for folder in [UPLOAD_FOLDER, TEMP_UPLOAD_FOLDER]:
            for filename in os.listdir(folder):
                file_path = os.path.join(folder, filename)
                if os.path.isfile(file_path):
                    try:
                        # Verifica si el archivo está siendo usado por un worker
                        is_in_use = os.system(f"lsof | grep {file_path} > /dev/null") == 0

                        if not is_in_use:
                            # Obtiene la hora de creación del archivo
                            file_creation_time = os.path.getctime(file_path)
                            # Si el archivo tiene más tiempo que el intervalo de limpieza, lo borra
                            if time.time() - file_creation_time > CLEANUP_INTERVAL:
                                os.remove(file_path)
                                app.logger.info(f"Eliminado archivo expirado: {filename}")
                        else:
                            app.logger.info(f"Archivo {filename} está en uso, omitiendo limpieza.")
                    except FileNotFoundError:
                        app.logger.info(f"Archivo no encontrado durante la limpieza: {filename}")
        time.sleep(CLEANUP_INTERVAL)

def clear_redis_queue():
    """Limpia la cola de Redis al inicio."""
    app.logger.info("Limpiando la cola de Redis")
    q.empty()

def signal_handler(sig, frame):
    """Manejador de señales para un cierre elegante."""
    app.logger.info("Apagando la aplicación elegantemente...")
    # Agrega cualquier lógica de limpieza aquí
    sys.exit(0)

# Inicia el hilo de limpieza de archivos
cleanup_thread = Thread(target=cleanup_old_files, daemon=True)
cleanup_thread.start()
clear_redis_queue()

# Configura los manejadores de señales
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# Clase de error para límites de recursos
class ResourceLimitExceeded(Exception):
    pass

def requires_auth(f):
    """Decorador para requerir autenticación JWT en las rutas."""
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
            return jsonify({'message': '¡Falta el Token de Autenticación!', 'error': 'missing_token'}), 401
        try:
            data = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        except jwt.ExpiredSignatureError:
            return jsonify({'message': '¡El token ha expirado!', 'error': 'expired_token'}), 401
        except jwt.InvalidTokenError:
            return jsonify({'message': '¡Token inválido!', 'error': 'invalid_token'}), 401
        return f(*args, **kwargs)
    return decorated

@app.route('/', methods=['GET', 'POST'])
def index():
    """Ruta principal que muestra el formulario HTML."""
    return render_template('index.html')

@app.route('/convert', methods=['POST'])
@requires_auth
def convert():
    """Ruta para procesar y convertir imágenes. Requiere autenticación."""
    try:
        if 'files' not in request.files:
            return jsonify({'error': 'No se han subido archivos.'}), 400

        files = request.files.getlist('files')
        file_keys = request.form.getlist('file_keys[]')

        if not files:
            return jsonify({'error': 'No se han subido archivos.'}), 400

        if len(files) > MAX_FILES:
            return jsonify({'error': f'Se ha excedido el número máximo de archivos ({MAX_FILES}).'}), 400

        request_id = str(uuid.uuid4())  # Genera un ID único para esta solicitud
        job_ids = []
        uploaded_files_info = []

        try:
            check_resource_availability()  # Verifica los recursos antes de encolar
        except ResourceLimitExceeded as e:
             return jsonify({'error': str(e)}), 503

        # Asocia cada archivo con sus metadatos usando el orden de file_keys
        for i, file in enumerate(files):
            file_key = file_keys[i] if i < len(file_keys) else file.filename
            quality = int(request.form.get(f'quality-{file_key}', 95))
            resolution_percentage = float(request.form.get(f'resolution-{file_key}', 100)) / 100
            output_format = request.form.get(f'output_format-{file_key}', 'original').upper()

            if file and allowed_file(file.filename):
                if file.content_length > MAX_FILE_SIZE:
                    return jsonify({'error': f'El archivo {file.filename} excede el tamaño máximo de {MAX_FILE_SIZE / (1024 * 1024)} MB.'}), 400
                try:
                    # Encola el trabajo en Redis
                    job = q.enqueue(process_image, args=(output_format, quality, resolution_percentage, file.filename),
                                    job_timeout=360,
                                    result_ttl=360,
                                    failure_ttl=360,
                                    retry=Retry(max=3, interval=[10, 30, 60]))
                    job.retries_left = 3 
                    job_id = job.id
                    job_ids.append(job_id)
                    app.logger.info(f"Trabajo encolado: {job_id} para archivo: {file.filename}")

                    # Crea un nombre de archivo temporal único usando job_id
                    unique_filename = f"{job_id}.{file.filename.rsplit('.', 1)[1].lower()}"
                    temp_path = os.path.join(TEMP_UPLOAD_FOLDER, unique_filename)

                    # Guarda el archivo temporalmente
                    file.seek(0) # Resetea el puntero del archivo
                    file.save(temp_path)

                    # Guarda el mapeo entre ID de trabajo y nombre de archivo original en Redis
                    redis_conn.set(f"job:{job_id}:filename", file.filename)
                    redis_conn.set(f"job:{job_id}:filekey", file_key)
                    redis_conn.set(f"job:{job_id}:temppath", temp_path)

                    image = Image.open(temp_path)
                    if image.width * image.height > MAX_PIXELS:
                        os.remove(temp_path)
                        return jsonify({'error': f'El archivo {file.filename} excede el tamaño máximo de {MAX_PIXELS / 1000000} MP.'}), 400

                    uploaded_files_info.append({
                        'key': file_key,
                        'name': file.filename,
                        'extension': file.filename.rsplit('.', 1)[1].lower(),
                        'resolution': f"{image.width}x{image.height}",
                        'size': file.content_length,
                    })
                except Exception as e:
                    app.logger.error(f"Error al procesar {file.filename}: {e}")
                    return jsonify({'error': f"Error al procesar {file.filename}: {e}"}), 500

        # Guarda los nombres de los trabajos y el ID de la solicitud en Redis
        redis_conn.set(f"request:{request_id}:jobs", ",".join(job_ids))
        redis_conn.set(f"request:{request_id}:total", len(job_ids))
        redis_conn.set(f"request:{request_id}:completed", 0)

        # Crea un diccionario de resultados que incluye los ID de los trabajos
        results = {job_id: {'status': 'processing'} for job_id in job_ids}

        app.logger.info(f"ID de solicitud: {request_id}, Nombres de trabajo: {job_ids}")
        return jsonify({'request_id': request_id, 'uploaded_files_info': uploaded_files_info, 'results': results})
    except Exception as e:
        app.logger.error(f"Error inesperado en /convert: {e}")
        return jsonify({'error': 'Ocurrió un error inesperado en el servidor.'}), 500

@app.route('/download/<job_id>')
@requires_auth
def download_file(job_id):
    """Ruta para descargar un archivo procesado. Requiere autenticación."""
    try:
        job = q.fetch_job(job_id)
        if job is None:
            abort(404, description="Trabajo no encontrado")
        if job.is_failed:
            abort(500, description=f"El trabajo falló: {job.exc_info}")
        if not job.is_finished:
            abort(400, description="El trabajo aún no ha terminado")

        # Obtiene el nombre del archivo de salida y el nombre original
        output_filename = job.result
        original_filename = redis_conn.get(f"job:{job_id}:original_filename").decode()

        if not output_filename:
            abort(500, description="No se encontró el nombre del archivo de salida")

        file_path = os.path.join(UPLOAD_FOLDER, output_filename)
        if not os.path.exists(file_path):
            abort(404, description=f"Archivo no encontrado: {output_filename}")

        # Usa el nombre original para la descarga
        response = send_file(file_path, as_attachment=True, download_name=original_filename)
        return response

    except NoSuchJobError:
        abort(404, description="Trabajo no encontrado")
    except Exception as e:
        app.logger.error(f"Error al descargar el archivo: {e}")
        abort(500, description=f"Error al descargar el archivo: {e}")

@app.route('/file/<job_id>')
@requires_auth
def get_file(job_id):
    """Ruta para obtener un archivo procesado como imagen para mostrar en el navegador. Requiere autenticación."""
    try:
        job = q.fetch_job(job_id)
        if job is None:
            abort(404, description="Trabajo no encontrado")
        if job.is_failed:
            abort(500, description=f"El trabajo falló: {job.exc_info}")
        if not job.is_finished:
            abort(400, description="El trabajo aún no ha terminado")

        output_filename = job.result
        if not output_filename:
            abort(500, description="No se encontró el nombre del archivo de salida")

        file_path = os.path.join(UPLOAD_FOLDER, output_filename)

        if not os.path.exists(file_path):
            abort(404, description=f"Archivo no encontrado: {output_filename}")

        return send_file(file_path, mimetype='image/jpeg')
    except NoSuchJobError:
        abort(404, description="Trabajo no encontrado")
    except Exception as e:
        app.logger.error(f"Error al obtener el archivo: {e}")
        abort(500, description=f"Error al obtener el archivo: {e}")

@app.route('/progress/<request_id>')
@requires_auth
def progress(request_id):
    """Ruta para obtener el progreso de la conversión. Requiere autenticación."""
    try:
        job_ids_str = redis_conn.get(f"request:{request_id}:jobs")
        if job_ids_str is None:
            return jsonify({'error': 'ID de solicitud no encontrado.'}), 404

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
                    # Obtiene el nombre de archivo original de Redis
                    original_filename = redis_conn.get(f"job:{job_id}:filename").decode()
                    file_key = redis_conn.get(f"job:{job_id}:filekey").decode()
                    results[job_id] = {'status': 'finished', 'result': job.result, 'original_filename': original_filename, 'filekey': file_key}
                elif job.is_failed:
                    failed_jobs += 1
                    results[job_id] = {'status': 'failed', 'error': job.exc_info}
                else:
                    results[job_id] = {'status': 'processing'}
            except NoSuchJobError:
                failed_jobs += 1
                results[job_id] = {'status': 'not found'}
            except redis.exceptions.ConnectionError as e:
                app.logger.error(f"Error de conexión a Redis: {e}")
                return jsonify({'error': 'Error de conexión a Redis'}), 500

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
        app.logger.error(f"Ocurrió un error inesperado: {e}")
        return jsonify({'error': 'Ocurrió un error inesperado en el servidor.'}), 500

@app.errorhandler(Exception)
def handle_unexpected_error(error):
    """Maneja cualquier error inesperado en la aplicación."""
    app.logger.error(f"Ocurrió un error inesperado: {error}")
    return jsonify({'error': 'Ocurrió un error inesperado en el servidor.'}), 500

@app.route('/register', methods=['POST'])
def register():
    """Ruta para registrar un nuevo usuario."""
    app.logger.info("Endpoint de registro llamado")
    username = request.json.get('username')
    password = request.json.get('password')
    app.logger.info(f"Credenciales recibidas: {username}, {password}")

    if not username or not password:
        return jsonify({'error': 'Se requieren nombre de usuario y contraseña'}), 400

    try:
        conn = get_db_connection()
        cur = conn.cursor() # Crea un objeto cursor
        cur.execute("INSERT INTO users (username, password) VALUES (?, ?)", (username, password))
        conn.commit()
        cur.close() # Cierra el cursor
        return jsonify({'message': 'Usuario registrado exitosamente'}), 201
    except sqlite3.IntegrityError:
        return jsonify({'error': 'El nombre de usuario ya existe'}), 409
    except Exception as e:
        app.logger.error(f"Error durante el registro: {e}")
        return jsonify({'error': 'Ocurrió un error durante el registro'}), 500

@app.route('/auth', methods=['POST'])
def authenticate():
    """Ruta para autenticar un usuario y generar un token JWT."""
    app.logger.info("Endpoint de autenticación llamado")
    username = request.json.get('username')
    password = request.json.get('password')
    app.logger.info(f"Credenciales recibidas: {username}, {password}")

    if not username or not password:
        return jsonify({'error': 'Se requieren nombre de usuario y contraseña'}), 400

    try:
        conn = get_db_connection()
        cur = conn.cursor() # Crea un objeto cursor
        cur.execute("SELECT password FROM users WHERE username = ?", (username,))
        result = cur.fetchone()

        if result and result[0] == password:
            payload = {
                'sub': username,
                'iat': datetime.utcnow(),
                'exp': datetime.utcnow() + timedelta(hours=1)
            }
            token = jwt.encode(payload, SECRET_KEY, algorithm='HS256')
            cur.close() # Cierra el cursor
            return jsonify({'token': token})
        else:
            cur.close() # Cierra el cursor
            return jsonify({'error': 'Credenciales inválidas'}), 401
    except Exception as e:
        app.logger.error(f"Error durante la autenticación: {e}")
        return jsonify({'error': 'Ocurrió un error durante la autenticación'}), 500

@app.route('/logout')
def logout():
    """Ruta para cerrar la sesión del usuario."""
    # Invalida el token en el lado del cliente (remueve del almacenamiento local)
    response = make_response(jsonify({'message': 'Cierre de sesión exitoso'}))
    return response

@app.route('/health')
def health_check():
    """Ruta para la comprobación de salud."""
    return jsonify({'status': 'ok'}), 200

# Manejador de señales para un cierre elegante
signal.signal(signal.SIGINT, close_db_connection)
signal.signal(signal.SIGTERM, close_db_connection)

if __name__ == '__main__':
    try:
        conn = get_db_connection() # Crea una conexión al inicio
        app.run(debug=True, host='0.0.0.0', port=5000)
    except Exception as e:
        app.logger.error(f"Fallo al iniciar el servidor: {e}")