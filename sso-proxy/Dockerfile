FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py config.py store.py glitchtip.py ./
COPY routes/ ./routes/
# Numeric UID required for OpenShift SCC compatibility (cannot run as root)
RUN groupadd -g 1000 appuser && useradd -u 1000 -g appuser appuser
USER 1000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
