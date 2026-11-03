FROM python:3-alpine
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY monitor.py .
CMD ["python", "monitor.py"]
