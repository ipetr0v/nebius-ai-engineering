"""Repository analyzer — orchestrates the L1/L2 pipeline.

Ties together GitHubClient, tree utilities, and LLMClient to produce
a structured summary of a GitHub repository.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

from summary.github import GitHubClient, FileType, TreeEntry
from summary.llm import LLMClient, SummaryResult, TokenUsage, count_tokens
from summary.tree import (
    PruneConfig,
    find_l1_files,
    format_tree,
    prune_tree,
)

DEFAULT_MAX_CONTEXT_TOKENS = 80_000
DEFAULT_MAX_FILE_TOKENS = 10_000  # Truncate individual files beyond this


@dataclass
class AnalysisStats:
    """Token usage and timing statistics for an analysis run."""
    tree_entries_raw: int = 0
    tree_entries_pruned: int = 0
    tree_tokens: int = 0
    l1_files: int = 0
    l1_tokens: int = 0
    l2_files_requested: int = 0
    l2_files_fetched: int = 0
    l2_tokens: int = 0
    total_tokens: int = 0
    budget: int = DEFAULT_MAX_CONTEXT_TOKENS
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0
    llm_total_tokens: int = 0
    elapsed_seconds: float = 0.0

    @property
    def budget_used_pct(self) -> float:
        return (self.total_tokens / self.budget) * 100 if self.budget else 0

    def format(self) -> str:
        return (
            f"  ┌─ Context Budget ──────────────────────────────\n"
            f"  │ Tree:     {self.tree_tokens:>7,} tokens ({self.tree_entries_raw} → {self.tree_entries_pruned} entries)\n"
            f"  │ L1 files: {self.l1_tokens:>7,} tokens ({self.l1_files} files)\n"
            f"  │ L2 files: {self.l2_tokens:>7,} tokens ({self.l2_files_fetched}/{self.l2_files_requested} files)\n"
            f"  │ Total:    {self.total_tokens:>7,} / {self.budget:,} tokens ({self.budget_used_pct:.1f}%)\n"
            f"  ├─ LLM Token Usage ────────────────────────────\n"
            f"  │ Input:    {self.llm_input_tokens:>7,} tokens\n"
            f"  │ Output:   {self.llm_output_tokens:>7,} tokens\n"
            f"  │ Total:    {self.llm_total_tokens:>7,} tokens\n"
            f"  ├─────────────────────────────────────────────\n"
            f"  │ Time:     {self.elapsed_seconds:.1f}s\n"
            f"  └─────────────────────────────────────────────"
        )


@dataclass
class AnalysisContext:
    """Accumulated context for the LLM summarization call."""
    tree_text: str = ""
    l1_files: dict[str, str] = field(default_factory=dict)   # path → content
    l2_files: dict[str, str] = field(default_factory=dict)   # path → content
    tokens_used: int = 0
    _budget: int = DEFAULT_MAX_CONTEXT_TOKENS

    def add_section(self, label: str, content: str, store: dict[str, str]) -> int:
        """Add content to context, return tokens consumed."""
        tokens = count_tokens(content)
        store[label] = content
        self.tokens_used += tokens
        return tokens

    @property
    def tokens_remaining(self) -> int:
        return max(0, self._budget - self.tokens_used)

    def set_budget(self, budget: int) -> None:
        self._budget = budget

    def format(self) -> str:
        """Build the final context string for the summarizer LLM."""
        sections: list[str] = []

        # Directory tree first (as user requested)
        if self.tree_text:
            sections.append(
                f"## Directory Structure\n\n```\n{self.tree_text}\n```"
            )

        # L1 files (README, AGENTS.md, etc.)
        for path, content in self.l1_files.items():
            sections.append(f"## {path}\n\n{content}")

        # L2 files (LLM-selected config, docs, source)
        if self.l2_files:
            for path, content in self.l2_files.items():
                sections.append(f"## {path}\n\n{content}")

        return "\n\n---\n\n".join(sections)


class RepoAnalyzer:
    """Orchestrates repository analysis through the L1/L2 pipeline.

    L1 (deterministic): Fetch README, AGENTS.md, llms.txt + generate tree.
    L2 (LLM-guided):    Ask LLM which files to read, then fetch them.
    Final:              Ask LLM to summarize everything.
    """

    def __init__(
        self,
        github: GitHubClient,
        llm: LLMClient,
    ) -> None:
        self._github = github
        self._llm = llm
        self._max_context_tokens = llm.MAX_CONTEXT_TOKENS
        self._max_file_tokens = llm.MAX_FILE_TOKENS

    async def analyze(self, url: str) -> tuple[SummaryResult, AnalysisStats]:
        """Run the full analysis pipeline for a GitHub repository URL.

        Returns:
            Tuple of (SummaryResult, AnalysisStats).
        """
        t0 = time.monotonic()
        owner, repo = GitHubClient.parse_url(url)
        ctx = AnalysisContext()
        ctx.set_budget(self._max_context_tokens)
        stats = AnalysisStats(budget=self._max_context_tokens)

        log.info("Analyzing %s/%s (budget=%d ctx, %d/file)",
                 owner, repo, self._max_context_tokens, self._max_file_tokens)

        # ── Fetch and process the tree ──
        t_tree = time.monotonic()
        raw_tree = await self._github.fetch_tree(owner, repo)
        log.info("  📂 Tree fetched: %d entries in %.1fs",
                 len(raw_tree), time.monotonic() - t_tree)

        pruned = prune_tree(raw_tree)
        ctx.tree_text = format_tree(pruned)
        ctx.tokens_used += count_tokens(ctx.tree_text)

        stats.tree_entries_raw = len(raw_tree)
        stats.tree_entries_pruned = len(pruned)
        stats.tree_tokens = ctx.tokens_used

        log.info("  📂 Tree pruned: %d → %d entries (%d tokens)",
                 len(raw_tree), len(pruned), ctx.tokens_used)

        # ── L1: Fetch deterministic files ──
        l1_entries = find_l1_files(raw_tree)
        l1_token_start = ctx.tokens_used
        for entry in l1_entries:
            if ctx.tokens_remaining <= 0:
                break
            content = await self._github.fetch_file(owner, repo, entry.path)
            content = self._truncate(content)
            tokens = ctx.add_section(entry.path, content, ctx.l1_files)
            log.debug("  📄 L1: %s (%d tokens)", entry.path, tokens)
        stats.l1_files = len(ctx.l1_files)
        stats.l1_tokens = ctx.tokens_used - l1_token_start

        log.debug("  📊 After L1: %d tokens used, %d remaining",
                  ctx.tokens_used, ctx.tokens_remaining)

        # ── L2: LLM-guided file selection ──
        l2_token_start = ctx.tokens_used
        if ctx.tokens_remaining > 5_000:
            l2_paths = await self._llm.pick_files(
                ctx.tree_text, ctx.tokens_remaining
            )
            log.info("  🤖 LLM picked %d files: %s", len(l2_paths), l2_paths)

            # Filter out L1 files (already fetched).
            # Don't validate against tree — tree may be truncated, and
            # fetch_file will 404 gracefully if a path doesn't exist.
            l2_paths = [p for p in l2_paths if p not in ctx.l1_files]

            # Fallback: if LLM returned nothing, pick files deterministically
            if not l2_paths:
                l2_paths = self._fallback_file_picker(raw_tree, ctx.l1_files)
                log.info("  🔄 Fallback picker selected %d files: %s",
                         len(l2_paths), l2_paths)

            stats.l2_files_requested = len(l2_paths)

            for path in l2_paths:
                if ctx.tokens_remaining <= 0:
                    log.warning("  ⚠️  Budget exhausted, skipping remaining L2 files")
                    break
                try:
                    content = await self._github.fetch_file(owner, repo, path)
                    content = self._truncate(content)
                    tokens = ctx.add_section(path, content, ctx.l2_files)
                    log.debug("  📄 L2: %s (%d tokens)", path, tokens)
                except Exception as e:
                    log.warning("  ⚠️  Failed to fetch %s: %s", path, e)

        stats.l2_files_fetched = len(ctx.l2_files)
        stats.l2_tokens = ctx.tokens_used - l2_token_start
        stats.total_tokens = ctx.tokens_used

        # ── Summarize ──
        context_str = ctx.format()
        log.info("  🧠 Sending %d tokens to LLM for summarization...",
                 count_tokens(context_str))
        result = await self._llm.summarize(context_str)

        # Capture LLM usage stats
        llm_usage = self._llm.total_usage
        stats.llm_input_tokens = llm_usage.input_tokens
        stats.llm_output_tokens = llm_usage.output_tokens
        stats.llm_total_tokens = llm_usage.total_tokens

        stats.elapsed_seconds = time.monotonic() - t0
        log.info("\n%s", stats.format())

        return result, stats

    def _truncate(self, content: str) -> str:
        """Truncate file content if it exceeds the per-file token limit."""
        tokens = count_tokens(content)
        if tokens <= self._max_file_tokens:
            return content

        # Take first N lines until we're under budget
        lines = content.split("\n")
        truncated_lines: list[str] = []
        running_tokens = 0

        for line in lines:
            line_tokens = count_tokens(line)
            if running_tokens + line_tokens > self._max_file_tokens:
                break
            truncated_lines.append(line)
            running_tokens += line_tokens

        truncated_lines.append(
            f"\n[... truncated — {tokens:,} tokens total, "
            f"showing first {running_tokens:,} ...]"
        )
        return "\n".join(truncated_lines)

    @staticmethod
    def _fallback_file_picker(
        tree: list[TreeEntry],
        already_fetched: dict[str, str],
    ) -> list[str]:
        """Deterministic fallback when LLM file picker fails.

        Picks high-value files in priority order:
        1. Config/manifest files at root
        2. Documentation files (depth ≤ 1)
        3. Small top-level source files
        """
        # High-priority config filenames (case-insensitive)
        CONFIG_NAMES = {
            "makefile", "cmakelists.txt", "meson.build",
            "pyproject.toml", "setup.py", "setup.cfg",
            "package.json", "cargo.toml", "go.mod",
            "gemfile", "build.gradle", "pom.xml",
            "dockerfile", "docker-compose.yml",
            ".github/workflows/ci.yml",
        }

        DOC_EXTENSIONS = {"md", "rst", "txt"}
        SOURCE_EXTENSIONS = {
            "py", "go", "rs", "js", "ts", "c", "h",
            "java", "rb", "sh", "toml", "yaml", "yml", "json",
        }

        blobs = [
            e for e in tree
            if e.type == FileType.BLOB and e.path not in already_fetched
        ]

        picked: list[str] = []

        # 1. Config files at root
        for entry in blobs:
            if entry.name.lower() in CONFIG_NAMES and entry.depth <= 1:
                picked.append(entry.path)

        # 2. Docs at depth ≤ 1
        for entry in blobs:
            if (entry.extension in DOC_EXTENSIONS
                    and entry.depth <= 1
                    and entry.path not in picked
                    and entry.size < 50_000):
                picked.append(entry.path)

        # 3. Small source files at root (< 20KB)
        for entry in blobs:
            if (entry.extension in SOURCE_EXTENSIONS
                    and entry.depth == 0
                    and entry.path not in picked
                    and entry.size < 20_000):
                picked.append(entry.path)

        return picked[:30]  # Cap at 30 files
