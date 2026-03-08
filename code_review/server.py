"""FastAPI webhook server for GitHub PR events.

Receives webhook events from GitHub when a PR is opened or updated,
runs the multi-agent code review pipeline, and posts the review back.
"""

import asyncio
import hashlib
import hmac
import logging
import os
from datetime import datetime, timezone

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel

from code_review.github_client import fetch_pr_diffs, post_review_comment
from code_review.graph import build_review_graph
from code_review.llm import validate_env
from code_review.state import ReviewState

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

MAX_PAYLOAD_BYTES = 10 * 1024 * 1024  # 10 MB

app = FastAPI(
    title="Agentic Code Review Bot",
    description="Multi-agent AI code review powered by Groq + LangGraph",
    version="1.0.0",
)


@app.on_event("startup")
async def startup_validation() -> None:
    """Validate required configuration at server startup."""
    validate_env()
    logger.info("Server starting — environment validated")


def _verify_webhook_signature(payload: bytes, signature: str | None, secret: str) -> bool:
    """Verify GitHub webhook signature (HMAC-SHA256)."""
    if not signature:
        return False

    expected = "sha256=" + hmac.new(
        secret.encode(),
        payload,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(expected, signature)


@app.get("/health")
async def health():
    """Health check with basic diagnostics."""
    return {
        "status": "healthy",
        "service": "agentic-code-review",
        "version": "1.0.0",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.post("/webhook/github")
async def github_webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(None),
    x_github_event: str | None = Header(None),
):
    """Handle GitHub webhook events for PR reviews."""
    body = await request.body()

    # Enforce payload size limit
    if len(body) > MAX_PAYLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Payload too large")

    # Enforce webhook signature verification
    webhook_secret = os.environ.get("GITHUB_WEBHOOK_SECRET")
    if webhook_secret:
        if not _verify_webhook_signature(body, x_hub_signature_256, webhook_secret):
            raise HTTPException(status_code=401, detail="Invalid webhook signature")
    else:
        logger.warning("GITHUB_WEBHOOK_SECRET not set — webhook signature verification is disabled")

    payload = await request.json()

    # Only process PR events
    if x_github_event != "pull_request":
        return {"status": "ignored", "reason": f"Event type: {x_github_event}"}

    action = payload.get("action")
    if action not in ("opened", "synchronize", "reopened"):
        return {"status": "ignored", "reason": f"PR action: {action}"}

    # Validate required payload fields
    pr = payload.get("pull_request")
    if not pr or not isinstance(pr, dict):
        raise HTTPException(status_code=400, detail="Missing pull_request in payload")

    repo = payload.get("repository")
    if not repo or not isinstance(repo, dict):
        raise HTTPException(status_code=400, detail="Missing repository in payload")

    repo_full_name = repo.get("full_name")
    pr_number = pr.get("number")

    if not repo_full_name or not pr_number:
        raise HTTPException(status_code=400, detail="Missing repository.full_name or pull_request.number")

    logger.info("Reviewing PR #%d on %s: %s", pr_number, repo_full_name, pr.get("title", ""))

    try:
        result = await run_review(repo_full_name, pr_number)
        return {
            "status": "reviewed",
            "pr": pr_number,
            "findings": result["total_findings"],
            "comment_url": result.get("comment_url", ""),
        }
    except Exception:
        logger.exception("Review failed for PR #%d", pr_number)
        raise HTTPException(status_code=500, detail="Review pipeline failed")


async def run_review(repo_full_name: str, pr_number: int) -> dict:
    """Run the full code review pipeline on a PR."""
    logger.info("Fetching PR #%d from %s", pr_number, repo_full_name)
    metadata, file_diffs = await asyncio.to_thread(fetch_pr_diffs, repo_full_name, pr_number)

    if not file_diffs:
        logger.info("No file changes to review")
        return {"total_findings": 0}

    state = ReviewState(
        pr_number=pr_number,
        pr_title=metadata["pr_title"],
        pr_body=metadata["pr_body"],
        repo_full_name=repo_full_name,
        base_branch=metadata["base_branch"],
        file_diffs=file_diffs,
    )

    logger.info("Running review agents on %d files", len(file_diffs))
    graph = build_review_graph()

    # Run blocking graph.stream() in a thread to avoid blocking the event loop
    def _stream_graph():
        final_state = None
        for event in graph.stream(state, stream_mode="updates"):
            for node_name in event:
                logger.info("Agent completed: %s", node_name)
            final_state = event
        return final_state

    final_state = await asyncio.to_thread(_stream_graph)

    # Extract aggregator output
    review_comment = ""
    total_findings = 0
    if final_state:
        for update in final_state.values():
            if "review_comment" in update:
                review_comment = update["review_comment"]
                total_findings = update.get("total_findings", 0)

    # Post review comment to GitHub
    comment_url = ""
    if review_comment:
        logger.info("Posting review comment to GitHub")
        comment_url = await asyncio.to_thread(post_review_comment, repo_full_name, pr_number, review_comment)
        logger.info("Review posted: %s", comment_url)

    return {
        "total_findings": total_findings,
        "comment_url": comment_url,
    }


class ReviewRequest(BaseModel):
    """Request body for manual review trigger."""
    repo: str
    pr: int


@app.post("/review")
async def manual_review(request: ReviewRequest):
    """Manually trigger a review on a specific PR."""
    result = await run_review(request.repo, request.pr)
    return result
