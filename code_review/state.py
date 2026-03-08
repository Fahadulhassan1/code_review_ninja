"""State definitions for the code review multi-agent graph."""

from enum import Enum
from typing import Annotated

from pydantic import BaseModel, Field
from langgraph.graph.message import add_messages


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class ReviewCategory(str, Enum):
    SECURITY = "security"
    PERFORMANCE = "performance"
    STYLE = "style"
    DOCUMENTATION = "documentation"


class ReviewFinding(BaseModel):
    """A single code review finding from a specialist agent."""
    category: ReviewCategory
    severity: Severity
    file_path: str = Field(description="File path where the issue was found")
    line_range: str = Field(default="", description="Line range, e.g. '10-15'")
    title: str = Field(description="Short summary of the finding")
    description: str = Field(description="Detailed explanation of the issue")
    suggestion: str = Field(default="", description="Suggested fix or improvement")


class FileDiff(BaseModel):
    """A single file's diff from a PR."""
    filename: str
    status: str = Field(description="added, modified, removed, renamed")
    patch: str = Field(default="", description="The unified diff patch")
    additions: int = 0
    deletions: int = 0
    language: str = Field(default="", description="Detected programming language")


class ReviewState(BaseModel):
    """Shared state for the code review agent graph."""
    # PR metadata
    pr_number: int = Field(description="GitHub PR number")
    pr_title: str = Field(default="")
    pr_body: str = Field(default="", description="PR description")
    repo_full_name: str = Field(default="", description="owner/repo")
    base_branch: str = Field(default="main")

    # Code diffs to review
    file_diffs: list[FileDiff] = Field(default_factory=list)

    # Agent findings
    security_findings: list[ReviewFinding] = Field(default_factory=list)
    performance_findings: list[ReviewFinding] = Field(default_factory=list)
    style_findings: list[ReviewFinding] = Field(default_factory=list)
    docs_findings: list[ReviewFinding] = Field(default_factory=list)

    # Aggregated output
    summary: str = Field(default="", description="Overall review summary")
    review_comment: str = Field(default="", description="Final formatted PR comment")

    # Metadata
    total_findings: int = 0
    has_critical: bool = False

    # Message history
    messages: Annotated[list, add_messages] = Field(default_factory=list)
