"""Nebius Token Factory LLM client (OpenAI-compatible API)."""

from __future__ import annotations

import os

from summary.llm import LLMClient, TokenUsage


class NebiusLLMClient(LLMClient):
    """LLM client for Nebius Token Factory (OpenAI-compatible API).

    Model: Llama-3.3-70B-Instruct — chosen for fast inference (120 tok/s),
    reliable structured JSON output, and no hidden <think> reasoning tokens
    (unlike Qwen3 models). See README.md for the full benchmark comparison.
    """

    DEFAULT_BASE_URL = "https://api.tokenfactory.nebius.com/v1/"
    DEFAULT_MODEL = "meta-llama/Llama-3.3-70B-Instruct"

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
    ) -> None:
        super().__init__()
        import openai

        self._model = model or self.DEFAULT_MODEL
        self._client = openai.AsyncOpenAI(
            api_key=api_key or os.environ.get("NEBIUS_API_KEY", ""),
            base_url=base_url or self.DEFAULT_BASE_URL,
        )

    async def complete(self, system: str, user: str) -> tuple[str, TokenUsage]:
        response = await self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.2,
        )
        usage = TokenUsage()
        if response.usage:
            usage = TokenUsage(
                input_tokens=response.usage.prompt_tokens or 0,
                output_tokens=response.usage.completion_tokens or 0,
                total_tokens=response.usage.total_tokens or 0,
            )
        return response.choices[0].message.content or "", usage

    async def close(self) -> None:
        await self._client.close()
