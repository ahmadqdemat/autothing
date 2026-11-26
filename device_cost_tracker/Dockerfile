FROM python:3.11-alpine
RUN apk add --no-cache tzdata dos2unix
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ ./app/
COPY frontend/ ./frontend/
COPY run.sh /run.sh
RUN dos2unix /run.sh && chmod +x /run.sh
CMD ["/run.sh"]
