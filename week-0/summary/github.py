"""GitHub API client for fetching repository contents."""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from enum import Enum

import httpx


class GitHubError(Exception):
    """Base exception for GitHub API errors."""


class RepoNotFoundError(GitHubError):
    """Raised when the repository doesn't exist or is private."""


class RateLimitError(GitHubError):
    """Raised when the GitHub API rate limit is exceeded."""


class FileType(Enum):
    """Type of entry in the repository tree."""
    BLOB = "blob"
    TREE = "tree"


@dataclass(frozen=True)
class TreeEntry:
    """A single entry (file or directory) in the repository tree."""
    path: str
    type: FileType
    size: int  # 0 for directories

    @property
    def name(self) -> str:
        """Filename without the directory path."""
        return self.path.rsplit("/", 1)[-1]

    @property
    def extension(self) -> str:
        """File extension (lowercase), empty string if none."""
        if "." in self.name:
            return self.name.rsplit(".", 1)[-1].lower()
        return ""

    @property
    def depth(self) -> int:
        """Directory nesting depth (0 for root-level files)."""
        return self.path.count("/")


_GITHUB_URL_PATTERN = re.compile(
    r"^https?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$"
)

API_BASE = "https://api.github.com"


class GitHubClient:
    """Async client for the GitHub REST API.

    Fetches repository metadata, file trees, and individual file contents.
    Works without authentication (60 req/hr) or with an optional token
    (5,000 req/hr).
    """

    def __init__(self, token: str | None = None) -> None:
        headers = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "repo-summarizer",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"

        self._http = httpx.AsyncClient(
            base_url=API_BASE,
            headers=headers,
            timeout=30.0,
        )

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._http.aclose()

    @staticmethod
    def parse_url(url: str) -> tuple[str, str]:
        """Extract (owner, repo) from a GitHub URL.

        Args:
            url: GitHub repository URL, e.g. "https://github.com/psf/requests"

        Returns:
            Tuple of (owner, repo_name).

        Raises:
            ValueError: If the URL is not a valid GitHub repository URL.
        """
        match = _GITHUB_URL_PATTERN.match(url.strip())
        if not match:
            raise ValueError(
                f"Invalid GitHub repository URL: {url!r}. "
                f"Expected format: https://github.com/owner/repo"
            )
        return match.group("owner"), match.group("repo")

    async def get_default_branch(self, owner: str, repo: str) -> str:
        """Get the default branch name for a repository.

        Raises:
            RepoNotFoundError: If the repo doesn't exist or is private.
            RateLimitError: If the API rate limit is exceeded.
            GitHubError: For other API errors.
        """
        response = await self._http.get(f"/repos/{owner}/{repo}")
        self._check_response(response)
        return response.json()["default_branch"]

    async def fetch_tree(
        self, owner: str, repo: str, max_depth: int = 3,
        max_api_calls: int = 50,
    ) -> list[TreeEntry]:
        """Fetch the file tree for a repository, limited to a given depth.

        Fetches the tree level-by-level (non-recursive) up to max_depth.
        Applies skip filtering at each level so we never expand junk
        directories (node_modules, vendor, __pycache__, etc.).

        Args:
            owner: Repository owner.
            repo: Repository name.
            max_depth: Maximum directory depth to expand (0 = root only).
            max_api_calls: Maximum number of tree API calls to make.

        Returns:
            List of TreeEntry objects (already filtered).

        Raises:
            RepoNotFoundError, RateLimitError, GitHubError
        """
        from summary.tree import should_skip
        import logging
        log = logging.getLogger(__name__)

        branch = await self.get_default_branch(owner, repo)
        api_calls = 0

        # Fetch root tree (non-recursive)
        response = await self._http.get(
            f"/repos/{owner}/{repo}/git/trees/{branch}",
        )
        self._check_response(response)
        data = response.json()
        api_calls += 1

        all_entries: list[TreeEntry] = []
        dirs_to_expand: list[tuple[str, str, int]] = []

        for item in data.get("tree", []):
            try:
                entry = TreeEntry(
                    path=item["path"],
                    type=FileType(item["type"]),
                    size=item.get("size", 0),
                )
                if should_skip(entry):
                    continue
                all_entries.append(entry)
                if entry.type == FileType.TREE and 0 < max_depth:
                    dirs_to_expand.append((item["sha"], entry.path, 1))
            except (ValueError, KeyError):
                continue

        log.debug("  📂 Tree depth 0: %d entries, %d dirs to expand",
                  len(all_entries), len(dirs_to_expand))

        # Expand subdirectories level by level
        while dirs_to_expand:
            next_level: list[tuple[str, str, int]] = []

            for sha, parent_path, depth in dirs_to_expand:
                if api_calls >= max_api_calls:
                    log.debug("  📂 API call cap reached (%d), stopping expansion",
                              max_api_calls)
                    break

                try:
                    response = await self._http.get(
                        f"/repos/{owner}/{repo}/git/trees/{sha}",
                    )
                    self._check_response(response)
                    api_calls += 1
                    sub_data = response.json()

                    for item in sub_data.get("tree", []):
                        try:
                            full_path = f"{parent_path}/{item['path']}"
                            entry = TreeEntry(
                                path=full_path,
                                type=FileType(item["type"]),
                                size=item.get("size", 0),
                            )
                            if should_skip(entry):
                                continue
                            all_entries.append(entry)
                            if (entry.type == FileType.TREE
                                    and depth < max_depth):
                                next_level.append(
                                    (item["sha"], full_path, depth + 1)
                                )
                        except (ValueError, KeyError):
                            continue
                except Exception as e:
                    log.warning("  ⚠️  Failed to fetch tree %s: %s",
                                parent_path, e)

            if api_calls >= max_api_calls:
                break

            log.debug("  📂 Tree depth %d: %d total entries, %d dirs next (%d API calls)",
                      depth, len(all_entries), len(next_level), api_calls)
            dirs_to_expand = next_level

        log.debug("  📂 Tree complete: %d entries, %d API calls", len(all_entries), api_calls)
        return all_entries

    async def fetch_file(self, owner: str, repo: str, path: str) -> str:
        """Fetch the content of a single file from the repository.

        Uses the Contents API which returns base64-encoded content.

        Args:
            owner: Repository owner.
            repo: Repository name.
            path: File path relative to repo root.

        Returns:
            The file content as a UTF-8 string.

        Raises:
            RepoNotFoundError, RateLimitError, GitHubError
        """
        response = await self._http.get(
            f"/repos/{owner}/{repo}/contents/{path}",
        )
        self._check_response(response)

        data = response.json()
        if data.get("encoding") == "base64":
            content_bytes = base64.b64decode(data["content"])
            try:
                return content_bytes.decode("utf-8")
            except UnicodeDecodeError:
                return "[binary file — content not displayable]"

        # Fallback: some files may have content directly
        return data.get("content", "")

    def _check_response(self, response: httpx.Response) -> None:
        """Check for common GitHub API error responses.

        Raises:
            RepoNotFoundError: 404 responses.
            RateLimitError: 403/429 rate limit responses.
            GitHubError: Any other non-2xx response.
        """
        if response.status_code == 404:
            raise RepoNotFoundError(
                "Repository not found. Check the URL and ensure the repo "
                "is public."
            )

        if response.status_code in (403, 429):
            remaining = response.headers.get("X-RateLimit-Remaining", "?")
            reset = response.headers.get("X-RateLimit-Reset", "?")
            raise RateLimitError(
                f"GitHub API rate limit exceeded. "
                f"Remaining: {remaining}, resets at: {reset}. "
                f"Set GITHUB_TOKEN for higher limits."
            )

        if response.status_code >= 400:
            raise GitHubError(
                f"GitHub API error {response.status_code}: "
                f"{response.text[:200]}"
            )
