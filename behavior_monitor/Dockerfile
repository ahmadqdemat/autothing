FROM python:3.12-alpine

RUN apk add --no-cache tzdata && pip install --no-cache-dir "aiohttp>=3.9"

WORKDIR /app
COPY app/engine.py app/main.py /app/
COPY run.sh /run.sh
RUN chmod +x /run.sh

CMD ["/run.sh"]
