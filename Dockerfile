FROM python:3.11-slim

# LightGBM's compiled extension links against libgomp (OpenMP) at import time, which
# python:3.11-slim doesn't ship -- fails as `OSError: libgomp.so.1: cannot open shared
# object file` the moment anything imports policy/hazard.py, not at pip-install time.
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
# Just results.json, not the rest of benchmarks/ (report.md, PNGs, ~25 per-arm audit
# SQLite DBs) -- dashboard.py's _load_benchmark_results() reads only this file, and the
# audit DBs alone are tens of MB that would bloat the image for a "60 seconds" demo goal.
COPY benchmarks/results.json ./benchmarks/results.json

RUN pip install --no-cache-dir -e .

EXPOSE 8000
CMD ["uvicorn", "vasool.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
