FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY exporter.py .

EXPOSE 8080
CMD ["python", "exporter.py"]
