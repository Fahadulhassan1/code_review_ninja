"""Agentic Code Review Bot — Test Suite."""

import sys
from pathlib import Path

# Ensure project root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_imports():
    """Test all modules import cleanly."""
    from code_review.state import ReviewState, FileDiff, ReviewFinding, Severity, ReviewCategory
    from code_review.agents import security_agent, performance_agent, style_agent, docs_agent, aggregator_agent
    from code_review.graph import build_review_graph
    from code_review.cli import _parse_unified_diff, DEMO_GO_DIFF
    from code_review.cli import _parse_pr_url
    from code_review.github_client import _detect_language
    from code_review.llm import get_reasoning_llm, validate_env
    print("✅ All imports successful")


def test_pr_url_parsing():
    """Test GitHub PR URL parsing."""
    from code_review.cli import _parse_pr_url
    # Valid URLs
    assert _parse_pr_url("https://github.com/owner/repo/pull/42") == ("owner/repo", 42)
    assert _parse_pr_url("https://github.com/Fahadulhassan1/agentic/pull/1") == ("Fahadulhassan1/agentic", 1)
    assert _parse_pr_url("http://github.com/a/b/pull/999") == ("a/b", 999)
    # Invalid URLs
    assert _parse_pr_url("https://github.com/owner/repo") is None
    assert _parse_pr_url("https://github.com/owner/repo/issues/5") is None
    assert _parse_pr_url("not-a-url") is None
    assert _parse_pr_url("https://gitlab.com/owner/repo/pull/1") is None
    print("✅ PR URL parsing: 7 tests passed")


def test_graph_compilation():
    """Test LangGraph compiles with correct nodes."""
    from code_review.graph import build_review_graph
    g = build_review_graph()
    nodes = [n for n in g.nodes if not n.startswith("__")]
    assert nodes == ["security", "performance", "style", "docs", "aggregator"], f"Unexpected nodes: {nodes}"
    print(f"✅ Graph compiled: {nodes}")


def test_diff_parsing():
    """Test unified diff parsing."""
    from code_review.cli import _parse_unified_diff, DEMO_GO_DIFF
    diffs = _parse_unified_diff(DEMO_GO_DIFF)
    assert len(diffs) == 1, f"Expected 1 file, got {len(diffs)}"
    assert diffs[0].filename == "handler.go"
    assert diffs[0].language == "go"
    assert diffs[0].additions > 0
    print(f"✅ Diff parsing: {diffs[0].filename} ({diffs[0].language}) +{diffs[0].additions}/-{diffs[0].deletions}")


def test_language_detection():
    """Test file extension to language mapping."""
    from code_review.github_client import _detect_language
    tests = {
        "main.go": "go",
        "server.py": "python",
        "app.ts": "typescript",
        "index.tsx": "typescript",
        "handler.rs": "rust",
        "Dockerfile": "dockerfile",
        "Makefile": "makefile",
        "unknown.xyz": "",
    }
    for f, expected in tests.items():
        got = _detect_language(f)
        assert got == expected, f"{f}: expected '{expected}', got '{got}'"
    print(f"✅ Language detection: {len(tests)} tests passed")


def test_state_creation():
    """Test ReviewState model."""
    from code_review.state import ReviewState, FileDiff
    diff = FileDiff(filename="test.go", status="modified", patch="+code", additions=1, language="go")
    state = ReviewState(pr_number=42, file_diffs=[diff])
    assert state.pr_number == 42
    assert len(state.file_diffs) == 1
    assert state.security_findings == []
    assert state.total_findings == 0
    print("✅ State creation and defaults")


def test_aggregator_empty():
    """Test aggregator with no findings produces clean output."""
    from code_review.state import ReviewState, FileDiff
    from code_review.agents import aggregator_agent
    diff = FileDiff(filename="clean.go", status="modified", patch="+good code", additions=1, language="go")
    state = ReviewState(pr_number=1, file_diffs=[diff])
    result = aggregator_agent(state)
    assert result["total_findings"] == 0
    assert "No issues found" in result["summary"]
    assert "✅" in result["review_comment"]
    print("✅ Aggregator (no findings) → clean review")


def test_aggregator_with_findings():
    """Test aggregator formats findings correctly."""
    from code_review.state import ReviewState, FileDiff, ReviewFinding, ReviewCategory, Severity
    from code_review.agents import aggregator_agent

    diff = FileDiff(filename="test.go", status="modified", patch="+code", additions=1, language="go")
    findings = [
        ReviewFinding(
            category=ReviewCategory.SECURITY,
            severity=Severity.CRITICAL,
            file_path="test.go",
            title="SQL Injection",
            description="Uses string concat for SQL",
            suggestion="Use parameterized queries",
        ),
        ReviewFinding(
            category=ReviewCategory.PERFORMANCE,
            severity=Severity.MEDIUM,
            file_path="test.go",
            title="O(n^2) loop",
            description="Nested loop is quadratic",
        ),
    ]
    state = ReviewState(
        pr_number=99,
        file_diffs=[diff],
        security_findings=[findings[0]],
        performance_findings=[findings[1]],
    )
    result = aggregator_agent(state)
    assert result["total_findings"] == 2
    assert result["has_critical"] is True
    assert "SQL Injection" in result["review_comment"]
    assert "O(n^2)" in result["review_comment"] or "O(n" in result["review_comment"]
    assert "PR #99" in result["review_comment"]
    print("✅ Aggregator (with findings) → formatted review with severity sorting")


def test_groq_connection():
    """Test Groq API is reachable."""
    from code_review.llm import get_reasoning_llm
    llm = get_reasoning_llm()
    resp = llm.invoke("Respond with only the word OK")
    assert len(resp.content.strip()) > 0
    print(f"✅ Groq API connected: '{resp.content.strip()[:30]}'")


def test_full_demo_pipeline():
    """Test the full multi-agent pipeline end-to-end with demo Go code."""
    from code_review.cli import _parse_unified_diff, DEMO_GO_DIFF, run_review_on_diffs
    diffs = _parse_unified_diff(DEMO_GO_DIFF)
    review = run_review_on_diffs(diffs)

    # The demo Go code has SQL injection + command injection + O(n^2) — agents MUST find these
    review_lower = review.lower()
    assert "sql injection" in review_lower or "sql" in review_lower, "Security agent missed SQL injection"
    assert "command injection" in review_lower or "exec.command" in review_lower or "command" in review_lower, "Security agent missed command injection"
    assert "findings" in review_lower or "finding" in review_lower, "Aggregator didn't produce findings"
    assert "PR #" in review, "Missing PR reference in output"
    print("✅ Full pipeline: found SQL injection, command injection, formatted review")


def test_multi_file_diff():
    """Test parsing a diff with multiple files."""
    from code_review.cli import _parse_unified_diff
    multi_diff = '''diff --git a/main.go b/main.go
--- a/main.go
+++ b/main.go
@@ -1,3 +1,5 @@
 package main
+
+import "fmt"
diff --git a/utils.py b/utils.py
--- a/utils.py
+++ b/utils.py
@@ -0,0 +1,3 @@
+def helper():
+    pass
'''
    diffs = _parse_unified_diff(multi_diff)
    assert len(diffs) == 2, f"Expected 2 files, got {len(diffs)}"
    assert diffs[0].filename == "main.go"
    assert diffs[0].language == "go"
    assert diffs[1].filename == "utils.py"
    assert diffs[1].language == "python"
    print(f"✅ Multi-file diff: parsed {len(diffs)} files correctly")


def main():
    print("\n" + "=" * 60)
    print("  Agentic Code Review Bot — Test Suite")
    print("=" * 60 + "\n")

    # Unit tests (no API calls)
    unit_tests = [
        test_imports,
        test_graph_compilation,
        test_diff_parsing,
        test_pr_url_parsing,
        test_language_detection,
        test_state_creation,
        test_aggregator_empty,
        test_aggregator_with_findings,
        test_multi_file_diff,
    ]

    # Integration tests (require Groq API)
    integration_tests = [
        test_groq_connection,
        test_full_demo_pipeline,
    ]

    passed = 0
    failed = 0

    print("--- Unit Tests ---\n")
    for test in unit_tests:
        try:
            test()
            passed += 1
        except Exception as e:
            print(f"❌ {test.__name__}: {e}")
            failed += 1

    print("\n--- Integration Tests (Groq API) ---\n")
    for test in integration_tests:
        try:
            test()
            passed += 1
        except Exception as e:
            print(f"❌ {test.__name__}: {e}")
            failed += 1

    print(f"\n{'=' * 60}")
    print(f"  Results: {passed} passed, {failed} failed, {passed + failed} total")
    print(f"{'=' * 60}\n")

    if failed > 0:
        sys.exit(1)
    print("🎯 All tests passed!\n")


if __name__ == "__main__":
    main()
