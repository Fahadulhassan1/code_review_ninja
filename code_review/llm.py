"""LLM configuration with multi-provider support, rate limiting, and timeouts.

Supported providers (set via LLM_PROVIDER env var):
    groq      — Groq Cloud (free tier, default)
    openai    — OpenAI (GPT-4o, etc.)
    anthropic — Anthropic (Claude)
    ollama    — Local Ollama server (free, unlimited)
    gemini    — Google Gemini (free tier available)
"""

import logging
import os
import threading
import time

from dotenv import load_dotenv
from langchain_core.language_models.chat_models import BaseChatModel

load_dotenv()

logger = logging.getLogger(__name__)

# --- Provider configuration ---
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "groq").lower()

# Default models per provider
_DEFAULT_MODELS = {
    "groq": "llama-3.3-70b-versatile",
    "openai": "gpt-4o",
    "anthropic": "claude-sonnet-4-20250514",
    "ollama": "llama3.1",
    "gemini": "gemini-2.0-flash",
}

# Required API key env var per provider (None = no key needed)
_REQUIRED_KEY = {
    "groq": "GROQ_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "ollama": None,
    "gemini": "GOOGLE_API_KEY",
}

REASONING_MODEL = os.environ.get("LLM_REASONING_MODEL", _DEFAULT_MODELS.get(LLM_PROVIDER, ""))
FAST_MODEL = os.environ.get("LLM_FAST_MODEL", REASONING_MODEL)
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
            _request_times[:] = [t for t in _request_times if now - t < _RATE_WINDOW]
            if len(_request_times) < _RATE_LIMIT:
                _request_times.append(now)
                return
            wait = _RATE_WINDOW - (now - _request_times[0]) + 0.1
        logger.warning("Rate limit reached (%d/%d). Waiting %.1fs...", len(_request_times), _RATE_LIMIT, wait)
        time.sleep(wait)


def validate_env() -> None:
    """Validate required environment variables at startup. Call early to fail fast."""
    if LLM_PROVIDER not in _REQUIRED_KEY:
        raise ValueError(
            f"Unknown LLM_PROVIDER='{LLM_PROVIDER}'. "
            f"Supported: {', '.join(_REQUIRED_KEY.keys())}"
        )
    key_var = _REQUIRED_KEY[LLM_PROVIDER]
    if key_var and not os.environ.get(key_var):
        raise ValueError(
            f"Missing {key_var} for provider '{LLM_PROVIDER}'. "
            "See .env.example for setup instructions."
        )


def _create_groq_llm(model: str, temperature: float) -> BaseChatModel:
    from langchain_groq import ChatGroq
    _wait_for_rate_limit()
    return ChatGroq(
        model=model,
        temperature=temperature,
        api_key=os.environ.get("GROQ_API_KEY"),
        request_timeout=REQUEST_TIMEOUT,
    )


def _create_openai_llm(model: str, temperature: float) -> BaseChatModel:
    from langchain_openai import ChatOpenAI
    return ChatOpenAI(
        model=model,
        temperature=temperature,
        api_key=os.environ.get("OPENAI_API_KEY"),
        request_timeout=REQUEST_TIMEOUT,
    )


def _create_anthropic_llm(model: str, temperature: float) -> BaseChatModel:
    from langchain_anthropic import ChatAnthropic
    return ChatAnthropic(
        model=model,
        temperature=temperature,
        api_key=os.environ.get("ANTHROPIC_API_KEY"),
        timeout=REQUEST_TIMEOUT,
    )


def _create_ollama_llm(model: str, temperature: float) -> BaseChatModel:
    from langchain_ollama import ChatOllama
    base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
    return ChatOllama(
        model=model,
        temperature=temperature,
        base_url=base_url,
    )


def _create_gemini_llm(model: str, temperature: float) -> BaseChatModel:
    from langchain_google_genai import ChatGoogleGenerativeAI
    return ChatGoogleGenerativeAI(
        model=model,
        temperature=temperature,
        google_api_key=os.environ.get("GOOGLE_API_KEY"),
        request_timeout=REQUEST_TIMEOUT,
    )


_PROVIDER_FACTORY = {
    "groq": _create_groq_llm,
    "openai": _create_openai_llm,
    "anthropic": _create_anthropic_llm,
    "ollama": _create_ollama_llm,
    "gemini": _create_gemini_llm,
}


def get_llm(model: str = "", temperature: float = 0.7) -> BaseChatModel:
    """Get a configured LLM instance for the active provider."""
    model = model or REASONING_MODEL
    factory = _PROVIDER_FACTORY.get(LLM_PROVIDER)
    if not factory:
        raise ValueError(f"Unknown LLM_PROVIDER='{LLM_PROVIDER}'")
    logger.debug("Creating %s LLM: model=%s temp=%.1f", LLM_PROVIDER, model, temperature)
    return factory(model, temperature)


def get_fast_llm() -> BaseChatModel:
    """Lighter model for routing/classification."""
    return get_llm(model=FAST_MODEL, temperature=0.0)


def get_reasoning_llm() -> BaseChatModel:
    """Powerful model for complex reasoning tasks."""
    return get_llm(model=REASONING_MODEL, temperature=0.7)
