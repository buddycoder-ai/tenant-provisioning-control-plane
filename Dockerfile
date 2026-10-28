FROM python:3.12-slim
WORKDIR /service
COPY pyproject.toml ./
RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir "fastapi>=0.115,<0.116" "uvicorn[standard]>=0.30,<0.31" \
    "sqlalchemy>=2.0,<2.1" "psycopg[binary]>=3.2,<3.3" "pika>=1.3,<1.4" \
    "pydantic>=2.9,<2.10" "alembic>=1.13,<1.14" "python-json-logger>=2.0,<3.0"
COPY app ./app
COPY tests ./tests
COPY alembic.ini ./
COPY migrations ./migrations
EXPOSE 8000
