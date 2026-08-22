FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src

RUN pip install --no-cache-dir -e .

# Placeholder until the API app exists (Phase 8) and `make up` is real.
CMD ["python", "-c", "print('vasool: not implemented yet')"]
