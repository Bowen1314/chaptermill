"""Workflows. Deterministic orchestration only; all I/O happens in activities.

CatalogWorkflow  - one per back-catalog run. Fetches the feed, then runs one
                   EpisodeWorkflow per episode (N at a time) behind a budget
                   gate that waits, durably, for a raise_budget signal.
EpisodeWorkflow  - download -> transcribe in 5-minute chunks (one activity per
                   chunk, so a crash loses at most one chunk) -> merge -> the
                   show-notes agent loop (one activity per LLM turn and per
                   tool call) -> write files.
"""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import timedelta
from typing import Any, Optional

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError, ChildWorkflowError

with workflow.unsafe.imports_passed_through():
    from . import agent
    from .activities import Activities
    from .config import TASK_QUEUE_CPU, TASK_QUEUE_IO
    from .notes import parse_args
    from .payloads import (
        CatalogInput,
        ChunkInput,
        ChunkResult,
        EpisodeInput,
        EpisodeRef,
        EpisodeResult,
        LlmRequest,
        MergeInput,
        ReadArgs,
        SearchArgs,
        ValidateArgs,
        WriteInput,
    )
    from .transcript import chunk_bounds

DOWNLOAD_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=2), backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=1), maximum_attempts=8,
)
CPU_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=5), backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=2), maximum_attempts=6,
)
LLM_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=3), backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=2), maximum_attempts=8,
    non_retryable_error_types=["HardCap", "Config"],
)
TOOL_RETRY = RetryPolicy(initial_interval=timedelta(seconds=1), backoff_coefficient=2.0, maximum_attempts=6)
CAP_WAIT = timedelta(hours=24)
# A busy 2-core box with a slow disk can take more than the default 10 s to
# replay a workflow after its cache is evicted; give workflow tasks a minute.
WORKFLOW_TASK_TIMEOUT = timedelta(seconds=60)


@workflow.defn
class EpisodeWorkflow:
    def __init__(self) -> None:
        self._stage = "queued"
        self._chunks_done = 0
        self._chunks_total = 0
        self._turns = 0
        self._tool_calls = 0
        self._rejections = 0
        self._usd = 0.0
        self._cap = 0.0

    @workflow.query
    def progress(self) -> dict[str, Any]:
        return {
            "stage": self._stage,
            "chunks": f"{self._chunks_done}/{self._chunks_total}",
            "turns": self._turns,
            "tool_calls": self._tool_calls,
            "rejections": self._rejections,
            "usd": round(self._usd, 6),
            "cap_usd": self._cap,
        }

    @workflow.signal
    def raise_cap(self, usd: float) -> None:
        self._cap = max(self._cap, float(usd))

    async def _wait_for_cap(self) -> None:
        if self._usd < self._cap:
            return
        previous = self._stage
        self._stage = "waiting for raise_cap signal"
        try:
            await workflow.wait_condition(lambda: self._usd < self._cap, timeout=CAP_WAIT)
        except asyncio.TimeoutError:
            raise ApplicationError(f"episode spend cap ${self._cap:.4f} reached and not raised", non_retryable=True)
        self._stage = previous

    @workflow.run
    async def run(self, inp: EpisodeInput) -> EpisodeResult:
        ep = inp.episode
        self._cap = inp.cap_usd

        self._stage = "downloading"
        dl = await workflow.execute_activity_method(
            Activities.download_audio, ep, task_queue=TASK_QUEUE_IO,
            start_to_close_timeout=timedelta(minutes=15), heartbeat_timeout=timedelta(seconds=90),
            retry_policy=DOWNLOAD_RETRY,
        )
        probe = await workflow.execute_activity_method(
            Activities.probe_audio, dl.path, task_queue=TASK_QUEUE_IO,
            start_to_close_timeout=timedelta(minutes=1), retry_policy=TOOL_RETRY,
        )
        duration = probe.duration_s

        bounds = chunk_bounds(duration, inp.chunk_seconds)
        self._chunks_total = len(bounds)
        self._stage = "transcribing"
        chunks: list[ChunkResult] = []
        for i, (start, length) in enumerate(bounds):
            r = await workflow.execute_activity_method(
                Activities.transcribe_chunk, ChunkInput(ep.key, dl.path, i, start, length),
                task_queue=TASK_QUEUE_CPU,
                start_to_close_timeout=timedelta(minutes=30), heartbeat_timeout=timedelta(seconds=60),
                retry_policy=CPU_RETRY,
            )
            chunks.append(r)
            self._chunks_done = i + 1

        self._stage = "merging"
        merged = await workflow.execute_activity_method(
            Activities.merge_transcript, MergeInput(ep.key, [c.path for c in chunks], duration),
            task_queue=TASK_QUEUE_IO, start_to_close_timeout=timedelta(minutes=2), retry_policy=TOOL_RETRY,
        )

        self._stage = "writing notes"
        messages = agent.initial_messages(ep.title, ep.author, ep.summary, duration, merged.outline)
        final: Optional[dict[str, Any]] = None
        prompt_tokens = completion_tokens = cached_turns = corrections = 0
        list_usd = 0.0
        for turn in range(agent.MAX_TURNS):
            left = agent.MAX_TURNS - turn
            if left == agent.WARN_AT_TURNS_LEFT and final is None:
                messages.append({"role": "user", "content": agent.turns_left_note(left)})
            await self._wait_for_cap()
            resp = await workflow.execute_activity_method(
                Activities.llm_turn,
                LlmRequest(model=inp.model, messages=messages, tools=agent.TOOLS, workflow_id=workflow.info().workflow_id),
                task_queue=TASK_QUEUE_IO,
                start_to_close_timeout=timedelta(minutes=3), heartbeat_timeout=timedelta(seconds=30),
                retry_policy=LLM_RETRY,
            )
            self._turns += 1
            self._usd += resp.usd
            list_usd += resp.list_usd
            prompt_tokens += resp.prompt_tokens
            completion_tokens += resp.completion_tokens
            cached_turns += 1 if resp.cached else 0
            messages.append(resp.message)
            calls = resp.message.get("tool_calls") or []
            if not calls:
                messages.append({"role": "user", "content": agent.NUDGE})
                continue
            for call in calls:
                self._tool_calls += 1
                name = call["function"]["name"]
                args = parse_args(call["function"].get("arguments"))
                if not isinstance(args, dict):
                    args = {}
                if final is not None:
                    out = "Already accepted."
                elif name == "read_transcript":
                    out = await workflow.execute_activity_method(
                        Activities.read_transcript, ReadArgs(merged.transcript_path, args.get("start"), args.get("end")),
                        task_queue=TASK_QUEUE_IO, start_to_close_timeout=timedelta(seconds=30), retry_policy=TOOL_RETRY,
                    )
                elif name == "search_transcript":
                    out = await workflow.execute_activity_method(
                        Activities.search_transcript, SearchArgs(merged.transcript_path, str(args.get("query", ""))),
                        task_queue=TASK_QUEUE_IO, start_to_close_timeout=timedelta(seconds=30), retry_policy=TOOL_RETRY,
                    )
                elif name == "submit_notes":
                    v = await workflow.execute_activity_method(
                        Activities.validate_notes,
                        ValidateArgs(merged.transcript_path, call["function"].get("arguments") or "{}", duration),
                        task_queue=TASK_QUEUE_IO, start_to_close_timeout=timedelta(seconds=30), retry_policy=TOOL_RETRY,
                    )
                    if v.ok:
                        final = v.clean
                        corrections = v.corrections
                        out = "Accepted."
                    else:
                        self._rejections += 1
                        out = "Rejected. Fix these and call submit_notes again:\n- " + "\n- ".join(v.errors)
                else:
                    out = f"Unknown tool {name!r}. Available: read_transcript, search_transcript, submit_notes."
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": out})
            if final is not None:
                break
        if final is None:
            raise ApplicationError(f"agent gave no valid notes in {agent.MAX_TURNS} turns", non_retryable=True)

        stats = {
            "duration_s": duration,
            "words": merged.words,
            "chunks": len(chunks),
            "chunk_cache_hits": sum(1 for c in chunks if c.cached),
            "processing_s": round(sum(c.processing_s for c in chunks), 1),
            "whisper_model": chunks[0].model if chunks else "",
            "turns": self._turns,
            "tool_calls": self._tool_calls,
            "rejections": self._rejections,
            "timestamp_corrections": corrections,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "usd_spent": round(self._usd, 6),
            "usd_list": round(list_usd, 6),
            "llm_cached_turns": cached_turns,
            "workflow_id": workflow.info().workflow_id,
        }
        self._stage = "saving"
        out_dir = await workflow.execute_activity_method(
            Activities.write_outputs, WriteInput(ep, merged.transcript_path, final, stats),
            task_queue=TASK_QUEUE_IO, start_to_close_timeout=timedelta(minutes=1), retry_policy=TOOL_RETRY,
        )
        self._stage = "done"
        return EpisodeResult(
            key=ep.key, title=ep.title, status="done", duration_s=duration, words=merged.words,
            chunks=len(chunks), chunk_cache_hits=stats["chunk_cache_hits"], processing_s=stats["processing_s"],
            turns=self._turns, tool_calls=self._tool_calls, rejections=self._rejections,
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens, usd=round(self._usd, 6),
            llm_cached_turns=cached_turns, out_dir=out_dir,
        )


@workflow.defn
class CatalogWorkflow:
    def __init__(self) -> None:
        self._budget = 0.0
        self._spent = 0.0
        self._reserved = 0.0
        self._paused = False
        self._episodes: dict[str, dict[str, Any]] = {}

    @workflow.query
    def progress(self) -> dict[str, Any]:
        return {
            "budget_usd": self._budget,
            "spent_usd": round(self._spent, 6),
            "reserved_usd": round(self._reserved, 6),
            "paused": self._paused,
            "episodes": self._episodes,
        }

    @workflow.signal
    def raise_budget(self, usd: float) -> None:
        self._budget = max(self._budget, float(usd))

    @workflow.signal
    def pause(self) -> None:
        self._paused = True

    @workflow.signal
    def resume(self) -> None:
        self._paused = False

    def _can_start(self, cap: float) -> bool:
        return not self._paused and self._spent + self._reserved + cap <= self._budget + 1e-9

    async def _one(self, ep: EpisodeRef, inp: CatalogInput, sem: asyncio.Semaphore) -> EpisodeResult:
        async with sem:
            cap = inp.cap_usd_per_episode
            if not self._can_start(cap):
                self._episodes[ep.key]["status"] = "waiting for budget (signal raise_budget)"
            # Loop: several waiters can wake in the same pass; only the first one
            # to reserve gets through, the rest re-check and keep waiting.
            while not self._can_start(cap):
                await workflow.wait_condition(lambda: self._can_start(cap))
            self._reserved += cap
            self._episodes[ep.key]["status"] = "running"
            try:
                result = await workflow.execute_child_workflow(
                    EpisodeWorkflow.run,
                    EpisodeInput(ep, cap, inp.model),
                    id=f"{workflow.info().workflow_id}--{ep.key}",
                    task_queue=TASK_QUEUE_IO,
                    task_timeout=WORKFLOW_TASK_TIMEOUT,
                )
            except ChildWorkflowError as e:
                cause = e.cause
                while cause is not None and getattr(cause, "cause", None) is not None:
                    cause = cause.cause
                result = EpisodeResult(ep.key, ep.title, "failed", error=str(cause or e)[:300])
            finally:
                self._reserved -= cap
            self._spent += result.usd
            self._episodes[ep.key].update(status=result.status, usd=result.usd)
            return result

    @workflow.run
    async def run(self, inp: CatalogInput) -> dict[str, Any]:
        self._budget = inp.budget_usd
        episodes = await workflow.execute_activity_method(
            Activities.fetch_feed, inp, task_queue=TASK_QUEUE_IO,
            start_to_close_timeout=timedelta(seconds=45), retry_policy=DOWNLOAD_RETRY,
        )
        for e in episodes:
            self._episodes[e.key] = {"title": e.title, "status": "queued"}
        sem = asyncio.Semaphore(max(1, inp.concurrency))
        results = await asyncio.gather(*(self._one(e, inp, sem) for e in episodes))
        return {
            "budget_usd": self._budget,
            "spent_usd": round(self._spent, 6),
            "episodes": [dataclasses.asdict(r) for r in results],
        }
