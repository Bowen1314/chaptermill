"""Settings. Everything comes from CHAPTERMILL_* environment variables.

The LLM key is read only from CHAPTERMILL_LLM_API_KEY. Generic names such as
OPENAI_API_KEY are deliberately ignored so a key meant for something else is
never picked up by accident.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

DEFAULT_BASE_URL = "https://api.tokenfactory.nebius.com/v1"
DEFAULT_MODEL = "Qwen/Qwen3-235B-A22B-Instruct-2507"

# USD per token (prompt, completion). Read from Nebius Token Factory
# GET /v1/models?verbose=true on 2026-10-15. Unknown models are billed at 0 in
# our own ledger and flagged, so check your provider's page before trusting it.
PRICES: dict[str, tuple[float, float]] = {
    "Qwen/Qwen3-235B-A22B-Instruct-2507": (0.20e-6, 0.60e-6),
    "openai/gpt-oss-120b": (0.15e-6, 0.60e-6),
    "Qwen/Qwen3-30B-A3B-Instruct-2507": (0.10e-6, 0.30e-6),
}

TASK_QUEUE_IO = "chaptermill-io"
TASK_QUEUE_CPU = "chaptermill-cpu"


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    llm_base_url: str
    llm_model: str
    llm_api_key: str
    whisper_model: str
    whisper_threads: int
    temporal_address: str
    hard_cap_usd: float
    faults: str
    models_dir: Optional[Path] = None

    @property
    def whisper_dir(self) -> Path:
        return self.models_dir or self.data_dir / "models"

    @property
    def episodes_dir(self) -> Path:
        return self.data_dir / "episodes"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def ledger_path(self) -> Path:
        return self.data_dir / "ledger.jsonl"


def load_settings() -> Settings:
    env = os.environ
    return Settings(
        data_dir=Path(env.get("CHAPTERMILL_DATA", "./data")).resolve(),
        llm_base_url=env.get("CHAPTERMILL_LLM_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
        llm_model=env.get("CHAPTERMILL_LLM_MODEL", DEFAULT_MODEL),
        llm_api_key=env.get("CHAPTERMILL_LLM_API_KEY", ""),
        whisper_model=env.get("CHAPTERMILL_WHISPER_MODEL", "base.en"),
        whisper_threads=int(env.get("CHAPTERMILL_WHISPER_THREADS", "2")),
        temporal_address=env.get("CHAPTERMILL_TEMPORAL", "localhost:7233"),
        hard_cap_usd=float(env.get("CHAPTERMILL_HARD_CAP_USD", "1.00")),
        faults=env.get("CHAPTERMILL_FAULTS", ""),
        models_dir=Path(env["CHAPTERMILL_MODELS"]).resolve() if env.get("CHAPTERMILL_MODELS") else None,
    )


def price_for(model: str) -> tuple[float, float]:
    return PRICES.get(model, (0.0, 0.0))
