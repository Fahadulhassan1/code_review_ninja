"""Specialist review agents for code review.

Each agent is a domain expert that analyzes code diffs for specific concerns.
They run sequentially and their findings are aggregated into a single review.
"""

import logging
import re
import time

from groq import RateLimitError
from langchain_core.messages import HumanMessage, SystemMessage
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from code_review.llm import get_reasoning_llm
from code_review.state import (
    FileDiff,
    ReviewCategory,
    ReviewFinding,
    ReviewState,
    Severity,
)

logger = logging.getLogger(__name__)

# Module-level constants
_SEVERITY_MAP = {
    "critical": Severity.CRITICAL,
    "high": Severity.HIGH,
    "medium": Severity.MEDIUM,
    "low": Severity.LOW,
    "info": Severity.INFO,
}
_VALID_SEVERITIES = set(_SEVERITY_MAP.keys())


def _format_diffs_for_review(file_diffs: list[FileDiff]) -> str:
    """Format file diffs into a readable string for LLM consumption."""
    parts = []
    for diff in file_diffs:
        if not diff.patch:
            continue
        parts.append(
            f"=== File: {diff.filename} ({diff.status}) "
            f"[+{diff.additions}/-{diff.deletions}] ===\n"
            f"Language: {diff.language or 'unknown'}\n"
            f"```\n{diff.patch}\n```\n"
        )
    return "\n".join(parts) if parts else "No code changes to review."


def _parse_findings(
    raw_response: str,
    category: ReviewCategory,
    file_diffs: list[FileDiff],
) -> list[ReviewFinding]:
    """Parse LLM response into structured ReviewFinding objects.

    Expected format from LLM:
    FINDING:
    FILE: path/to/file.go
    LINES: 10-15
    SEVERITY: high
    TITLE: SQL injection vulnerability
    DESCRIPTION: The query uses string concatenation...
    SUGGESTION: Use parameterized queries...
    """
    findings = []
    current: dict = {}
    valid_files = {d.filename for d in file_diffs}

    for line in raw_response.split("\n"):
        line = line.strip()

        if line.startswith("FINDING:"):
            if current.get("title"):
                finding = _build_finding(current, category, valid_files)
                if finding:
                    findings.append(finding)
            current = {}
        elif line.startswith("FILE:"):
            current["file_path"] = line[5:].strip()
        elif line.startswith("LINES:"):
            current["line_range"] = line[6:].strip()
        elif line.startswith("SEVERITY:"):
            severity = line[9:].strip().lower()
            if severity in _VALID_SEVERITIES:
                current["severity"] = severity
            else:
                logger.warning("Invalid severity '%s', defaulting to medium", severity)
                current["severity"] = "medium"
        elif line.startswith("TITLE:"):
            current["title"] = line[6:].strip()
        elif line.startswith("DESCRIPTION:"):
            current["description"] = line[12:].strip()
        elif line.startswith("SUGGESTION:"):
            current["suggestion"] = line[11:].strip()

    # Don't forget the last finding
    if current.get("title"):
        finding = _build_finding(current, category, valid_files)
        if finding:
            findings.append(finding)

    return findings


def _build_finding(
    data: dict,
    category: ReviewCategory,
    valid_files: set[str],
) -> ReviewFinding | None:
    """Build a ReviewFinding from parsed data with validation."""
    title = data.get("title", "").strip()
    if not title:
        logger.warning("Skipping finding with empty title")
        return None

    file_path = data.get("file_path", "unknown")
    # Validate file path exists in the diff
    if file_path not in valid_files and valid_files:
        # Try suffix match — but only accept unambiguous matches
        suffix_matches = [vf for vf in valid_files if vf.endswith("/" + file_path) or file_path.endswith(vf)]
        if len(suffix_matches) == 1:
            file_path = suffix_matches[0]
        elif suffix_matches:
            logger.warning("Ambiguous file match for '%s': %s", file_path, suffix_matches)

    return ReviewFinding(
        category=category,
        severity=_SEVERITY_MAP.get(data.get("severity", "medium"), Severity.MEDIUM),
        file_path=file_path,
        line_range=data.get("line_range", ""),
        title=title,
        description=data.get("description", ""),
        suggestion=data.get("suggestion", ""),
    )


_REVIEW_OUTPUT_FORMAT = """
For EACH issue found, use EXACTLY this format:

FINDING:
FILE: <exact file path from the diff>
LINES: <line range, e.g. 10-15>
SEVERITY: <critical|high|medium|low|info>
TITLE: <short one-line summary>
DESCRIPTION: <detailed explanation>
SUGGESTION: <specific fix or code example>

If you find NO issues in your domain, respond with:
NO_ISSUES_FOUND

Be specific. Reference exact code from the diff. Do not fabricate issues that aren't there.
Only report issues in the changed code (lines starting with + in the diff).
"""


class DailyQuotaExceeded(Exception):
    """Raised when Groq daily token quota is exhausted."""
    def __init__(self, wait_message: str):
        self.wait_message = wait_message
        super().__init__(f"Groq daily token limit reached. {wait_message}")


def _is_daily_limit(exc: BaseException) -> bool:
    """Check if a RateLimitError is a daily token limit (not retryable)."""
    msg = str(exc)
    # Daily limits mention "tokens per day" or have long wait times (minutes/hours)
    if "tokens per day" in msg.lower() or "tpd" in msg.lower():
        return True
    # If wait time is > 60 seconds, treat as daily limit
    match = re.search(r"try again in (\d+)m", msg)
    if match and int(match.group(1)) > 1:
        return True
    return False


def _should_retry(exc: BaseException) -> bool:
    """Return True for transient errors worth retrying."""
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return True
    if isinstance(exc, RateLimitError) and not _is_daily_limit(exc):
        return True
    return False


@retry(
    wait=wait_exponential(multiplier=2, min=3, max=30),
    stop=stop_after_attempt(3),
    retry=_should_retry,
    before_sleep=lambda retry_state: logger.warning(
        "LLM call failed (attempt %d), retrying: %s",
        retry_state.attempt_number,
        retry_state.outcome.exception(),
    ),
)
def _invoke_llm_with_retry(llm, system_prompt: str, user_prompt: str):
    """Invoke LLM with automatic retry on transient failures.

    Retries on: connection errors, timeouts, per-minute rate limits.
    Fails immediately on: daily token quota exhaustion.
    """
    try:
        return llm.invoke([
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_prompt),
        ])
    except RateLimitError as e:
        if _is_daily_limit(e):
            wait_match = re.search(r"try again in ([^.]+)", str(e), re.IGNORECASE)
            wait_msg = f"Try again in {wait_match.group(1)}." if wait_match else "Try again later."
            raise DailyQuotaExceeded(wait_msg) from e
        raise  # Per-minute limit — let tenacity retry


def security_agent(state: ReviewState) -> dict:
    """Security specialist — finds vulnerabilities and security anti-patterns."""
    start = time.monotonic()
    logger.info("Security agent starting on %d files", len(state.file_diffs))

    llm = get_reasoning_llm()
    diffs = _format_diffs_for_review(state.file_diffs)

    system_prompt = f"""You are a senior application security engineer conducting a security-focused code review.
You specialize in finding vulnerabilities in backend code (especially Go, Python, TypeScript).

Focus on these categories (prioritized):
1. **Injection**: SQL injection, command injection, XSS, template injection
2. **Authentication/Authorization**: Missing auth checks, broken access control, hardcoded secrets
3. **Cryptographic failures**: Weak hashing, plaintext secrets, insecure random
4. **SSRF/Path traversal**: Unvalidated URLs, file paths from user input
5. **Unsafe deserialization**: Untrusted data deserialization
6. **Error handling**: Information leakage through error messages
7. **Dependencies**: Known vulnerable patterns

IMPORTANT:
- Only flag REAL security issues, not theoretical ones
- For Go code: check for sql.Query with string concat, os/exec usage, missing input validation
- For Python: check for eval(), subprocess with shell=True, SQL string formatting
- Severity guide: critical = exploitable RCE/SQLi, high = auth bypass/data leak, medium = missing validation, low = best practice

{_REVIEW_OUTPUT_FORMAT}"""

    response = _invoke_llm_with_retry(llm, system_prompt, f"Review these code changes for security issues:\n\n{diffs}")
    findings = _parse_findings(response.content, ReviewCategory.SECURITY, state.file_diffs)

    elapsed = time.monotonic() - start
    logger.info("Security agent completed in %.1fs with %d findings", elapsed, len(findings))
    return {"security_findings": findings}


def performance_agent(state: ReviewState) -> dict:
    """Performance specialist — identifies inefficiencies and resource issues."""
    start = time.monotonic()
    logger.info("Performance agent starting on %d files", len(state.file_diffs))

    llm = get_reasoning_llm()
    diffs = _format_diffs_for_review(state.file_diffs)

    system_prompt = f"""You are a senior performance engineer conducting a performance-focused code review.
You specialize in backend systems performance (especially Go, Python, TypeScript).

Focus on these categories (prioritized):
1. **Database**: N+1 queries, missing indexes hints, unbounded queries, connection leaks
2. **Memory**: Unbounded allocations, large copies, buffer reuse opportunities
3. **Concurrency**: Goroutine/thread leaks, missing timeouts, deadlock risks, race conditions
4. **Algorithmic**: O(n²) where O(n) is possible, unnecessary iterations, redundant computations
5. **I/O**: Missing connection pooling, unbuffered I/O, synchronous blocking
6. **Caching**: Missing cache opportunities for repeated expensive operations

IMPORTANT:
- Only flag issues that would meaningfully impact performance at scale
- For Go: check for defer in loops, missing context timeouts, slice pre-allocation
- For Python: check for list comprehension vs generator, global imports in hot paths
- Include Big-O analysis where relevant

{_REVIEW_OUTPUT_FORMAT}"""

    response = _invoke_llm_with_retry(llm, system_prompt, f"Review these code changes for performance issues:\n\n{diffs}")
    findings = _parse_findings(response.content, ReviewCategory.PERFORMANCE, state.file_diffs)

    elapsed = time.monotonic() - start
    logger.info("Performance agent completed in %.1fs with %d findings", elapsed, len(findings))
    return {"performance_findings": findings}


def style_agent(state: ReviewState) -> dict:
    """Style specialist — checks for idiomatic code and best practices."""
    start = time.monotonic()
    logger.info("Style agent starting on %d files", len(state.file_diffs))

    llm = get_reasoning_llm()
    diffs = _format_diffs_for_review(state.file_diffs)

    system_prompt = f"""You are a senior software engineer conducting a code style and best practices review.
You are an expert in idiomatic code patterns for Go, Python, and TypeScript.

Focus on these categories (prioritized):
1. **Error handling**: Proper error wrapping (Go: fmt.Errorf with %w), not swallowing errors
2. **Naming**: Clear, consistent names following language conventions (Go: camelCase, Python: snake_case)
3. **Code organization**: Single responsibility, function length, package structure
4. **Idioms**: Language-specific best practices (Go: accept interfaces return structs, Python: EAFP)
5. **API design**: Clean interfaces, proper HTTP status codes, consistent response formats
6. **Testing**: Missing edge cases, test organization

IMPORTANT:
- Be pragmatic, not pedantic — only flag things that meaningfully improve readability/maintainability
- For Go: check effective Go patterns, error wrapping, interface usage, struct organization
- For Python: PEP 8 violations that tools miss, pythonic patterns, type hints
- MAX 5 style findings (focus on the most impactful)

{_REVIEW_OUTPUT_FORMAT}"""

    response = _invoke_llm_with_retry(llm, system_prompt, f"Review these code changes for style and best practices:\n\n{diffs}")
    findings = _parse_findings(response.content, ReviewCategory.STYLE, state.file_diffs)

    elapsed = time.monotonic() - start
    logger.info("Style agent completed in %.1fs with %d findings", elapsed, len(findings))
    return {"style_findings": findings}


def docs_agent(state: ReviewState) -> dict:
    """Documentation specialist — identifies missing or outdated documentation."""
    start = time.monotonic()
    logger.info("Docs agent starting on %d files", len(state.file_diffs))

    llm = get_reasoning_llm()
    diffs = _format_diffs_for_review(state.file_diffs)

    system_prompt = f"""You are a senior technical writer reviewing code changes for documentation completeness.
You focus on ensuring code is well-documented for team collaboration.

Focus on these categories (prioritized):
1. **Exported interfaces**: Public functions/methods/types missing documentation
2. **Complex logic**: Non-obvious algorithms or business logic without explanatory comments
3. **API contracts**: Missing or outdated API documentation (endpoints, request/response)
4. **Configuration**: New config options without documentation
5. **Breaking changes**: Changes that affect other components without migration notes

IMPORTANT:
- For Go: all exported symbols (capital letter) should have godoc comments
- For Python: public functions should have docstrings for complex logic
- Don't flag trivial getters/setters — focus on complex or important interfaces
- MAX 4 documentation findings

{_REVIEW_OUTPUT_FORMAT}"""

    response = _invoke_llm_with_retry(llm, system_prompt, f"Review these code changes for documentation completeness:\n\n{diffs}")
    findings = _parse_findings(response.content, ReviewCategory.DOCUMENTATION, state.file_diffs)

    elapsed = time.monotonic() - start
    logger.info("Docs agent completed in %.1fs with %d findings", elapsed, len(findings))
    return {"docs_findings": findings}


def aggregator_agent(state: ReviewState) -> dict:
    """Aggregator — combines all specialist findings into a formatted PR review comment."""
    all_findings = (
        state.security_findings
        + state.performance_findings
        + state.style_findings
        + state.docs_findings
    )

    total = len(all_findings)
    has_critical = any(f.severity == Severity.CRITICAL for f in all_findings)

    if total == 0:
        review_comment = _format_clean_review(state)
        return {
            "summary": "No issues found. Code looks good!",
            "review_comment": review_comment,
            "total_findings": 0,
            "has_critical": False,
        }

    # Count by severity
    severity_counts = {}
    for f in all_findings:
        severity_counts[f.severity] = severity_counts.get(f.severity, 0) + 1

    # Count by category
    category_counts = {}
    for f in all_findings:
        category_counts[f.category] = category_counts.get(f.category, 0) + 1

    # Build summary
    severity_str = ", ".join(
        f"{count} {sev.value}" for sev, count in sorted(severity_counts.items(), key=lambda x: list(Severity).index(x[0]))
    )
    summary = f"Found {total} issues ({severity_str})"

    # Build formatted comment
    review_comment = _format_review_comment(state, all_findings, summary)

    return {
        "summary": summary,
        "review_comment": review_comment,
        "total_findings": total,
        "has_critical": has_critical,
    }


def _format_clean_review(state: ReviewState) -> str:
    """Format a review comment when no issues are found."""
    return f"""## 🤖 AI Code Review — PR #{state.pr_number}

✅ **No issues found!** The code changes look good.

<details>
<summary>Review Details</summary>

- **Files reviewed**: {len(state.file_diffs)}
- **Agents**: Security, Performance, Style, Documentation
- **Model**: Llama 3.3 70B via Groq

</details>

---
*Powered by [Agentic Code Review Bot](https://github.com/Fahadulhassan1/code_review_ninja) — Multi-agent review with LangGraph + Groq*
"""


SEVERITY_EMOJI = {
    Severity.CRITICAL: "🔴",
    Severity.HIGH: "🟠",
    Severity.MEDIUM: "🟡",
    Severity.LOW: "🔵",
    Severity.INFO: "ℹ️",
}

CATEGORY_EMOJI = {
    ReviewCategory.SECURITY: "🔒",
    ReviewCategory.PERFORMANCE: "⚡",
    ReviewCategory.STYLE: "🎨",
    ReviewCategory.DOCUMENTATION: "📝",
}


def _format_review_comment(
    state: ReviewState,
    findings: list[ReviewFinding],
    summary: str,
) -> str:
    """Format findings into a structured GitHub PR comment."""
    # Sort: critical first, then high, etc.
    sorted_findings = sorted(findings, key=lambda f: list(Severity).index(f.severity))

    # Header
    has_critical = any(f.severity == Severity.CRITICAL for f in findings)
    status = "🚨 **Action Required**" if has_critical else "⚠️ **Review Findings**"

    lines = [
        f"## 🤖 AI Code Review — PR #{state.pr_number}",
        "",
        f"{status} — {summary}",
        "",
    ]

    # Group by category
    for category in ReviewCategory:
        cat_findings = [f for f in sorted_findings if f.category == category]
        if not cat_findings:
            continue

        emoji = CATEGORY_EMOJI[category]
        lines.append(f"### {emoji} {category.value.title()} ({len(cat_findings)} findings)")
        lines.append("")

        for f in cat_findings:
            sev_emoji = SEVERITY_EMOJI[f.severity]
            location = f"`{f.file_path}`"
            if f.line_range:
                location += f" (L{f.line_range})"

            lines.append(f"#### {sev_emoji} [{f.severity.value.upper()}] {f.title}")
            lines.append(f"📍 {location}")
            lines.append("")
            if f.description:
                lines.append(f"{f.description}")
                lines.append("")
            if f.suggestion:
                lines.append(f"💡 **Suggestion:** {f.suggestion}")
                lines.append("")
            lines.append("---")
            lines.append("")

    # Footer
    lines.extend([
        "<details>",
        "<summary>Review Details</summary>",
        "",
        f"- **Files reviewed**: {len(state.file_diffs)}",
        f"- **Total findings**: {len(findings)}",
        "- **Agents**: Security, Performance, Style, Documentation",
        "- **Model**: Llama 3.3 70B via Groq",
        "",
        "</details>",
        "",
        "---",
        "*Powered by [Agentic Code Review Bot](https://github.com/Fahadulhassan1/code_review_ninja) — Multi-agent review with LangGraph + Groq*",
    ])

    return "\n".join(lines)
