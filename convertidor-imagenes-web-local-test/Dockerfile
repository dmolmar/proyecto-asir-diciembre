# Usa la imagen base de Python 3.9 con una versión slim de Debian para un tamaño de imagen más pequeño
FROM python:3.9-slim-buster

# Establece el directorio de trabajo dentro del contenedor
WORKDIR /app

# Copia el archivo requirements.txt al directorio de trabajo
COPY requirements.txt .

# Instala las dependencias de Python especificadas en requirements.txt
RUN pip install -r requirements.txt

# Copia la carpeta 'app' al directorio de trabajo en el contenedor
COPY app /app

# Instala curl (para comprobaciones de salud) y lsof para la limpieza de archivos (opcional)
RUN apt-get update && apt-get install -y --no-install-recommends curl lsof telnet && rm -rf /var/lib/apt/lists/*

# Expone el puerto 5000 para que la aplicación sea accesible desde fuera del contenedor
EXPOSE 5000

# El comando por defecto al iniciar el contenedor ejecuta app.py
CMD ["python", "app.py"]