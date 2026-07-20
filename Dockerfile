FROM python:3.12-slim

WORKDIR /app

# Системные зависимости: git — для gitpython, gcc/libffi — для cryptography
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    gcc \
    libffi-dev \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Устанавливаем project в режиме editable
COPY manage.py pyproject.toml README.md ./
COPY core/ core/
COPY webui/ webui/
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN pip install --no-cache-dir psycopg2-binary -e .[dev] && \
    chmod +x /docker-entrypoint.sh && \
    python3 manage.py collectstatic --noinput --clear

ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["python3", "manage.py", "runserver", "0.0.0.0:8000"]
