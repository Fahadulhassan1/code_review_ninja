"""GitHub API integration for fetching PR data and posting review comments."""

import logging
import os
import re
from pathlib import PurePosixPath

from github import Auth, Github, GithubException

from code_review.llm import LLM_PROVIDER, REASONING_MODEL
from code_review.state import FileDiff, ReviewFinding, ReviewCategory, Severity

logger = logging.getLogger(__name__)

# Language detection by file extension
EXTENSION_TO_LANGUAGE = {
    ".go": "go",
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".rs": "rust",
    ".java": "java",
    ".rb": "ruby",
    ".php": "php",
    ".c": "c",
    ".cpp": "cpp",
    ".h": "c",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".swift": "swift",
    ".kt": "kotlin",
    ".sql": "sql",
    ".sh": "shell",
    ".bash": "shell",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
    ".toml": "toml",
    ".dockerfile": "dockerfile",
    ".tf": "terraform",
}

# Special filenames without extensions
FILENAME_TO_LANGUAGE = {
    "dockerfile": "dockerfile",
    "makefile": "makefile",
    "gemfile": "ruby",
    "rakefile": "ruby",
}

GITHUB_TIMEOUT = int(os.environ.get("GITHUB_TIMEOUT_SECONDS", "30"))


def _detect_language(filename: str) -> str:
    """Detect programming language from file extension or name."""
    basename = PurePosixPath(filename).name.lower()
    if basename in FILENAME_TO_LANGUAGE:
        return FILENAME_TO_LANGUAGE[basename]
    suffix = PurePosixPath(filename).suffix.lower()
    return EXTENSION_TO_LANGUAGE.get(suffix, "")


def _get_github_client() -> Github:
    """Create an authenticated GitHub client with timeout."""
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise ValueError(
            "GITHUB_TOKEN not set. Create a personal access token at "
            "https://github.com/settings/tokens with 'repo' scope."
        )
    return Github(auth=Auth.Token(token), timeout=GITHUB_TIMEOUT, retry=0)


def fetch_pr_diffs(repo_full_name: str, pr_number: int) -> tuple[dict, list[FileDiff]]:
    """Fetch PR metadata and file diffs from GitHub.

    Returns:
        Tuple of (pr_metadata dict, list of FileDiff objects)
    """
    gh = _get_github_client()
    repo = gh.get_repo(repo_full_name)
    pr = repo.get_pull(pr_number)

    # PR metadata
    metadata = {
        "pr_title": pr.title,
        "pr_body": pr.body or "",
        "base_branch": pr.base.ref,
    }

    # File diffs
    file_diffs = []
    for f in pr.get_files():
        diff = FileDiff(
            filename=f.filename,
            status=f.status,
            patch=f.patch or "",
            additions=f.additions,
            deletions=f.deletions,
            language=_detect_language(f.filename),
        )
        file_diffs.append(diff)

    gh.close()
    return metadata, file_diffs


def post_review_comment(repo_full_name: str, pr_number: int, comment: str) -> str:
    """Post a review comment on a GitHub PR.

    Returns:
        URL of the posted comment.
    """
    gh = _get_github_client()
    repo = gh.get_repo(repo_full_name)
    pr = repo.get_pull(pr_number)

    review_comment = pr.create_issue_comment(comment)

    gh.close()
    return review_comment.html_url


# --- Inline review support ---

_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")

_INLINE_SEVERITY_EMOJI = {
    Severity.CRITICAL: "🔴",
    Severity.HIGH: "🟠",
    Severity.MEDIUM: "🟡",
    Severity.LOW: "🔵",
    Severity.INFO: "ℹ️",
}

_INLINE_CATEGORY_EMOJI = {
    ReviewCategory.SECURITY: "🔒",
    ReviewCategory.PERFORMANCE: "⚡",
    ReviewCategory.STYLE: "🎨",
    ReviewCategory.DOCUMENTATION: "📝",
}


def _parse_commentable_lines(patch: str) -> set[int]:
    """Extract new-file line numbers that can receive inline review comments."""
    lines = set()
    current = 0
    for raw in patch.split("\n"):
        m = _HUNK_HEADER.match(raw)
        if m:
            current = int(m.group(1))
            continue
        if raw.startswith("-"):
            continue
        if raw.startswith("+") or raw.startswith(" "):
            lines.add(current)
            current += 1
    return lines


def _find_comment_line(line_range: str, commentable: set[int]) -> int | None:
    """Map a finding's line range to a commentable line in the diff."""
    if not line_range or not commentable:
        return None
    parts = line_range.replace(" ", "").split("-")
    try:
        start = int(parts[0])
        end = int(parts[-1]) if len(parts) > 1 else start
    except (ValueError, IndexError):
        return None
    # Exact match in range
    for line in range(start, end + 1):
        if line in commentable:
            return line
    # Nearby (±3 lines)
    for offset in range(1, 4):
        if start - offset in commentable:
            return start - offset
        if end + offset in commentable:
            return end + offset
    return None


def _format_inline_comment(finding: ReviewFinding) -> str:
    """Format a single finding as a GitHub inline review comment."""
    sev_emoji = _INLINE_SEVERITY_EMOJI.get(finding.severity, "⚠️")
    cat_emoji = _INLINE_CATEGORY_EMOJI.get(finding.category, "")
    parts = [f"{cat_emoji} {sev_emoji} **[{finding.severity.value.upper()}] {finding.title}**", ""]
    if finding.description:
        parts.append(finding.description)
        parts.append("")
    if finding.suggestion:
        parts.append(f"💡 **Suggestion:** {finding.suggestion}")
    return "\n".join(parts).strip()


def post_inline_review(
    repo_full_name: str,
    pr_number: int,
    findings: list[ReviewFinding],
    file_diffs: list[FileDiff],
    review_comment: str,
) -> str:
    """Post a PR review with inline comments on specific diff lines.

    Findings that map to diff lines become inline comments.
    Unmapped findings are appended to the review summary.
    Falls back to an issue comment if no inline placement is possible.

    Returns the PR HTML URL.
    """
    gh = _get_github_client()
    repo = gh.get_repo(repo_full_name)
    pr = repo.get_pull(pr_number)

    if not findings:
        comment = pr.create_issue_comment(review_comment)
        gh.close()
        return comment.html_url

    # Build commentable lines per file
    commentable: dict[str, set[int]] = {}
    for fd in file_diffs:
        if fd.patch:
            commentable[fd.filename] = _parse_commentable_lines(fd.patch)

    inline_comments = []
    unplaced = []

    for finding in findings:
        file_lines = commentable.get(finding.file_path, set())
        target = _find_comment_line(finding.line_range, file_lines)
        if target:
            inline_comments.append({
                "path": finding.file_path,
                "line": target,
                "body": _format_inline_comment(finding),
            })
        else:
            unplaced.append(finding)

    if not inline_comments:
        comment = pr.create_issue_comment(review_comment)
        gh.close()
        return comment.html_url

    # Review body is intentionally minimal — findings live as inline comments.
    review_body = ""

    try:
        pr.create_review(
            body=review_body,
            event="COMMENT",
            comments=inline_comments,
        )
    except GithubException as e:
        # 403 = can't review own PR or insufficient permissions — fall back to issue comment
        logger.warning("Inline review failed (status=%s), falling back to issue comment", e.status)
        fallback_lines = ["## 🤖 Code Review Ninja — Inline Findings", ""]
        for ic in inline_comments:
            fallback_lines.append(f"**`{ic['path']}` L{ic['line']}**")
            fallback_lines.append(ic["body"])
            fallback_lines.append("")
        try:
            pr.create_issue_comment("\n".join(fallback_lines))
        except GithubException:
            logger.error("Could not post fallback comment either — check GITHUB_TOKEN permissions")
    except Exception as e:
        logger.warning("Inline review failed (%s), falling back to issue comment", e)
        fallback_lines = ["## 🤖 Code Review Ninja — Inline Findings", ""]
        for ic in inline_comments:
            fallback_lines.append(f"**`{ic['path']}` L{ic['line']}**")
            fallback_lines.append(ic["body"])
            fallback_lines.append("")
        try:
            pr.create_issue_comment("\n".join(fallback_lines))
        except GithubException:
            logger.error("Could not post fallback comment either — check GITHUB_TOKEN permissions")

    gh.close()
    return pr.html_url
