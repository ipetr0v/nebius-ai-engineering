#!/usr/bin/env python3
"""Integration test: run the summarizer against real repositories.

Usage:
    cd week-0
    source ../.env  # load API keys
    python tests/integration_test.py [--provider google|nebius]

Reads repositories from testdata/repositories.txt, writes results to
testdata/summaries.md.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

# Add parent directory to path so we can import the summary package
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from summary.agent import AnalysisStats, RepoAnalyzer
from summary.github import GitHubClient
from summary.llm import create_llm_client


WEEK0_DIR = Path(__file__).resolve().parent.parent
TESTDATA_DIR = WEEK0_DIR / "testdata"
REPOS_FILE = TESTDATA_DIR / "repositories.txt"
OUTPUT_FILE = TESTDATA_DIR / "summaries.md"

DEFAULT_TOKEN_FILE = Path("~/.ssh/github_token").expanduser()


def load_repos() -> list[str]:
    """Load repository URLs from repositories.txt, skipping comments."""
    lines = REPOS_FILE.read_text().strip().split("\n")
    return [l.strip() for l in lines if l.strip() and not l.strip().startswith("#")]


def load_github_token() -> str | None:
    """Load GitHub token from env or default file."""
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        return token
    if DEFAULT_TOKEN_FILE.is_file():
        return DEFAULT_TOKEN_FILE.read_text().strip()
    return None


def write_report(results: list[dict], output: Path) -> None:
    """Write batch results to a markdown summary file."""
    lines = ["# Integration Test Results\n"]

    ok = sum(1 for r in results if r["status"] == "ok")
    lines.append(f"**{ok}/{len(results)}** repositories summarized successfully.\n")

    for r in results:
        url = r["url"]
        parts = url.rstrip("/").split("/")
        repo_name = f"{parts[-2]}/{parts[-1]}"

        lines.append("---\n")
        lines.append(f"## [{repo_name}]({url})\n")

        if r["status"] == "error":
            lines.append(f"**❌ Error**: {r['error']}\n")
            continue

        stats: AnalysisStats = r["stats"]
        lines.append(
            f"| Metric | Value |\n"
            f"|--------|-------|\n"
            f"| Tree | {stats.tree_entries_raw} → {stats.tree_entries_pruned} entries ({stats.tree_tokens:,} tok) |\n"
            f"| L1 files | {stats.l1_files} files ({stats.l1_tokens:,} tok) |\n"
            f"| L2 files | {stats.l2_files_fetched}/{stats.l2_files_requested} files ({stats.l2_tokens:,} tok) |\n"
            f"| Context total | {stats.total_tokens:,} / {stats.budget:,} tok ({stats.budget_used_pct:.1f}%) |\n"
            f"| LLM input | {stats.llm_input_tokens:,} tok |\n"
            f"| LLM output | {stats.llm_output_tokens:,} tok |\n"
            f"| LLM total | {stats.llm_total_tokens:,} tok |\n"
            f"| Time | {stats.elapsed_seconds:.1f}s |\n"
        )
        lines.append(f"\n### Summary\n\n{r['summary']}\n")
        lines.append(f"\n### Technologies\n\n{', '.join(r['technologies'])}\n")
        lines.append(f"\n### Structure\n\n{r['structure']}\n")

    output.write_text("\n".join(lines))


async def run(provider: str | None) -> None:
    repos = load_repos()
    print(f"📋 Integration test: {len(repos)} repositories")

    token = load_github_token()
    if token:
        print("🔑 Using GitHub token")
    else:
        print("🔓 No GitHub token (60 req/hr)")

    github = GitHubClient(token=token)
    llm = create_llm_client(provider=provider)
    analyzer = RepoAnalyzer(github=github, llm=llm)

    results: list[dict] = []

    for i, url in enumerate(repos, 1):
        print(f"\n{'═' * 60}")
        print(f"[{i}/{len(repos)}] {url}")
        print(f"{'═' * 60}")

        try:
            result, stats = await analyzer.analyze(url)
            results.append({
                "url": url,
                "status": "ok",
                "summary": result.summary,
                "technologies": result.technologies,
                "structure": result.structure,
                "stats": stats,
            })
            print(f"  ✅ Done")
        except Exception as e:
            results.append({
                "url": url,
                "status": "error",
                "error": str(e),
            })
            print(f"  ❌ Error: {e}")

        # Reset LLM usage for next repo
        llm.total_usage = type(llm.total_usage)()

    await github.close()
    await llm.close()

    write_report(results, OUTPUT_FILE)
    print(f"\n📝 Results → {OUTPUT_FILE}")

    # Summary table
    print(f"\n{'═' * 60}")
    print("📊 Summary")
    print(f"{'═' * 60}")
    for r in results:
        parts = r["url"].rstrip("/").split("/")
        name = f"{parts[-2]}/{parts[-1]}"
        if r["status"] == "ok":
            s = r["stats"]
            print(f"  ✅ {name:<35} {s.total_tokens:>6,} ctx tok | "
                  f"{s.llm_total_tokens:>6,} llm tok | {s.elapsed_seconds:.1f}s")
        else:
            print(f"  ❌ {name:<35} {r['error'][:40]}")


def main():
    parser = argparse.ArgumentParser(description="Integration test for repo summarizer")
    parser.add_argument("--provider", choices=["nebius", "google"], default=None)
    args = parser.parse_args()
    asyncio.run(run(args.provider))


if __name__ == "__main__":
    main()
