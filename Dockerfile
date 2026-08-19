FROM python:3.12-slim-bookworm

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Dependencies first so this layer stays cached unless requirements.txt
# changes. No system build tools are needed: every dependency ships a
# prebuilt manylinux cp312 wheel, enforced by --only-binary=:all:.
COPY requirements.txt ./
RUN pip install --only-binary=:all: -r requirements.txt

# Copy the whole (.dockerignore-trimmed) context in ONE layer. A single
# COPY — rather than separate `COPY backend` + `COPY frontend` steps —
# gives a fresh layer graph, sidestepping a corrupt BuildKit cache ref
# left by an earlier canceled build ("failed to compute cache key ...
# /frontend: not found").
COPY . ./

EXPOSE 8080

CMD ["python", "-u", "-m", "backend"]
