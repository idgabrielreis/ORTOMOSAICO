FROM python:3.11-slim

# GDAL, PROJ e as bibliotecas nativas do OpenCV precisam existir no sistema:
# rasterio e opencv trazem os binários nas wheels, mas dependem destas libs.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 libexpat1 curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt celery[redis]==5.4.0 "psycopg[binary]==3.2.3"

COPY . .

ENV PYTHONUNBUFFERED=1 ORTO_STORAGE_ROOT=/storage
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
