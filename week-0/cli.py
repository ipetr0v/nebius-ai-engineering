#!/usr/bin/env python3
"""CLI for testing the GitHub repository summarizer.

Usage:
    # Full analysis (reads GitHub token from ~/.ssh/github_token):
    python cli.py https://github.com/psf/requests

    # Just print the README:
    python cli.py https://github.com/psf/requests --readme-only

    # Force a specific LLM provider:
    python cli.py https://github.com/psf/requests --provider google

    # Without GitHub auth:
    python cli.py https://github.com/psf/requests --no-token
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path

from summary.github import GitHubClient

DEFAULT_TOKEN_FILE = Path("~/.ssh/github_token").expanduser()


def load_token(token_file: Path | None, no_token: bool) -> str | None:
    """Load GitHub token from file or environment.

    Priority: --no-token flag > --token-file argument > env var > default file.
    """
    if no_token:
        return None

    if token_file and token_file.is_file():
        return token_file.read_text().strip()

    env_token = os.environ.get("GITHUB_TOKEN")
    if env_token:
        return env_token

    if DEFAULT_TOKEN_FILE.is_file():
        return DEFAULT_TOKEN_FILE.read_text().strip()

    return None


async def run_readme(url: str, github: GitHubClient) -> None:
    """Fetch and print just the README (quick test mode)."""
    owner, repo = GitHubClient.parse_url(url)
    print(f"📦 Fetching repo: {owner}/{repo}")

    tree = await github.fetch_tree(owner, repo)
    files = [e for e in tree if e.type.value == "blob"]
    dirs = [e for e in tree if e.type.value == "tree"]
    print(f"📂 Found {len(files)} files in {len(dirs)} directories")

    readme_entry = None
    for entry in tree:
        if entry.name.lower().startswith("readme"):
            readme_entry = entry
            break

    if not readme_entry:
        print("⚠️  No README found in this repository.")
        return

    print(f"📄 Reading: {readme_entry.path} ({readme_entry.size:,} bytes)")
    print("─" * 60)
    content = await github.fetch_file(owner, repo, readme_entry.path)
    print(content)


async def run_analysis(url: str, github: GitHubClient, provider: str | None) -> None:
    """Run the full L1+L2 analysis pipeline."""
    from summary.agent import RepoAnalyzer
    from summary.llm import create_llm_client

    llm = create_llm_client(provider=provider)
    analyzer = RepoAnalyzer(github=github, llm=llm)

    try:
        owner, repo = GitHubClient.parse_url(url)
        print(f"📦 Analyzing: {owner}/{repo}")
        print("─" * 60)

        result, stats = await analyzer.analyze(url)

        print("\n" + "═" * 60)
        print("📋 SUMMARY")
        print("═" * 60)
        print(json.dumps(
            {
                "summary": result.summary,
                "technologies": result.technologies,
                "structure": result.structure,
            },
            indent=2,
        ))
    finally:
        await llm.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Analyze a GitHub repository and generate a summary."
    )
    parser.add_argument("url", help="GitHub repository URL")
    parser.add_argument(
        "--readme-only",
        action="store_true",
        help="Just fetch and print the README (skip LLM analysis)",
    )
    parser.add_argument(
        "--provider",
        choices=["nebius", "google"],
        default=None,
        help="LLM provider (auto-detected from env vars if not specified)",
    )
    parser.add_argument(
        "--token-file",
        type=Path,
        default=None,
        help=f"Path to GitHub token file (default: {DEFAULT_TOKEN_FILE})",
    )
    parser.add_argument(
        "--no-token",
        action="store_true",
        help="Run without GitHub authentication (60 req/hr limit)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose logging (shows raw LLM responses)",
    )
    args = parser.parse_args()

    # Configure logging
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
    )
    # Silence noisy HTTP library logs
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    token = load_token(args.token_file, args.no_token)
    if token:
        print("🔑 Using GitHub token")
    else:
        print("🔓 No token — unauthenticated API (60 req/hr)")

    github = GitHubClient(token=token)

    async def run():
        try:
            if args.readme_only:
                await run_readme(args.url, github)
            else:
                await run_analysis(args.url, github, args.provider)
        finally:
            await github.close()

    asyncio.run(run())


if __name__ == "__main__":
    main()
