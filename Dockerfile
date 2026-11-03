# Monitor: Python sobre Alpine (~50 MB de imagen, frente a ~120 MB de slim)
FROM python:3.12-alpine

# tzdata = horas locales en los mensajes (TZ la fija el compose)
# ca-certificates = TLS hacia Discord a través del proxy
RUN apk add --no-cache tzdata ca-certificates

WORKDIR /app
COPY monitor.py .

CMD ["python3", "-u", "monitor.py"]
