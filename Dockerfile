FROM python:3.12-slim

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy project files and install dependencies
COPY pyproject.toml uv.lock* ./
RUN uv sync --no-dev --no-install-project

COPY code_review/ code_review/

EXPOSE 8000

# Run the webhook server
CMD ["uv", "run", "uvicorn", "code_review.server:app", "--host", "0.0.0.0", "--port", "8000"]
