"""LLM configuration and Groq client setup with rate limiting and timeouts."""

import logging
import os
import threading
import time

from dotenv import load_dotenv
from langchain_groq import ChatGroq

load_dotenv()

logger = logging.getLogger(__name__)

# Configurable model names via environment variables
REASONING_MODEL = os.environ.get("LLM_REASONING_MODEL", "llama-3.3-70b-versatile")
FAST_MODEL = os.environ.get("LLM_FAST_MODEL", "llama-3.1-8b-instant")
REQUEST_TIMEOUT = int(os.environ.get("LLM_TIMEOUT_SECONDS", "60"))

# --- Rate limiter (token bucket, thread-safe) ---
_RATE_LIMIT = int(os.environ.get("GROQ_RATE_LIMIT", "25"))  # requests per window
_RATE_WINDOW = 60  # seconds
_request_times: list[float] = []
_rate_lock = threading.Lock()


def _wait_for_rate_limit() -> None:
    """Block until a request slot is available within the rate window."""
    while True:
        with _rate_lock:
            now = time.monotonic()
            # Prune old timestamps outside the window
            _request_times[:] = [t for t in _request_times if now - t < _RATE_WINDOW]
            if len(_request_times) < _RATE_LIMIT:
                _request_times.append(now)
                return
            # Calculate wait time until the oldest request expires
            wait = _RATE_WINDOW - (now - _request_times[0]) + 0.1
        logger.warning("Rate limit reached (%d/%d). Waiting %.1fs...", len(_request_times), _RATE_LIMIT, wait)
        time.sleep(wait)


def validate_env() -> None:
    """Validate required environment variables at startup. Call early to fail fast."""
    missing = []
    if not os.environ.get("GROQ_API_KEY"):
        missing.append("GROQ_API_KEY")
    if missing:
        raise ValueError(
            f"Missing required environment variable(s): {', '.join(missing)}. "
            "See .env.example for setup instructions."
        )


def get_llm(model: str = REASONING_MODEL, temperature: float = 0.7) -> ChatGroq:
    """Get a configured Groq LLM instance with timeout and rate limiting.

    Rate limiting is enforced per-call to stay within Groq free tier limits.
    """
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise ValueError(
            "GROQ_API_KEY not set. Get your free key at https://console.groq.com "
            "and add it to .env file."
        )

    _wait_for_rate_limit()

    return ChatGroq(
        model=model,
        temperature=temperature,
        api_key=api_key,
        request_timeout=REQUEST_TIMEOUT,
    )


def get_fast_llm() -> ChatGroq:
    """Lighter model for routing/classification — higher rate limits."""
    return get_llm(model=FAST_MODEL, temperature=0.0)


def get_reasoning_llm() -> ChatGroq:
    """Powerful model for complex reasoning tasks."""
    return get_llm(model=REASONING_MODEL, temperature=0.7)
