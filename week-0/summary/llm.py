"""LLM client base class and factory.

Provides the abstract LLMClient interface, shared utilities (token counting,
JSON parsing), and a factory function that auto-detects the provider.

Provider implementations live in their own modules:
  - summary.nebius  → NebiusLLMClient
  - summary.gemini  → GoogleLLMClient
"""

from __future__ import annotations

import json
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

import tiktoken

log = logging.getLogger(__name__)


# ── Token counting ───────────────────────────────────────────────────────

# Use cl100k_base (GPT-4 tokenizer) as a reasonable approximation
# for all models. Not exact, but within ~15% for budget estimation.
_ENCODER = tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    """Estimate token count for a text string."""
    return len(_ENCODER.encode(text))


# ── Prompt loading ───────────────────────────────────────────────────────

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


def _load_prompt(name: str) -> str:
    """Load a prompt template from the prompts/ directory."""
    path = _PROMPTS_DIR / name
    return path.read_text().strip()


# ── Response models ──────────────────────────────────────────────────────

@dataclass
class TokenUsage:
    """Token usage from a single LLM call."""
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
        )


@dataclass
class SummaryResult:
    """Structured result from the summarization LLM call."""
    summary: str
    technologies: list[str]
    structure: str


# ── Abstract base ────────────────────────────────────────────────────────

class LLMClient(ABC):
    """Abstract interface for LLM providers."""

    # Default context budget (tokens for repo content).
    # Override in subclasses based on model limits.
    MAX_CONTEXT_TOKENS = 80_000
    MAX_FILE_TOKENS = 10_000

    def __init__(self) -> None:
        self.total_usage = TokenUsage()

    @abstractmethod
    async def complete(self, system: str, user: str) -> tuple[str, TokenUsage]:
        """Send a chat completion request and return (response_text, usage)."""

    async def pick_files(self, tree_text: str, token_budget: int) -> list[str]:
        """Ask the LLM which files to read based on the directory tree.

        Args:
            tree_text: Formatted directory tree string.
            token_budget: Approximate remaining token budget for file contents.

        Returns:
            List of file paths the LLM wants to read.
        """
        byte_budget = token_budget * 4  # rough tokens-to-bytes

        template = _load_prompt("file_picker.md")
        system = template.format(
            token_budget=f"{token_budget:,}",
            byte_budget=f"{byte_budget:,}",
        )

        user = f"Here is the directory tree with file sizes:\n\n{tree_text}"

        response, usage = await self.complete(system, user)
        self.total_usage = self.total_usage + usage
        log.debug("  file_picker raw response: %s", response[:300])
        log.debug("  file_picker usage: in=%d out=%d",
                  usage.input_tokens, usage.output_tokens)
        return self._parse_file_list(response)

    async def summarize(self, context: str) -> SummaryResult:
        """Generate a structured summary of a repository.

        Args:
            context: The assembled context (tree, README, file contents).

        Returns:
            SummaryResult with summary, technologies, and structure.
        """
        system = _load_prompt("summarizer.md")

        response, usage = await self.complete(system, context)
        self.total_usage = self.total_usage + usage
        log.debug("  summarizer raw response: %s", response[:500])
        log.debug("  summarizer usage: in=%d out=%d",
                  usage.input_tokens, usage.output_tokens)
        return self._parse_summary(response)

    @staticmethod
    def _parse_file_list(response: str) -> list[str]:
        """Extract a JSON list of file paths from the LLM response."""
        text = response.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:])
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()

        try:
            result = json.loads(text)
            if isinstance(result, list):
                return [str(p) for p in result if isinstance(p, str)]
        except json.JSONDecodeError:
            pass

        # Fallback: try to find a JSON array in the response
        start = text.find("[")
        end = text.rfind("]")
        if start != -1 and end != -1:
            try:
                result = json.loads(text[start:end + 1])
                if isinstance(result, list):
                    return [str(p) for p in result if isinstance(p, str)]
            except json.JSONDecodeError:
                pass

        return []

    @staticmethod
    def _parse_summary(response: str) -> SummaryResult:
        """Parse a JSON summary response from the LLM."""
        text = response.strip()

        # Strip markdown code fences (may have preamble text before them)
        import re
        # Try closed fence first, then unclosed (truncated output)
        fence_match = re.search(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL)
        if not fence_match:
            fence_match = re.search(r"```(?:json)?\s*\n(.*)", text, re.DOTALL)
        if fence_match:
            text = fence_match.group(1).strip()

        # Try direct JSON parse
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # Fallback: find JSON object in the text
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end != -1 and end > start:
                try:
                    data = json.loads(text[start:end + 1])
                except json.JSONDecodeError:
                    # JSON may be truncated — try to repair
                    fragment = text[start:]
                    # Close any open strings and braces
                    if fragment.count('"') % 2 == 1:
                        fragment += '"'
                    while fragment.count("{") > fragment.count("}"):
                        fragment += "}"
                    try:
                        data = json.loads(fragment)
                    except json.JSONDecodeError:
                        raise ValueError(
                            f"Could not parse LLM response as JSON: {text[:200]}"
                        )
            else:
                raise ValueError(
                    f"Could not parse LLM response as JSON: {text[:200]}"
                )

        return SummaryResult(
            summary=data.get("summary", ""),
            technologies=data.get("technologies", []),
            structure=data.get("structure", ""),
        )

    async def close(self) -> None:
        """Clean up resources. Override in subclasses if needed."""


# ── Factory ──────────────────────────────────────────────────────────────

def create_llm_client(
    provider: str | None = None,
    **kwargs,
) -> LLMClient:
    """Create an LLM client, auto-detecting the provider from environment.

    Priority:
      1. Explicit provider argument ("nebius" or "google")
      2. LLM_PROVIDER env var
      3. NEBIUS_API_KEY is set → Nebius
      4. GOOGLE_API_KEY is set → Google
    """
    if provider is None:
        provider = os.environ.get("LLM_PROVIDER", "").strip() or None

    if provider is None:
        if os.environ.get("NEBIUS_API_KEY", "").strip():
            provider = "nebius"
        elif os.environ.get("GOOGLE_API_KEY", "").strip():
            provider = "google"
        else:
            raise ValueError(
                "No LLM API key found. Set NEBIUS_API_KEY or GOOGLE_API_KEY."
            )

    if provider == "nebius":
        from summary.nebius import NebiusLLMClient
        return NebiusLLMClient(**kwargs)
    elif provider == "google":
        from summary.gemini import GoogleLLMClient
        return GoogleLLMClient(**kwargs)
    else:
        raise ValueError(f"Unknown LLM provider: {provider!r}")
