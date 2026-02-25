"""Google GenAI (Gemini) LLM client."""

from __future__ import annotations

import os

from summary.llm import LLMClient, TokenUsage


class GoogleLLMClient(LLMClient):
    """LLM client for Google GenAI (Gemini models)."""

    DEFAULT_MODEL = "gemini-2.5-flash"

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
    ) -> None:
        super().__init__()
        from google import genai

        self._model = model or self.DEFAULT_MODEL
        self._client = genai.Client(
            api_key=api_key or os.environ.get("GOOGLE_API_KEY", ""),
        )

    async def complete(self, system: str, user: str) -> tuple[str, TokenUsage]:
        from google.genai import types

        response = await self._client.aio.models.generate_content(
            model=self._model,
            contents=user,
            config=types.GenerateContentConfig(
                system_instruction=system,
                temperature=0.2,
            ),
        )
        usage = TokenUsage()
        if response.usage_metadata:
            usage = TokenUsage(
                input_tokens=response.usage_metadata.prompt_token_count or 0,
                output_tokens=response.usage_metadata.candidates_token_count or 0,
                total_tokens=response.usage_metadata.total_token_count or 0,
            )
        return response.text or "", usage
