# Monitor: solo librería estándar de Python, cero dependencias externas.
FROM python:3.12-slim
WORKDIR /app
COPY monitor.py .
CMD ["python3", "-u", "monitor.py"]
