# Single-stage image — demo scale, no orchestration.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Build the deterministic synthetic dataset at image-build time so the container
# starts ready. external.db is read-only at runtime; system.db is created on
# first boot by the app's lifespan hook.
RUN python -m src.synthetic.build

EXPOSE 8000
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
