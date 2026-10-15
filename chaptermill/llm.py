"""OpenAI-compatible chat completions over plain httpx.

Follows Temporal's AI cookbook advice: the HTTP client does no retries of its
own; this module classifies failures so Temporal's retry policy decides.
  * 408, 409, 429 and 5xx, timeouts, connection errors -> retryable
    (Retry-After is passed on as next_retry_delay when it is 0-60 s)
  * other 4xx -> non-retryable (a bad key or a bad request will not fix itself)
"""

from __future__ import annotations

import email.utils
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

import httpx

from .config import price_for


@dataclass
class HttpVerdict:
    retryable: bool
    delay_s: Optional[float]


def parse_retry_after(headers: Mapping[str, str]) -> Optional[float]:
    lower = {k.lower(): v for k, v in headers.items()}
    if "retry-after-ms" in lower:
        try:
            return float(lower["retry-after-ms"]) / 1000.0
        except ValueError:
            pass
    value = lower.get("retry-after")
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return None


def classify(status: int, headers: Mapping[str, str]) -> HttpVerdict:
    lower = {k.lower(): v for k, v in headers.items()}
    flag = lower.get("x-should-retry", "").lower()
    if flag in ("true", "false"):
        retryable = flag == "true"
    else:
        retryable = status in (408, 409, 429) or status >= 500
    delay = parse_retry_after(headers) if retryable else None
    if delay is not None and not 0 < delay <= 60:
        delay = None
    return HttpVerdict(retryable, delay)


class LlmHttpError(Exception):
    def __init__(self, status: int, verdict: HttpVerdict, body: str):
        super().__init__(f"LLM HTTP {status}: {body[:300]}")
        self.status = status
        self.verdict = verdict


def cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    p_in, p_out = price_for(model)
    return prompt_tokens * p_in + completion_tokens * p_out


async def chat_completion(
    base_url: str,
    api_key: str,
    payload: dict[str, Any],
    timeout_s: float = 120.0,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> tuple[dict[str, Any], float]:
    """One HTTP call. Returns (response json, latency seconds). Raises LlmHttpError
    for HTTP errors; httpx exceptions (timeouts, resets) propagate as-is."""
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    t0 = time.monotonic()
    async with httpx.AsyncClient(timeout=timeout_s, transport=transport) as client:
        resp = await client.post(f"{base_url}/chat/completions", json=payload, headers=headers)
    latency = time.monotonic() - t0
    if resp.status_code >= 400:
        raise LlmHttpError(resp.status_code, classify(resp.status_code, resp.headers), resp.text)
    return resp.json(), latency


def assistant_message(response: dict[str, Any]) -> dict[str, Any]:
    """Keep only the fields we send back on the next turn."""
    msg = response["choices"][0]["message"]
    out: dict[str, Any] = {"role": "assistant", "content": msg.get("content") or ""}
    calls = msg.get("tool_calls") or []
    if calls:
        out["tool_calls"] = [
            {
                "id": c.get("id") or f"call_{i}",
                "type": "function",
                "function": {
                    "name": c["function"]["name"],
                    "arguments": c["function"].get("arguments") or "{}",
                },
            }
            for i, c in enumerate(calls)
        ]
    return out
