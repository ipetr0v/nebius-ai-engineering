"""FastAPI application for the GitHub Repository Summarizer.

Exposes a POST /summarize endpoint that takes a GitHub repo URL
and returns an LLM-generated summary.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, field_validator

from summary.agent import RepoAnalyzer
from summary.github import (
    GitHubClient,
    GitHubError,
    RateLimitError,
    RepoNotFoundError,
)
from summary.llm import create_llm_client

DEFAULT_TOKEN_FILE = Path("~/.ssh/github_token").expanduser()


# ── Pydantic models ─────────────────────────────────────────────────────

class SummarizeRequest(BaseModel):
    github_url: str

    @field_validator("github_url")
    @classmethod
    def validate_github_url(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("github_url must not be empty")
        # Full validation happens in GitHubClient.parse_url()
        return v


class SummarizeResponse(BaseModel):
    summary: str
    technologies: list[str]
    structure: str


class ErrorResponse(BaseModel):
    status: str = "error"
    message: str


# ── Token loading ────────────────────────────────────────────────────────

def _load_github_token() -> str | None:
    """Load GitHub token from env var or default file."""
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        return token
    if DEFAULT_TOKEN_FILE.is_file():
        return DEFAULT_TOKEN_FILE.read_text().strip()
    return None


# ── App lifecycle ────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage shared resources (HTTP clients) across the app lifecycle."""
    github_token = _load_github_token()
    app.state.github = GitHubClient(token=github_token)
    app.state.llm = create_llm_client()
    app.state.analyzer = RepoAnalyzer(
        github=app.state.github,
        llm=app.state.llm,
    )
    yield
    await app.state.github.close()
    await app.state.llm.close()


app = FastAPI(
    title="GitHub Repository Summarizer",
    description="Takes a GitHub repository URL and returns an LLM-generated summary.",
    version="0.1.0",
    lifespan=lifespan,
)


# ── Endpoint ─────────────────────────────────────────────────────────────

@app.post(
    "/summarize",
    response_model=SummarizeResponse,
    responses={
        400: {"model": ErrorResponse, "description": "Invalid request"},
        404: {"model": ErrorResponse, "description": "Repository not found"},
        429: {"model": ErrorResponse, "description": "Rate limit exceeded"},
        502: {"model": ErrorResponse, "description": "LLM API error"},
    },
)
async def summarize(request: SummarizeRequest) -> SummarizeResponse:
    """Analyze a GitHub repository and return a structured summary."""
    analyzer: RepoAnalyzer = app.state.analyzer

    try:
        # Validate URL early
        GitHubClient.parse_url(request.github_url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail={
            "status": "error",
            "message": str(e),
        })

    try:
        result, stats = await analyzer.analyze(request.github_url)
        return SummarizeResponse(
            summary=result.summary,
            technologies=result.technologies,
            structure=result.structure,
        )
    except RepoNotFoundError as e:
        raise HTTPException(status_code=404, detail={
            "status": "error",
            "message": str(e),
        })
    except RateLimitError as e:
        raise HTTPException(status_code=429, detail={
            "status": "error",
            "message": str(e),
        })
    except GitHubError as e:
        raise HTTPException(status_code=502, detail={
            "status": "error",
            "message": f"GitHub API error: {e}",
        })
    except Exception as e:
        raise HTTPException(status_code=502, detail={
            "status": "error",
            "message": f"Analysis failed: {e}",
        })
