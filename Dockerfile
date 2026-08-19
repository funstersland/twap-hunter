FROM python:3.12-slim-bookworm

WORKDIR /app

# PYTHONPATH=/app makes `python -m backend` resolve regardless of the cwd
# Railway starts the container from.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Dependencies first (cached unless requirements.txt changes). All deps
# ship manylinux cp312 wheels, so no C toolchain is needed.
COPY requirements.txt ./
RUN pip install --only-binary=:all: -r requirements.txt

# Copy the app packages EXPLICITLY by name. A whole-context `COPY . ./`
# did not reliably materialize these directories on Railway's builder;
# naming them directly is unambiguous (and fails the build loudly if a
# directory is somehow absent from the context).
COPY backend /app/backend
COPY frontend /app/frontend

# Prove the package is importable in the image — a clear BUILD failure
# beats a runtime "No module named backend" crash-loop.
RUN ls -la /app && python -c "import backend; print('backend package present')"

EXPOSE 8080

CMD ["python", "-u", "-m", "backend"]
