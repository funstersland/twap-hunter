FROM python:3.12-slim-bookworm

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# No system build tools are needed: every dependency (including
# eth-account, cryptography, py-clob-client, cytoolz, ckzg, cffi) ships a
# prebuilt manylinux wheel for cp312. --only-binary=:all: enforces that,
# so a build can never silently fall back to compiling from source.
COPY requirements.txt .
RUN pip install --only-binary=:all: -r requirements.txt

COPY backend ./backend
COPY frontend ./frontend

EXPOSE 8080

CMD ["python", "-u", "-m", "backend"]
