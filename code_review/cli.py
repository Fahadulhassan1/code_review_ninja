"""CLI for running code reviews — used by GitHub Actions and local testing.

Usage:
    # Review a GitHub PR (just paste the link)
    uv run python -m code_review https://github.com/owner/repo/pull/42

    # Review and post the comment back to GitHub
    uv run python -m code_review https://github.com/owner/repo/pull/42 --post

    # Review a local diff (pipe from git)
    git diff main | uv run python -m code_review --stdin

    # Review with a sample Go file (for testing without GitHub)
    uv run python -m code_review --demo
"""

import argparse
import re
import sys

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn

from code_review.agents import DailyQuotaExceeded
from code_review.graph import build_review_graph
from code_review.llm import validate_env
from code_review.state import FileDiff, ReviewState

console = Console()


def _parse_unified_diff(diff_text: str) -> list[FileDiff]:
    """Parse a unified diff string (from git diff) into FileDiff objects."""
    files = []
    current_file = None
    current_patch_lines = []

    for line in diff_text.split("\n"):
        if line.startswith("diff --git"):
            # Save previous file
            if current_file:
                current_file.patch = "\n".join(current_patch_lines)
                files.append(current_file)
                current_patch_lines = []

            # Extract filename from "diff --git a/path b/path"
            parts = line.split(" b/")
            filename = parts[-1] if len(parts) > 1 else "unknown"

            from code_review.github_client import _detect_language

            current_file = FileDiff(
                filename=filename,
                status="modified",
                language=_detect_language(filename),
            )
        elif current_file:
            current_patch_lines.append(line)
            if line.startswith("+") and not line.startswith("+++"):
                current_file.additions += 1
            elif line.startswith("-") and not line.startswith("---"):
                current_file.deletions += 1

    # Don't forget the last file
    if current_file:
        current_file.patch = "\n".join(current_patch_lines)
        files.append(current_file)

    return files


DEMO_GO_DIFF = '''diff --git a/handler.go b/handler.go
--- a/handler.go
+++ b/handler.go
@@ -1,5 +1,45 @@
 package main

 import (
+    "database/sql"
+    "fmt"
+    "net/http"
+    "os/exec"
 )

+var db *sql.DB
+
+func GetUser(w http.ResponseWriter, r *http.Request) {
+    userID := r.URL.Query().Get("id")
+
+    // Query user from database
+    query := fmt.Sprintf("SELECT * FROM users WHERE id = '%s'", userID)
+    rows, err := db.Query(query)
+    if err != nil {
+        http.Error(w, err.Error(), 500)
+        return
+    }
+
+    for rows.Next() {
+        var name string
+        rows.Scan(&name)
+        fmt.Fprintf(w, "User: %s", name)
+    }
+}
+
+func RunCommand(w http.ResponseWriter, r *http.Request) {
+    cmd := r.URL.Query().Get("cmd")
+    output, _ := exec.Command("sh", "-c", cmd).Output()
+    w.Write(output)
+}
+
+func ProcessData(items []string) []string {
+    results := []string{}
+    for _, item := range items {
+        for _, other := range items {
+            if item == other {
+                results = append(results, item)
+            }
+        }
+    }
+    return results
+}
'''


def run_review_on_diffs(file_diffs: list[FileDiff], pr_number: int = 0) -> str:
    """Run the multi-agent review on a list of file diffs. Returns the review comment."""
    state = ReviewState(
        pr_number=pr_number,
        pr_title="Local Review",
        file_diffs=file_diffs,
    )

    graph = build_review_graph()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("Running review agents...", total=None)

        final_update = {}
        for event in graph.stream(state, stream_mode="updates"):
            for node_name, update in event.items():
                progress.update(task, description=f"Agent: [bold green]{node_name}[/bold green]")

                if node_name == "security":
                    n = len(update.get("security_findings", []))
                    console.print(f"  🔒 Security: [bold]{n}[/bold] findings")
                elif node_name == "performance":
                    n = len(update.get("performance_findings", []))
                    console.print(f"  ⚡ Performance: [bold]{n}[/bold] findings")
                elif node_name == "style":
                    n = len(update.get("style_findings", []))
                    console.print(f"  🎨 Style: [bold]{n}[/bold] findings")
                elif node_name == "docs":
                    n = len(update.get("docs_findings", []))
                    console.print(f"  📝 Docs: [bold]{n}[/bold] findings")
                elif node_name == "aggregator":
                    total = update.get("total_findings", 0)
                    console.print(f"  📊 Total: [bold]{total}[/bold] findings")

                final_update.update(update)

    return final_update.get("review_comment", "No review generated")


# Regex to parse GitHub PR URLs like https://github.com/owner/repo/pull/42
_PR_URL_PATTERN = re.compile(
    r"^https?://github\.com/([^/]+/[^/]+)/pull/(\d+)/?$"
)


def _parse_pr_url(url: str) -> tuple[str, int] | None:
    """Extract (owner/repo, pr_number) from a GitHub PR URL. Returns None if invalid."""
    m = _PR_URL_PATTERN.match(url)
    if m:
        return m.group(1), int(m.group(2))
    return None


def main():
    parser = argparse.ArgumentParser(
        description="AI Code Review Bot",
        epilog="Example: uv run python -m code_review https://github.com/owner/repo/pull/42",
    )
    parser.add_argument("url", nargs="?", help="GitHub PR URL (e.g. https://github.com/owner/repo/pull/42)")
    parser.add_argument("--repo", help="GitHub repo (owner/repo)")
    parser.add_argument("--pr", type=int, help="PR number")
    parser.add_argument("--stdin", action="store_true", help="Read diff from stdin")
    parser.add_argument("--demo", action="store_true", help="Run demo with sample Go code")
    parser.add_argument("--post", action="store_true", help="Post review to GitHub")
    args = parser.parse_args()

    # Parse PR URL if provided as positional argument
    if args.url:
        parsed = _parse_pr_url(args.url)
        if not parsed:
            console.print(f"[red]Invalid PR URL:[/red] {args.url}")
            console.print("Expected format: https://github.com/owner/repo/pull/123")
            sys.exit(1)
        args.repo, args.pr = parsed

    # Show help if no mode selected
    if not (args.demo or args.stdin or (args.repo and args.pr)):
        parser.print_help()
        console.print("\n[yellow]Tip: Try --demo for a quick test![/yellow]")
        sys.exit(1)

    # Fail fast if GROQ_API_KEY is missing
    validate_env()

    console.print("\n[bold cyan]═══ Agentic AI Code Review Bot ═══[/bold cyan]\n")
    console.print("Powered by [bold]Groq[/bold] (Llama 3.3 70B) + [bold]LangGraph[/bold]\n")

    try:
        review = _run_mode(args)
    except DailyQuotaExceeded as e:
        console.print(f"\n[bold red]⛔ Groq free tier daily token limit reached.[/bold red]")
        console.print(f"[yellow]{e.wait_message}[/yellow]")
        console.print("\nTip: Check your usage at [link=https://console.groq.com]console.groq.com[/link]")
        sys.exit(1)

    # Display the review
    console.print("\n")
    console.print(Panel(
        Markdown(review),
        title="📋 Code Review Results",
        border_style="green",
        padding=(1, 2),
    ))


def _run_mode(args) -> str:
    """Execute the selected review mode and return the review comment."""
    if args.demo:
        console.print("[yellow]Running demo review on sample Go code with intentional vulnerabilities...[/yellow]\n")
        file_diffs = _parse_unified_diff(DEMO_GO_DIFF)
        return run_review_on_diffs(file_diffs)

    if args.stdin:
        console.print("[yellow]Reading diff from stdin...[/yellow]\n")
        diff_text = sys.stdin.read()
        if not diff_text.strip():
            console.print("[red]No diff provided on stdin[/red]")
            sys.exit(1)
        file_diffs = _parse_unified_diff(diff_text)
        return run_review_on_diffs(file_diffs)

    if args.repo and args.pr:
        console.print(f"[yellow]Fetching PR #{args.pr} from {args.repo}...[/yellow]\n")
        from code_review.github_client import fetch_pr_diffs, post_review_comment

        metadata, file_diffs = fetch_pr_diffs(args.repo, args.pr)
        console.print(f"  PR: {metadata['pr_title']}")
        console.print(f"  Files: {len(file_diffs)}\n")

        review = run_review_on_diffs(file_diffs, pr_number=args.pr)

        if args.post:
            console.print("\n[yellow]Posting review to GitHub...[/yellow]")
            url = post_review_comment(args.repo, args.pr, review)
            console.print(f"  ✅ Posted: {url}")
        return review

    # No mode selected — should not reach here due to earlier checks
    sys.exit(1)


if __name__ == "__main__":
    main()
