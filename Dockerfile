FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN pip install --no-cache-dir -e ".[postgres]"

COPY configs/ ./configs/
COPY sample_data/ ./sample_data/

ENTRYPOINT ["loader"]
CMD ["--help"]
