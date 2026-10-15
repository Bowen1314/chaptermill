"""Payloads passed between workflows and activities.

Large things (audio, transcripts) stay on disk and only their paths travel
through Temporal (the claim-check pattern), so event history stays small.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class EpisodeRef:
    key: str  # stable id, e.g. "hpr4746"
    title: str
    audio_url: str
    page_url: str = ""
    published: str = ""
    author: str = ""
    summary: str = ""
    license: str = ""


@dataclass
class CatalogInput:
    feed_url: str
    only: list[str] = field(default_factory=list)  # episode keys; empty = newest `limit`
    limit: int = 5
    budget_usd: float = 0.25  # whole run
    cap_usd_per_episode: float = 0.05
    concurrency: int = 2
    model: str = ""  # empty = worker default


@dataclass
class EpisodeInput:
    episode: EpisodeRef
    cap_usd: float = 0.05
    model: str = ""
    chunk_seconds: int = 300


@dataclass
class DownloadResult:
    path: str
    bytes: int
    sha256: str
    cached: bool


@dataclass
class ProbeResult:
    duration_s: float


@dataclass
class ChunkInput:
    episode_key: str
    audio_path: str
    index: int
    start_s: float
    duration_s: float


@dataclass
class ChunkResult:
    path: str
    index: int
    segments: int
    audio_s: float
    processing_s: float
    cached: bool
    model: str


@dataclass
class MergeInput:
    episode_key: str
    chunk_paths: list[str]
    duration_s: float


@dataclass
class MergeResult:
    transcript_path: str
    segments: int
    words: int
    outline: str


@dataclass
class LlmRequest:
    model: str
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    temperature: float = 0.2
    max_tokens: int = 1800
    workflow_id: str = ""


@dataclass
class LlmResponse:
    message: dict[str, Any]
    prompt_tokens: int
    completion_tokens: int
    usd: float
    cached: bool
    latency_s: float
    list_usd: float = 0.0  # what these tokens cost when first bought (non-zero even on a cache hit)


@dataclass
class ReadArgs:
    transcript_path: str
    start: Any = None
    end: Any = None


@dataclass
class SearchArgs:
    transcript_path: str
    query: str


@dataclass
class ValidateArgs:
    transcript_path: str
    submission_json: str
    duration_s: float


@dataclass
class ValidateResult:
    ok: bool
    errors: list[str]
    clean: Optional[dict[str, Any]] = None
    corrections: int = 0


@dataclass
class WriteInput:
    episode: EpisodeRef
    transcript_path: str
    notes: dict[str, Any]
    stats: dict[str, Any]


@dataclass
class EpisodeResult:
    key: str
    title: str
    status: str
    duration_s: float = 0.0
    words: int = 0
    chunks: int = 0
    chunk_cache_hits: int = 0
    processing_s: float = 0.0
    turns: int = 0
    tool_calls: int = 0
    rejections: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    usd: float = 0.0
    llm_cached_turns: int = 0
    out_dir: str = ""
    error: str = ""
