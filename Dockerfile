FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CHUNKWISE_HOST=0.0.0.0 \
    PORT=8080 \
    CHUNKWISE_HOME=/data/chunkwise

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . \
    && mkdir -p /data/chunkwise \
    && useradd --system --uid 10001 --create-home chunkwise \
    && chown -R chunkwise:chunkwise /data/chunkwise

USER 10001:10001
VOLUME ["/data/chunkwise"]
EXPOSE 8080
CMD ["chunkwise-web"]