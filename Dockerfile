FROM python:3.12-slim-bookworm

WORKDIR /app

# PYTHONPATH=/app makes `import backend` / `python -m backend` resolve no
# matter what working directory Railway runs the container from — the
# cause of the "No module named backend" crash-loop (Railway does not
# guarantee cwd == WORKDIR at runtime).
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Dependencies first so this layer stays cached unless requirements.txt
# changes. No system build tools needed: every dep ships a manylinux
# cp312 wheel, enforced by --only-binary=:all:.
COPY requirements.txt ./
RUN pip install --only-binary=:all: -r requirements.txt

# Whole context in one layer (.dockerignore keeps it tiny and excludes
# .env/.venv/data/.git). Single COPY also avoids the corrupt per-dir
# BuildKit cache refs a canceled build can leave behind.
COPY . ./

# Fail the BUILD with a clear error if the backend package didn't make it
# into the image, instead of crash-looping at runtime.
RUN python -c "import backend; print('backend package present')"

EXPOSE 8080

CMD ["python", "-u", "-m", "backend"]
