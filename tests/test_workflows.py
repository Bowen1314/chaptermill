"""Workflow tests on Temporal's time-skipping test server, with fake activities.

They check orchestration, not models: retries recover flaky activities, the
agent loop feeds validator rejections back, the budget gate waits for a signal,
a permanently failing episode does not sink the catalog, and recorded history
replays deterministically.
"""

import json
import uuid
from collections import Counter

import pytest
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from chaptermill import notes, transcript as tx
from chaptermill.config import TASK_QUEUE_CPU, TASK_QUEUE_IO
from chaptermill.payloads import (
    CatalogInput,
    ChunkInput,
    ChunkResult,
    DownloadResult,
    EpisodeInput,
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
from chaptermill import agent
from chaptermill.workflows import CatalogWorkflow, EpisodeWorkflow

SEGS = [
    {"start": 0.0, "end": 9.0, "text": "Welcome to the show, today we look at three small command line tools."},
    {"start": 80.0, "end": 95.0, "text": "The first one replaces cat and it adds syntax highlighting with almost no setup at all."},
    {"start": 320.0, "end": 335.0, "text": "My honest advice is to learn the boring default tool first and only then reach for the shiny one."},
    {"start": 600.0, "end": 612.0, "text": "That is the end of the episode, thanks for listening and please record a show."},
]
DURATION = 720.0

VALID = {
    "summary": " ".join(["Three small command line tools and when to use them."] * 4),
    "chapters": [{"start": "00:00", "title": "Intro"}, {"start": "01:20", "title": "A cat replacement"}, {"start": "05:20", "title": "Advice"}],
    "quotes": [
        {"text": "learn the boring default tool first and only then reach for the shiny one", "start": "05:21"},
        {"text": "it adds syntax highlighting with almost no setup at all", "start": "01:22"},
    ],
    "topics": ["cli", "cat", "advice"],
}
INVENTED = dict(VALID, quotes=[{"text": "the shiny tools are always better than the boring defaults, trust me", "start": "05:21"}, VALID["quotes"][1]])

STATE: dict = {}


def reset(**kw):
    STATE.clear()
    STATE.update(calls=Counter(), llm_fail_first=0, invent_first=False, download_404=set(), written=[])
    STATE.update(kw)


def tool_call(name, args, cid):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


@activity.defn(name="fetch_feed")
async def fake_fetch_feed(inp: CatalogInput) -> list[EpisodeRef]:
    keys = inp.only or ["hpr1", "hpr2"]
    return [EpisodeRef(key=k, title=f"Episode {k}", audio_url=f"https://example.org/{k}.mp3") for k in keys]


@activity.defn(name="download_audio")
async def fake_download(ep: EpisodeRef) -> DownloadResult:
    STATE["calls"]["download"] += 1
    if ep.key in STATE["download_404"]:
        raise ApplicationError("audio HTTP 404", non_retryable=True)
    return DownloadResult(f"/fake/{ep.key}.mp3", 1000, "0" * 64, False)


@activity.defn(name="probe_audio")
async def fake_probe(path: str) -> ProbeResult:
    return ProbeResult(DURATION)


@activity.defn(name="transcribe_chunk")
async def fake_transcribe(inp: ChunkInput) -> ChunkResult:
    STATE["calls"]["chunk"] += 1
    return ChunkResult(f"/fake/{inp.episode_key}/{inp.index}.json", inp.index, 1, inp.duration_s, 1.0, False, "fake")


@activity.defn(name="merge_transcript")
async def fake_merge(inp: MergeInput) -> MergeResult:
    STATE["calls"]["merge"] += 1
    return MergeResult("mem://t", len(SEGS), tx.word_count(SEGS), tx.outline(SEGS))


@activity.defn(name="llm_turn")
async def fake_llm(req: LlmRequest) -> LlmResponse:
    STATE["calls"]["llm_attempt"] += 1
    info = activity.info()
    if info.attempt <= STATE["llm_fail_first"]:
        raise ApplicationError("provider returned 503", type="HTTP503")
    assistant_turns = sum(1 for m in req.messages if m["role"] == "assistant")
    if assistant_turns == 0:
        msg = {"role": "assistant", "content": "", "tool_calls": [tool_call("read_transcript", {"start": "00:00", "end": "11:00"}, "c1")]}
    elif assistant_turns == 1 and STATE["invent_first"]:
        msg = {"role": "assistant", "content": "", "tool_calls": [tool_call("submit_notes", INVENTED, "c2")]}
    else:
        msg = {"role": "assistant", "content": "", "tool_calls": [tool_call("submit_notes", VALID, f"c{assistant_turns + 2}")]}
    return LlmResponse(msg, 100, 20, 0.001, False, 0.1, 0.001)


@activity.defn(name="read_transcript")
async def fake_read(args: ReadArgs) -> str:
    return tx.window(SEGS, tx.parse_ts(args.start), tx.parse_ts(args.end))


@activity.defn(name="search_transcript")
async def fake_search(args: SearchArgs) -> str:
    return json.dumps(tx.search(SEGS, args.query))


@activity.defn(name="validate_notes")
async def fake_validate(args: ValidateArgs) -> ValidateResult:
    clean, errors, corr = notes.validate_submission(json.loads(args.submission_json), SEGS, args.duration_s)
    return ValidateResult(not errors, errors, clean, corr)


@activity.defn(name="write_outputs")
async def fake_write(inp: WriteInput) -> str:
    STATE["written"].append(inp)
    return f"/fake/out/{inp.episode.key}"


IO_ACTS = [fake_fetch_feed, fake_download, fake_probe, fake_merge, fake_llm, fake_read, fake_search, fake_validate, fake_write]


@pytest.fixture
async def env():
    async with await WorkflowEnvironment.start_time_skipping() as e:
        yield e


def workers(env):
    io = Worker(env.client, task_queue=TASK_QUEUE_IO, workflows=[CatalogWorkflow, EpisodeWorkflow], activities=IO_ACTS)
    cpu = Worker(env.client, task_queue=TASK_QUEUE_CPU, activities=[fake_transcribe])
    return io, cpu


async def run_episode(env, **state):
    reset(**state)
    io, cpu = workers(env)
    wid = f"ep-{uuid.uuid4()}"
    async with io, cpu:
        result = await env.client.execute_workflow(
            EpisodeWorkflow.run,
            EpisodeInput(EpisodeRef("hpr9", "Episode 9", "https://example.org/9.mp3"), cap_usd=0.05),
            id=wid, task_queue=TASK_QUEUE_IO,
        )
        history = await env.client.get_workflow_handle(wid).fetch_history()
    return result, history


async def test_happy_path(env):
    result, _ = await run_episode(env)
    assert result.status == "done"
    assert result.chunks == 3 and STATE["calls"]["chunk"] == 3
    assert result.turns == 2 and result.rejections == 0
    notes_out = STATE["written"][0].notes
    assert [c["start"] for c in notes_out["chapters"]] == [0.0, 80.0, 320.0]
    assert 320 <= notes_out["quotes"][0]["start"] <= 335


async def test_flaky_llm_is_retried_by_temporal(env):
    result, _ = await run_episode(env, llm_fail_first=2)
    assert result.status == "done"
    # 2 LLM turns, each failed twice before succeeding = 6 attempts
    assert STATE["calls"]["llm_attempt"] == 6
    # nothing upstream was redone
    assert STATE["calls"]["download"] == 1 and STATE["calls"]["chunk"] == 3 and STATE["calls"]["merge"] == 1


async def test_invented_quote_is_rejected_then_fixed(env):
    result, _ = await run_episode(env, invent_first=True)
    assert result.status == "done"
    assert result.rejections == 1 and result.turns == 3
    assert all("shiny tools are always better" not in q["text"] for q in STATE["written"][0].notes["quotes"])


async def test_history_replays_deterministically(env):
    _, history = await run_episode(env, invent_first=True, llm_fail_first=1)
    await Replayer(workflows=[CatalogWorkflow, EpisodeWorkflow]).replay_workflow(history)


async def test_budget_gate_waits_for_signal(env):
    reset()
    io, cpu = workers(env)
    async with io, cpu:
        # each episode spends 0.002 and reserves a 0.05 cap while running. With a 0.053
        # budget episodes run one at a time, and the third (0.004 spent + 0.05) must wait.
        handle = await env.client.start_workflow(
            CatalogWorkflow.run,
            CatalogInput(feed_url="x", only=["hpr1", "hpr2", "hpr3"], budget_usd=0.053, cap_usd_per_episode=0.05, concurrency=3),
            id=f"cat-{uuid.uuid4()}", task_queue=TASK_QUEUE_IO,
        )
        # wait until two episodes finished and the third is blocked
        for _ in range(200):
            prog = await handle.query(CatalogWorkflow.progress)
            statuses = [e["status"] for e in prog["episodes"].values()]
            if statuses.count("done") == 2 and any(s.startswith("waiting for budget") for s in statuses):
                break
            await env.sleep(1)
        else:
            raise AssertionError(f"never blocked: {prog}")
        await handle.signal(CatalogWorkflow.raise_budget, 0.2)
        result = await handle.result()
    assert [e["status"] for e in result["episodes"]] == ["done", "done", "done"]
    assert result["budget_usd"] == 0.2
    assert abs(result["spent_usd"] - 0.006) < 1e-9


async def test_one_failed_episode_does_not_sink_the_catalog(env):
    reset(download_404={"hpr2"})
    io, cpu = workers(env)
    async with io, cpu:
        result = await env.client.execute_workflow(
            CatalogWorkflow.run,
            CatalogInput(feed_url="x", only=["hpr1", "hpr2"], budget_usd=1.0, concurrency=2),
            id=f"cat-{uuid.uuid4()}", task_queue=TASK_QUEUE_IO,
        )
    by_key = {e["key"]: e for e in result["episodes"]}
    assert by_key["hpr1"]["status"] == "done"
    assert by_key["hpr2"]["status"] == "failed" and "404" in by_key["hpr2"]["error"]
    assert STATE["calls"]["download"] == 2  # the 404 was not retried


async def test_agent_that_never_submits_fails_cleanly(env):
    reset()

    @activity.defn(name="llm_turn")
    async def chatty(req: LlmRequest) -> LlmResponse:
        return LlmResponse({"role": "assistant", "content": "thinking..."}, 10, 5, 0.0001, False, 0.1, 0.0001)

    acts = [a for a in IO_ACTS if a is not fake_llm] + [chatty]
    io = Worker(env.client, task_queue=TASK_QUEUE_IO, workflows=[CatalogWorkflow, EpisodeWorkflow], activities=acts)
    cpu = Worker(env.client, task_queue=TASK_QUEUE_CPU, activities=[fake_transcribe])
    async with io, cpu:
        with pytest.raises(WorkflowFailureError) as e:
            await env.client.execute_workflow(
                EpisodeWorkflow.run, EpisodeInput(EpisodeRef("hpr9", "E", "u")), id=f"ep-{uuid.uuid4()}", task_queue=TASK_QUEUE_IO,
            )
    assert "no valid notes" in str(e.value.cause)


async def test_agent_is_told_to_submit_before_it_runs_out_of_turns(env):
    """Regression for the first full run: a model that keeps reading until it is
    warned about its turn budget must still finish."""
    reset()

    @activity.defn(name="llm_turn")
    async def reader(req: LlmRequest) -> LlmResponse:
        warned = any(m["role"] == "user" and "turns left" in (m.get("content") or "") for m in req.messages)
        n = sum(1 for m in req.messages if m["role"] == "assistant")
        call = tool_call("submit_notes", VALID, f"s{n}") if warned else tool_call("read_transcript", {"start": "00:00", "end": "05:00"}, f"r{n}")
        return LlmResponse({"role": "assistant", "content": "", "tool_calls": [call]}, 10, 5, 0.0001, False, 0.1, 0.0001)

    acts = [a for a in IO_ACTS if a is not fake_llm] + [reader]
    io = Worker(env.client, task_queue=TASK_QUEUE_IO, workflows=[CatalogWorkflow, EpisodeWorkflow], activities=acts)
    cpu = Worker(env.client, task_queue=TASK_QUEUE_CPU, activities=[fake_transcribe])
    async with io, cpu:
        result = await env.client.execute_workflow(
            EpisodeWorkflow.run, EpisodeInput(EpisodeRef("hpr9", "E", "u"), cap_usd=0.05), id=f"ep-{uuid.uuid4()}", task_queue=TASK_QUEUE_IO,
        )
    assert result.status == "done"
    assert STATE["written"][0].stats["turns"] == agent.MAX_TURNS - agent.WARN_AT_TURNS_LEFT + 1
