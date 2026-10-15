"""Temporal activities: everything that touches the network, the disk or the CPU.

Each activity is written to be safe to run more than once (Temporal guarantees
at-least-once execution): downloads and transcript chunks are skipped when
their output already exists, and LLM completions are cached by request hash.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import httpx
from temporalio import activity
from temporalio.exceptions import ApplicationError

from . import faults
from . import notes as notes_mod
from . import transcript as tx
from .config import Settings
from .feed import parse_feed, select
from .llm import LlmHttpError, assistant_message, chat_completion, cost_usd
from .payloads import (
    CatalogInput,
    ChunkInput,
    ChunkResult,
    DownloadResult,
    EpisodeRef,
    LlmRequest,
    LlmResponse,
    MergeInput,
    MergeResult,
    ProbeResult,
    ReadArgs,
    SearchArgs,
    ValidateArgs,
    ValidateResult,
    WriteInput,
)
from .store import JsonCache, Ledger, atomic_write_json, atomic_write_text, read_json, request_key

USER_AGENT = "chaptermill/0.1 (+https://github.com/Bowen1314/chaptermill)"
MAX_WINDOW_S = 900.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _act_id() -> tuple[str, int]:
    info = activity.info()
    return f"{info.workflow_id}/{info.activity_id}", info.attempt


class _heartbeating:
    """Heartbeat every few seconds while awaiting a slow call, so that if this
    worker dies Temporal notices within heartbeat_timeout (not start_to_close)."""

    def __init__(self, details: Any, every_s: float = 5.0):
        self.details = details
        self.every_s = every_s
        self._task: Optional[asyncio.Task[None]] = None

    async def _beat(self) -> None:
        while True:
            activity.heartbeat(self.details)
            await asyncio.sleep(self.every_s)

    async def __aenter__(self) -> "_heartbeating":
        self._task = asyncio.create_task(self._beat())
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass


class _beating_thread:
    """Sync-activity counterpart of _heartbeating: a side thread heartbeats every
    few seconds while the activity thread is stuck in one long call (model load,
    ffmpeg decode, the first 30 s window) that cannot heartbeat by itself."""

    def __init__(self, details: dict, every_s: float = 10.0):
        self.details = details  # mutated by the caller to report progress
        self.every_s = every_s
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _run(self) -> None:
        while True:
            try:
                activity.heartbeat(dict(self.details))
            except Exception:  # noqa: BLE001 - activity finished or worker shutting down
                return
            if self._stop.wait(self.every_s):
                return

    def __enter__(self) -> "_beating_thread":
        ctx = contextvars.copy_context()  # activity.heartbeat needs the activity context
        self._thread = threading.Thread(target=ctx.run, args=(self._run,), daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)


_model_lock = threading.Lock()
_model: Any = None
_model_name: Optional[str] = None


def _whisper(settings: Settings) -> Any:
    global _model, _model_name
    with _model_lock:
        if _model is None or _model_name != settings.whisper_model:
            from faster_whisper import WhisperModel  # heavy import, CPU worker only

            _model = WhisperModel(
                settings.whisper_model,
                device="cpu",
                compute_type="int8",
                cpu_threads=settings.whisper_threads,
                download_root=str(settings.whisper_dir),
            )
            _model_name = settings.whisper_model
        return _model


def decode_pcm(path: str, start_s: float, duration_s: float) -> Any:
    import numpy as np

    cmd = [
        "ffmpeg", "-nostdin", "-v", "error",
        "-ss", f"{start_s:.3f}", "-t", f"{duration_s:.3f}", "-i", path,
        "-f", "s16le", "-ac", "1", "-ar", "16000", "-",
    ]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0


class Activities:
    def __init__(self, settings: Settings):
        self.s = settings
        self.cache = JsonCache(settings.cache_dir / "llm")
        self.ledger = Ledger(settings.ledger_path)
        self._transcripts: dict[str, tuple[float, list[dict[str, Any]]]] = {}

    # ---------- helpers ----------
    def _ep_dir(self, key: str) -> Path:
        d = self.s.episodes_dir / key
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _segments(self, transcript_path: str) -> list[dict[str, Any]]:
        p = Path(transcript_path)
        mtime = p.stat().st_mtime
        hit = self._transcripts.get(transcript_path)
        if hit and hit[0] == mtime:
            return hit[1]
        segs = read_json(p)["segments"]
        self._transcripts[transcript_path] = (mtime, segs)
        return segs

    # ---------- feed ----------
    @activity.defn
    async def fetch_feed(self, inp: CatalogInput) -> list[EpisodeRef]:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as c:
            r = await c.get(inp.feed_url)
        if r.status_code >= 400:
            raise ApplicationError(f"feed HTTP {r.status_code}", non_retryable=400 <= r.status_code < 500 and r.status_code not in (408, 429))
        try:
            episodes = select(parse_feed(r.text), inp.only, inp.limit)
        except ValueError as e:
            raise ApplicationError(str(e), non_retryable=True)
        return episodes

    # ---------- audio ----------
    @activity.defn
    async def download_audio(self, ep: EpisodeRef) -> DownloadResult:
        aid, attempt = _act_id()
        ext = Path(urlparse(ep.audio_url).query.split("filename=")[-1] or urlparse(ep.audio_url).path).suffix or ".mp3"
        if ext not in (".mp3", ".ogg", ".opus", ".m4a", ".wav", ".flac"):
            ext = ".mp3"
        d = self._ep_dir(ep.key)
        dest = d / f"audio{ext}"
        meta_p = d / "audio.meta.json"
        if dest.exists() and meta_p.exists():
            meta = read_json(meta_p)
            if meta.get("url") == ep.audio_url and meta.get("bytes") == dest.stat().st_size:
                return DownloadResult(str(dest), meta["bytes"], meta["sha256"], True)
        faults.maybe_fail(self.s.faults, "download", aid, attempt, f"connection reset while downloading {ep.key}")
        tmp = dest.with_suffix(dest.suffix + ".part")
        h = hashlib.sha256()
        n = 0
        last_beat = 0
        async with httpx.AsyncClient(timeout=httpx.Timeout(60, read=120), follow_redirects=True, headers={"User-Agent": USER_AGENT}) as c:
            async with c.stream("GET", ep.audio_url) as r:
                if r.status_code >= 400:
                    raise ApplicationError(
                        f"audio HTTP {r.status_code}",
                        non_retryable=400 <= r.status_code < 500 and r.status_code not in (408, 429),
                    )
                expected = int(r.headers.get("content-length") or 0)
                with open(tmp, "wb") as f:
                    async for chunk in r.aiter_bytes(1 << 18):
                        # disk writes go to a thread: on a slow disk a blocking write
                        # would stall the worker's event loop (and its workflow tasks)
                        await asyncio.to_thread(f.write, chunk)
                        h.update(chunk)
                        n += len(chunk)
                        if n - last_beat >= 1 << 20:
                            activity.heartbeat({"bytes": n})
                            last_beat = n
        if expected and n != expected:
            raise ApplicationError(f"short download: {n} of {expected} bytes")
        tmp.replace(dest)
        await asyncio.to_thread(atomic_write_json, meta_p, {"url": ep.audio_url, "bytes": n, "sha256": h.hexdigest(), "fetched": _now()})
        return DownloadResult(str(dest), n, h.hexdigest(), False)

    @activity.defn
    async def probe_audio(self, path: str) -> ProbeResult:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            raise ApplicationError(f"ffprobe failed: {err.decode()[:200]}", non_retryable=True)
        return ProbeResult(round(float(out.decode().strip()), 2))

    @activity.defn
    def transcribe_chunk(self, inp: ChunkInput) -> ChunkResult:
        """CPU-bound; runs on the cpu task queue in a thread, one at a time."""
        out = self._ep_dir(inp.episode_key) / "chunks" / f"{inp.index:03d}.json"
        if out.exists():
            try:
                doc = read_json(out)
                if doc.get("model") == self.s.whisper_model and doc.get("start_s") == inp.start_s:
                    return ChunkResult(str(out), inp.index, len(doc["segments"]), doc["duration_s"], doc["processing_s"], True, doc["model"])
            except (OSError, ValueError, KeyError):
                pass
        t0 = time.monotonic()
        progress = {"index": inp.index, "phase": "decode", "done_s": 0.0}
        with _beating_thread(progress):
            audio = decode_pcm(inp.audio_path, inp.start_s, inp.duration_s)
            progress["phase"] = "load_model"
            model = _whisper(self.s)
            progress["phase"] = "transcribe"
            segments_iter, _info = model.transcribe(
                audio, language="en", beam_size=1, vad_filter=True, condition_on_previous_text=False,
                temperature=0.0,  # no sampling fallback: a re-run chunk gives identical text
            )
            segs = []
            for s in segments_iter:
                segs.append({"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()})
                progress["done_s"] = round(s.end, 1)
        processing = round(time.monotonic() - t0, 2)
        atomic_write_json(
            out,
            {
                "model": self.s.whisper_model,
                "start_s": inp.start_s,
                "duration_s": round(len(audio) / 16000, 2),
                "processing_s": processing,
                "segments": segs,
                "finished": _now(),
            },
        )
        return ChunkResult(str(out), inp.index, len(segs), round(len(audio) / 16000, 2), processing, False, self.s.whisper_model)

    @activity.defn
    def merge_transcript(self, inp: MergeInput) -> MergeResult:
        parts = []
        for p in inp.chunk_paths:
            doc = read_json(Path(p))
            parts.append((float(doc["start_s"]), doc["segments"]))
        segs = tx.merge_chunks(parts)
        d = self._ep_dir(inp.episode_key)
        path = d / "transcript.json"
        atomic_write_json(path, {"episode": inp.episode_key, "duration_s": inp.duration_s, "segments": segs})
        atomic_write_text(d / "transcript.srt", tx.to_srt(segs))
        atomic_write_text(d / "transcript.vtt", tx.to_vtt(segs))
        return MergeResult(str(path), len(segs), tx.word_count(segs), tx.outline(segs))

    # ---------- LLM ----------
    @activity.defn
    async def llm_turn(self, req: LlmRequest) -> LlmResponse:
        aid, attempt = _act_id()
        model = req.model or self.s.llm_model
        payload = {
            "model": model,
            "messages": req.messages,
            "tools": req.tools,
            "temperature": req.temperature,
            "max_tokens": req.max_tokens,
        }
        key = request_key(payload)
        # All disk I/O below runs in threads: fsync on a busy disk can block for
        # seconds, and this coroutine shares the event loop with workflow tasks.
        hit = await asyncio.to_thread(self.cache.get, key)
        if hit is not None:
            return LlmResponse(hit["message"], hit["prompt_tokens"], hit["completion_tokens"], 0.0, True, 0.0, float(hit.get("usd", 0.0)))

        spent = await asyncio.to_thread(self.ledger.total_usd)
        if spent >= self.s.hard_cap_usd:
            raise ApplicationError(
                f"hard spending cap reached (${spent:.4f} >= ${self.s.hard_cap_usd:.2f}); raise CHAPTERMILL_HARD_CAP_USD to continue",
                type="HardCap",
                non_retryable=True,
            )
        if not self.s.llm_api_key:
            raise ApplicationError("CHAPTERMILL_LLM_API_KEY is not set", type="Config", non_retryable=True)

        base = {"ts": _now(), "activity": aid, "attempt": attempt, "key": key[:16], "model": model}
        if faults.should_fail(self.s.faults, "llm", aid, attempt):
            await asyncio.to_thread(self.ledger.append, {**base, "status": "injected_503", "usd": 0.0})
            raise ApplicationError("[injected fault] provider returned 503", type="InjectedFault")
        # "sent" is written before the request: if this worker is killed mid-call the
        # provider may still bill it, and the ledger shows a sent with no outcome.
        await asyncio.to_thread(self.ledger.append, {**base, "status": "sent", "usd": 0.0})
        try:
            async with _heartbeating({"waiting_for": model}):
                resp, latency = await chat_completion(self.s.llm_base_url, self.s.llm_api_key, payload)
        except LlmHttpError as e:
            await asyncio.to_thread(self.ledger.append, {**base, "status": f"http_{e.status}", "usd": 0.0})
            raise ApplicationError(
                str(e),
                type=f"HTTP{e.status}",
                non_retryable=not e.verdict.retryable,
                next_retry_delay=timedelta(seconds=e.verdict.delay_s) if e.verdict.delay_s else None,
            )
        except httpx.TimeoutException as e:
            # The provider may still bill a request we gave up on; we cannot know.
            await asyncio.to_thread(self.ledger.append, {**base, "status": "timeout", "usd": 0.0})
            raise ApplicationError(f"LLM timeout: {type(e).__name__}", type="Timeout")
        except httpx.TransportError as e:
            await asyncio.to_thread(self.ledger.append, {**base, "status": "transport_error", "usd": 0.0})
            raise ApplicationError(f"LLM transport error: {type(e).__name__}", type="Transport")

        usage = resp.get("usage") or {}
        pt, ct = int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
        usd = cost_usd(model, pt, ct)
        message = assistant_message(resp)
        # Ledger first, then cache: if we crash in between, the retry pays again
        # and the ledger shows the duplicate key, so spend is never under-reported.
        await asyncio.to_thread(self.ledger.append, {**base, "status": "ok", "prompt_tokens": pt, "completion_tokens": ct, "usd": round(usd, 8), "latency_s": round(latency, 2)})
        await asyncio.to_thread(self.cache.put, key, {"message": message, "prompt_tokens": pt, "completion_tokens": ct, "usd": usd, "model": model, "ts": _now()})
        return LlmResponse(message, pt, ct, usd, False, round(latency, 2), usd)

    # ---------- agent tools ----------
    @activity.defn
    def read_transcript(self, args: ReadArgs) -> str:
        aid, attempt = _act_id()
        faults.maybe_fail(self.s.faults, "tool", aid, attempt, "transcript store unavailable")
        segs = self._segments(args.transcript_path)
        start = tx.parse_ts(args.start)
        end = tx.parse_ts(args.end)
        if start is None:
            return f"Could not read start {args.start!r}; use mm:ss."
        if end is None or end <= start:
            end = start + 600
        clipped = ""
        if end - start > MAX_WINDOW_S:
            end = start + MAX_WINDOW_S
            clipped = f"\n[window limited to {int(MAX_WINDOW_S // 60)} minutes; continue from {tx.fmt_ts(end)}]"
        return tx.window(segs, start, end) + clipped

    @activity.defn
    def search_transcript(self, args: SearchArgs) -> str:
        aid, attempt = _act_id()
        faults.maybe_fail(self.s.faults, "tool", aid, attempt, "transcript store unavailable")
        hits = tx.search(self._segments(args.transcript_path), args.query)
        if not hits:
            return "No matches."
        return "\n".join(f"[{h['ts']}] {'EXACT ' if h['exact'] else ''}{h['text']}" for h in hits)

    @activity.defn
    def validate_notes(self, args: ValidateArgs) -> ValidateResult:
        segs = self._segments(args.transcript_path)
        clean, errors, corrections = notes_mod.validate_submission(notes_mod.parse_args(args.submission_json), segs, args.duration_s)
        return ValidateResult(not errors, errors, clean, corrections)

    # ---------- outputs ----------
    @activity.defn
    def write_outputs(self, inp: WriteInput) -> str:
        ep = inp.episode
        d = self._ep_dir(ep.key)
        attribution = ""
        if ep.key.startswith("hpr"):
            who = f" by {ep.author}" if ep.author else ""
            attribution = (
                f"Source: Hacker Public Radio episode {ep.key[3:]}{who}, released under CC BY-SA 4.0 "
                f"({ep.page_url or 'https://hackerpublicradio.org'}). These notes were generated by chaptermill "
                "from that episode and are shared under the same license."
            )
        doc = {
            "episode": {
                "key": ep.key, "title": ep.title, "audio_url": ep.audio_url, "page_url": ep.page_url,
                "published": ep.published, "author": ep.author, "summary": ep.summary,
            },
            "notes": inp.notes,
            "stats": inp.stats,
            "attribution": attribution,
            "generated": _now(),
        }
        atomic_write_json(d / "notes.json", doc)
        atomic_write_json(d / "chapters.json", notes_mod.to_podcast_chapters(inp.notes, ep.title))
        atomic_write_text(d / "youtube-chapters.txt", notes_mod.to_youtube_chapters(inp.notes) + "\n")
        atomic_write_text(d / "show-notes.md", notes_mod.to_markdown(inp.notes, ep.title, ep.page_url, attribution))
        return str(d)
