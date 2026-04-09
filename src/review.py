"""Entrypoint for the PR Review Agent GitHub Action."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import anthropic
import requests

from .prompts import build_review_prompt

_PREFIX = "[pr-review-agent]"


def write_output(name: str, value: str) -> None:
    """Write a GitHub Actions output variable."""
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as fh:
            fh.write(f"{name}={value}\n")
    else:
        print(f"  {name}={value}")


def get_pr_diff() -> str:
    """Get the diff for the current PR.

    Uses GITHUB_BASE_REF to diff against the target branch.
    Falls back to diffing against origin/main.
    """
    workspace = os.environ.get("GITHUB_WORKSPACE", "/github/workspace")
    base_ref = os.environ.get("GITHUB_BASE_REF", "main")

    # Mark workspace as safe for git (Docker container runs as root)
    subprocess.run(
        ["git", "config", "--global", "--add", "safe.directory", workspace],
        capture_output=True,
        text=True,
    )

    # Fetch the base branch so we can diff against it
    subprocess.run(
        ["git", "fetch", "origin", base_ref, "--depth=1"],
        capture_output=True,
        text=True,
        cwd=workspace,
    )

    result = subprocess.run(
        ["git", "diff", f"origin/{base_ref}...HEAD", "--unified=3"],
        capture_output=True,
        text=True,
        cwd=workspace,
    )

    if result.returncode != 0:
        print(f"{_PREFIX} WARNING: git diff failed: {result.stderr}", file=sys.stderr)
        # Fallback: diff against HEAD~1
        result = subprocess.run(
            ["git", "diff", "HEAD~1", "--unified=3"],
            capture_output=True,
            text=True,
            cwd=workspace,
        )

    return result.stdout


def get_changed_files_content(diff: str) -> dict[str, str]:
    """Parse diff to extract file paths, then read full content of changed files."""
    files = {}
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            path = line[6:]
            if os.path.isfile(path):
                try:
                    with open(path, "r", encoding="utf-8", errors="replace") as f:
                        files[path] = f.read()
                except OSError:
                    pass
    return files


def post_pr_comment(body: str, github_token: str) -> None:
    """Post a top-level comment on the PR."""
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path:
        print(f"{_PREFIX} No GITHUB_EVENT_PATH — printing comment to stdout")
        print(body)
        return

    with open(event_path, "r") as f:
        event = json.load(f)

    pr_number = event.get("pull_request", {}).get("number")
    repo = os.environ.get("GITHUB_REPOSITORY")

    if not pr_number or not repo:
        print(f"{_PREFIX} Cannot determine PR number or repo — printing comment to stdout")
        print(body)
        return

    url = f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments"
    headers = {
        "Authorization": f"token {github_token}",
        "Accept": "application/vnd.github.v3+json",
    }

    resp = requests.post(url, headers=headers, json={"body": body}, timeout=30)

    if resp.status_code in (200, 201):
        print(f"{_PREFIX} Posted PR comment: {resp.json().get('html_url')}")
    else:
        print(f"{_PREFIX} Failed to post comment: {resp.status_code} {resp.text}", file=sys.stderr)


def post_inline_comments(
    findings: list[dict], github_token: str, commit_sha: str
) -> int:
    """Post inline review comments on the PR via the reviews API."""
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path:
        return 0

    with open(event_path, "r") as f:
        event = json.load(f)

    pr_number = event.get("pull_request", {}).get("number")
    repo = os.environ.get("GITHUB_REPOSITORY")

    if not pr_number or not repo:
        return 0

    # Build review comments
    comments = []
    for finding in findings:
        if not finding.get("file") or not finding.get("line"):
            continue

        severity_icon = {
            "critical": "\U0001f6a8",
            "high": "\U0001f534",
            "medium": "\U0001f7e1",
            "low": "\U0001f535",
        }.get(finding.get("severity", "medium"), "\U0001f7e1")

        body = (
            f"{severity_icon} **{finding.get('severity', 'medium').upper()}** — "
            f"{finding.get('title', 'Issue found')}\n\n"
            f"{finding.get('description', '')}\n"
        )
        if finding.get("suggestion"):
            body += f"\n**Suggestion:**\n```\n{finding['suggestion']}\n```\n"

        comments.append({
            "path": finding["file"],
            "line": finding["line"],
            "body": body,
        })

    if not comments:
        return 0

    url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}/reviews"
    headers = {
        "Authorization": f"token {github_token}",
        "Accept": "application/vnd.github.v3+json",
    }

    payload = {
        "commit_id": commit_sha,
        "body": "\U0001f916 **PR Review Agent** — Automated code review powered by NVIDIA Inference Hub + Claude",
        "event": "COMMENT",
        "comments": comments,
    }

    resp = requests.post(url, headers=headers, json=payload, timeout=30)

    if resp.status_code in (200, 201):
        print(f"{_PREFIX} Posted {len(comments)} inline comment(s)")
        return len(comments)
    else:
        print(
            f"{_PREFIX} Failed to post inline comments: {resp.status_code} {resp.text}",
            file=sys.stderr,
        )
        return 0


def format_summary_comment(findings: list[dict], summary: str) -> str:
    """Format findings into a markdown PR comment."""
    lines = [
        "## \U0001f916 PR Review Agent\n",
        f"> {summary}\n",
    ]

    if not findings:
        lines.append("\u2705 **No issues found.** This PR looks good!\n")
    else:
        severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        sorted_findings = sorted(
            findings,
            key=lambda f: severity_order.get(f.get("severity", "medium"), 2),
        )

        lines.append(f"Found **{len(findings)}** issue(s):\n")
        lines.append("| Severity | File | Line | Issue |")
        lines.append("|----------|------|------|-------|")

        for f in sorted_findings:
            sev = f.get("severity", "medium")
            icon = {
                "critical": "\U0001f6a8",
                "high": "\U0001f534",
                "medium": "\U0001f7e1",
                "low": "\U0001f535",
            }.get(sev, "\U0001f7e1")
            lines.append(
                f"| {icon} {sev} | `{f.get('file', '?')}` | "
                f"L{f.get('line', '?')} | {f.get('title', '')} |"
            )

    lines.append(
        "\n---\n*Powered by NVIDIA Inference Hub + Claude*"
    )

    return "\n".join(lines)


def main() -> None:
    """Main entrypoint."""

    def _env(*names: str) -> str:
        for name in names:
            val = os.environ.get(name, "").strip()
            if val:
                return val
        return ""

    # Read inputs
    api_key = _env("INPUT_INFERENCE-HUB-API-KEY", "INPUT_INFERENCE_HUB_API_KEY")
    model = (
        _env("INPUT_INFERENCE-HUB-MODEL", "INPUT_INFERENCE_HUB_MODEL")
        or "aws/anthropic/bedrock-claude-opus-4-6"
    )
    review_type = _env("INPUT_REVIEW-TYPE", "INPUT_REVIEW_TYPE") or "both"
    custom_instructions = _env("INPUT_CUSTOM-INSTRUCTIONS", "INPUT_CUSTOM_INSTRUCTIONS")
    max_comments_str = _env("INPUT_MAX-COMMENTS", "INPUT_MAX_COMMENTS") or "15"
    github_token = _env("INPUT_GITHUB-TOKEN", "INPUT_GITHUB_TOKEN", "GITHUB_TOKEN")

    try:
        max_comments = int(max_comments_str)
    except ValueError:
        max_comments = 15

    if not api_key:
        print(f"{_PREFIX} ERROR: inference-hub-api-key is required", file=sys.stderr)
        sys.exit(1)

    print(f"{_PREFIX} Starting PR review (model={model}, type={review_type})")

    # Get PR diff
    diff = get_pr_diff()
    if not diff.strip():
        print(f"{_PREFIX} No diff found — nothing to review")
        write_output("findings-count", "0")
        write_output("summary", "No changes to review")
        return

    # Truncate very large diffs
    max_diff_chars = 80_000
    if len(diff) > max_diff_chars:
        diff = diff[:max_diff_chars] + "\n\n... (diff truncated) ..."
        print(f"{_PREFIX} Diff truncated to {max_diff_chars} chars")

    # Build prompt
    system_prompt = build_review_prompt(review_type, custom_instructions)

    user_message = (
        "Review the following pull request diff. "
        "Focus on real issues — skip style nitpicks.\n\n"
        f"```diff\n{diff}\n```"
    )

    # Call Inference Hub (Anthropic messages API)
    print(f"{_PREFIX} Calling Inference Hub ({model}) ...")

    client = anthropic.Anthropic(
        api_key=api_key,
        base_url="https://inference-api.nvidia.com",
    )

    try:
        response = client.messages.create(
            model=model,
            max_tokens=4096,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )
    except Exception as exc:
        print(f"{_PREFIX} ERROR calling Inference Hub: {exc}", file=sys.stderr)
        write_output("findings-count", "0")
        write_output("summary", f"Review failed: {exc}")
        return

    raw_text = response.content[0].text
    print(f"{_PREFIX} Got response ({len(raw_text)} chars)")

    # Parse JSON response
    try:
        # Strip markdown fences if present
        text = raw_text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1]
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
        text = text.strip()

        result = json.loads(text)
        findings = result.get("findings", [])
        summary = result.get("summary", "Review complete")
    except (json.JSONDecodeError, KeyError) as exc:
        print(f"{_PREFIX} WARNING: Could not parse JSON response: {exc}", file=sys.stderr)
        print(f"{_PREFIX} Raw response:\n{raw_text[:500]}", file=sys.stderr)
        findings = []
        summary = raw_text[:200]

    # Cap findings
    findings = findings[:max_comments]

    print(f"{_PREFIX} Found {len(findings)} issue(s)")

    # Post summary comment
    summary_comment = format_summary_comment(findings, summary)

    if github_token:
        post_pr_comment(summary_comment, github_token)

        # Post inline comments
        commit_sha = os.environ.get("GITHUB_SHA", "")
        if findings and commit_sha:
            post_inline_comments(findings, github_token, commit_sha)
    else:
        print(f"{_PREFIX} No GitHub token — printing summary:")
        print(summary_comment)

    # Write step summary
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        with open(summary_file, "a", encoding="utf-8") as f:
            f.write(summary_comment)

    # Write outputs
    write_output("findings-count", str(len(findings)))
    write_output("summary", summary)

    print(f"{_PREFIX} Done.")


if __name__ == "__main__":
    main()
