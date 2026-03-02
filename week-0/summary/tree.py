"""Tree pruning and formatting utilities.

Pure logic — no I/O, no API calls. Operates on lists of TreeEntry objects
from the GitHub client.
"""

from __future__ import annotations

from dataclasses import dataclass

from summary.github import FileType, TreeEntry

# ── L1 well-known files (case-insensitive basenames) ──────────────────────

L1_FILENAMES: set[str] = {
    # Standard project files
    "readme.md",
    "readme.rst",
    "readme.txt",
    "readme",
    "contributing.md",
    "security.md",
    # LLM-specific instruction files
    "agents.md",
    "gemini.md",
    "claude.md",
    "copilot.md",
    ".cursorrules",
    ".clinerules",
    "llms.txt",
    "llms-full.txt",
    "context.md",
}


def find_l1_files(tree: list[TreeEntry]) -> list[TreeEntry]:
    """Find L1 (always-include) files in the tree.

    Matches well-known filenames at the repo root only (depth 0).
    Returns them sorted by the canonical order above.
    """
    results = []
    for entry in tree:
        if entry.type != FileType.BLOB:
            continue
        if entry.depth == 0 and entry.name.lower() in L1_FILENAMES:
            results.append(entry)

    # Sort by canonical order: README first, then the rest alphabetically
    def sort_key(e: TreeEntry) -> tuple[int, str]:
        name = e.name.lower()
        if name.startswith("readme"):
            return (0, name)
        return (1, name)

    return sorted(results, key=sort_key)


# ── Skip patterns ────────────────────────────────────────────────────────

SKIP_EXTENSIONS: set[str] = {
    # Binary / media
    "png", "jpg", "jpeg", "gif", "bmp", "ico", "svg", "webp", "avif",
    "mp3", "mp4", "wav", "avi", "mov", "webm", "ogg", "flac",
    "ttf", "otf", "woff", "woff2", "eot",
    "zip", "tar", "gz", "bz2", "xz", "7z", "rar",
    "exe", "dll", "so", "dylib", "bin", "o", "a",
    "wasm", "class",
    # Python compiled
    "pyc", "pyo", "pyd",
    # Data (usually large, low signal)
    "db", "sqlite", "sqlite3",
    "pdf", "doc", "docx", "xls", "xlsx",
    # Minified
    "min.js", "min.css",
}

SKIP_DIRECTORIES: set[str] = {
    "node_modules", "vendor", "__pycache__", ".git", ".svn", ".hg",
    "dist", "build", ".next", ".nuxt", ".output",
    ".tox", ".nox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "venv", ".venv", "env", ".env",
    ".idea", ".vscode",
    "target",       # Rust/Java build output
    "coverage",
    "site-packages",
}

SKIP_FILENAMES: set[str] = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "poetry.lock", "cargo.lock", "gemfile.lock", "composer.lock",
    "go.sum",
}

MAX_FILE_SIZE = 500_000  # 500 KB — files larger than this are likely not prose


def should_skip(entry: TreeEntry) -> bool:
    """Check if a tree entry should be excluded from analysis."""
    name_lower = entry.name.lower()

    # Skip known junk directories
    if entry.type == FileType.TREE:
        return name_lower in SKIP_DIRECTORIES

    # Skip by extension
    if entry.extension in SKIP_EXTENSIONS:
        return True

    # Skip by filename
    if name_lower in SKIP_FILENAMES:
        return True

    # Skip oversized files
    if entry.size > MAX_FILE_SIZE:
        return True

    # Skip hidden files (dotfiles), but not dotfile configs like .env.example
    if name_lower.startswith(".") and "." not in name_lower[1:]:
        return True

    return False


# ── Tree pruning ─────────────────────────────────────────────────────────

@dataclass
class PruneConfig:
    """Configuration for tree pruning."""
    max_children: int = 30      # Collapse dirs with more than this many direct children
    max_total_entries: int = 200 # Stop listing after this many entries


def prune_tree(
    tree: list[TreeEntry],
    config: PruneConfig | None = None,
) -> list[TreeEntry]:
    """Post-process a tree by collapsing oversized directories and capping entries.

    Note: depth limiting and skip filtering are handled by fetch_tree().
    This function only handles max_children and total entry cap.

    Returns a filtered list of TreeEntry objects suitable for display.
    """
    if config is None:
        config = PruneConfig()

    result = list(tree)
    collapsed_dirs: set[str] = set()

    # Step 1: Collapse directories with too many direct children
    dir_child_counts: dict[str, int] = {}
    for entry in result:
        parent = entry.path.rsplit("/", 1)[0] if "/" in entry.path else ""
        dir_child_counts[parent] = dir_child_counts.get(parent, 0) + 1

    large_dirs: set[str] = {
        d for d, count in dir_child_counts.items()
        if count > config.max_children and d != ""
    }

    if large_dirs:
        final: list[TreeEntry] = []
        for entry in result:
            parent = entry.path.rsplit("/", 1)[0] if "/" in entry.path else ""
            if parent in large_dirs:
                continue  # Skip children of oversized directories
            final.append(entry)
        collapsed_dirs.update(large_dirs)
        result = final

    # Step 2: Limit total entries
    if len(result) > config.max_total_entries:
        result = result[:config.max_total_entries]

    return result


# ── Tree formatting ──────────────────────────────────────────────────────

def format_tree(
    tree: list[TreeEntry],
    collapsed_dirs: set[str] | None = None,
) -> str:
    """Format a tree as a flat list of full paths with sizes.

    Using full paths (not indented) so the LLM sees exact file paths
    and doesn't hallucinate wrong directory prefixes.

    Example output:
        README.md (2.9 KB)
        src/requests/
        src/requests/api.py (5.0 KB)
        src/requests/auth.py (3.2 KB)
        tests/ [collapsed]
    """
    if not tree:
        return "(empty repository)"

    if collapsed_dirs is None:
        collapsed_dirs = set()

    lines: list[str] = []

    for entry in tree:
        if entry.type == FileType.TREE:
            if entry.path in collapsed_dirs:
                lines.append(f"{entry.path}/ [collapsed]")
            else:
                lines.append(f"{entry.path}/")
        else:
            size_str = _format_size(entry.size)
            lines.append(f"{entry.path} ({size_str})")

    return "\n".join(lines)


def _format_size(size_bytes: int) -> str:
    """Format byte count as human-readable string."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    else:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
