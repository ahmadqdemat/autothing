FROM python:3.11-slim-bookworm

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app /app/app
COPY run.sh /run.sh
RUN chmod +x /run.sh

EXPOSE 8000

ENTRYPOINT ["/run.sh"]
