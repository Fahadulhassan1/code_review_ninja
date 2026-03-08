"""GitHub API integration for fetching PR data and posting review comments."""

import os
from pathlib import PurePosixPath

from github import Auth, Github

from code_review.state import FileDiff

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
    return Github(auth=Auth.Token(token), timeout=GITHUB_TIMEOUT)


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
