import json

import httpx
import pytest

from chaptermill import faults, notes, transcript as tx
from chaptermill.feed import parse_feed, select
from chaptermill.llm import LlmHttpError, assistant_message, chat_completion, classify, parse_retry_after

SEGS = [
    {"start": 0.0, "end": 9.0, "text": "This is Hacker Public Radio episode one for today."},
    {"start": 9.0, "end": 20.0, "text": "Today I want to talk about replacing cat with better tools."},
    {"start": 70.0, "end": 82.0, "text": "The first replacement is bat, which adds syntax highlighting and line numbers."},
    {"start": 200.0, "end": 214.0, "text": "Honestly the pager integration is the part I would miss the most if it went away."},
    {"start": 400.0, "end": 412.0, "text": "Next up is a tool that prints files in reverse, which sounds silly but is useful."},
    {"start": 560.0, "end": 575.0, "text": "That's all for today, thanks for listening and record a show yourself."},
]


def test_timestamps_roundtrip():
    assert tx.fmt_ts(0) == "00:00"
    assert tx.fmt_ts(75.9) == "01:15"
    assert tx.fmt_ts(3725) == "1:02:05"
    assert tx.parse_ts("01:15") == 75
    assert tx.parse_ts("1:02:05") == 3725
    assert tx.parse_ts(12) == 12.0
    assert tx.parse_ts("12.5") == 12.5
    assert tx.parse_ts("ten") is None
    assert tx.parse_ts(-3) is None
    assert tx.parse_ts(True) is None


def test_chunk_bounds_merges_short_tail():
    assert tx.chunk_bounds(720, 300) == [(0.0, 300.0), (300.0, 300.0), (600.0, 120.0)]
    assert tx.chunk_bounds(610, 300) == [(0.0, 300.0), (300.0, 310.0)]
    assert tx.chunk_bounds(100, 300) == [(0.0, 100.0)]
    total = sum(d for _, d in tx.chunk_bounds(3601.7, 300))
    assert abs(total - 3601.7) < 0.05


def test_merge_chunks_offsets_and_drops_empty():
    merged = tx.merge_chunks([(300.0, [{"start": 1.0, "end": 2.0, "text": " b "}]), (0.0, [{"start": 5, "end": 6, "text": "a"}, {"start": 7, "end": 8, "text": "  "}])])
    assert [s["text"] for s in merged] == ["a", "b"]
    assert merged[1]["start"] == 301.0


def test_outline_window_search_locate():
    out = tx.outline(SEGS, block_s=60)
    assert out.splitlines()[0].startswith("[00:00]")
    assert "[01:10]" in out
    w = tx.window(SEGS, 60, 210)
    assert "bat" in w and "pager" in w and "reverse" not in w
    assert tx.window(SEGS, 5000, 5100) == "(no speech in that range)"
    hits = tx.locate(SEGS, "the pager integration is the part I would miss")
    assert len(hits) == 1 and 200 <= hits[0] < 214
    assert tx.locate(SEGS, "the pager integration is NOT the part") == []
    s = tx.search(SEGS, "syntax highlighting")
    assert s[0]["exact"] and s[0]["ts"].startswith("01:1")
    fuzzy = tx.search(SEGS, "highlighting reverse")
    assert fuzzy and not fuzzy[0]["exact"]


def test_srt_vtt():
    srt = tx.to_srt(SEGS[:2])
    assert srt.startswith("1\n00:00:00,000 --> 00:00:09,000\n")
    assert tx.to_vtt(SEGS[:1]).startswith("WEBVTT\n\n00:00:00.000 --> 00:00:09.000")


def good_submission():
    return {
        "summary": " ".join(["word"] * 40),
        "chapters": [
            {"start": "00:10", "title": "Intro"},
            {"start": "01:10", "title": "bat"},
            {"start": "06:40", "title": "Reverse printing"},
        ],
        "quotes": [
            {"text": "the pager integration is the part I would miss the most", "start": "03:20"},
            {"text": "a tool that prints files in reverse, which sounds silly but is useful", "start": "00:00"},
        ],
        "topics": ["cat", "bat", "unix"],
    }


def test_validate_accepts_and_corrects():
    clean, errors, corrections = notes.validate_submission(good_submission(), SEGS, 600)
    assert errors == []
    assert clean["chapters"][0]["start"] == 0.0  # 00:10 snapped to 00:00
    assert 400 <= clean["quotes"][1]["start"] < 412  # wrong claimed time corrected
    assert corrections == 2


def test_validate_rejects_invented_quote_and_bad_chapters():
    sub = good_submission()
    sub["quotes"][0]["text"] = "the pager integration is the best thing ever invented by anyone"
    sub["chapters"][2]["start"] = "01:20"
    sub["chapters"].append({"start": "99:00", "title": "Too late"})
    clean, errors, _ = notes.validate_submission(sub, SEGS, 600)
    assert clean is None
    joined = "\n".join(errors)
    assert "not in the transcript word for word" in joined
    assert "at least 45s apart" in joined
    assert "after the episode ends" in joined


def test_validate_shape_errors():
    _, errors, _ = notes.validate_submission("nope", SEGS, 600)
    assert errors
    _, errors, _ = notes.validate_submission({"summary": "short", "chapters": [], "quotes": [], "topics": []}, SEGS, 600)
    assert len(errors) >= 4


def test_renderers():
    clean, _, _ = notes.validate_submission(good_submission(), SEGS, 600)
    pc = notes.to_podcast_chapters(clean, "Ep")
    assert pc["version"] == "1.2.0" and pc["chapters"][0] == {"startTime": 0.0, "title": "Intro"}
    assert notes.to_youtube_chapters(clean).splitlines()[0] == "00:00 Intro"
    md = notes.to_markdown(clean, "Ep", "https://x", "CC BY-SA")
    assert "## Pull quotes" in md and "CC BY-SA" in md
    assert notes.parse_args('{"a": 1}') == {"a": 1}
    assert "_unparsed" in notes.parse_args("{oops")


RSS = """<?xml version="1.0"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"><channel><title>HPR</title>
<item><title>HPR4746: Monotone</title><link>https://hackerpublicradio.org/eps/hpr4746/index.html</link>
<guid>g1</guid><itunes:author>Lee</itunes:author><itunes:summary>Lee does &lt;b&gt;analysis&lt;/b&gt;</itunes:summary>
<enclosure url="https://example.org/ccdn.php?filename=/eps/hpr4746/hpr4746.mp3" length="1" type="audio/mpeg"/></item>
<item><title>No audio</title></item>
<item><title>Other show</title><guid>xyz</guid><enclosure url="https://example.org/a.mp3" length="1" type="audio/mpeg"/></item>
</channel></rss>"""


def test_clean_author_drops_addresses():
    from chaptermill.feed import clean_author
    assert clean_author("someone.nospam@nospam.example.com (Lee)") == "Lee"
    assert clean_author("someone@example.com") == ""
    assert clean_author("Ken Fallon") == "Ken Fallon"


def test_feed_parse_and_select():
    eps = parse_feed(RSS)
    assert [e.key for e in eps][0] == "hpr4746"
    assert eps[1].key.startswith("ep-")
    assert eps[0].author == "Lee" and "analysis" in eps[0].summary and "<b>" not in eps[0].summary
    assert [e.key for e in select(eps, ["HPR4746"], 5)] == ["hpr4746"]
    assert len(select(eps, [], 1)) == 1
    with pytest.raises(ValueError):
        select(eps, ["hpr1"], 5)


def test_http_classification():
    assert classify(429, {"Retry-After": "7"}).retryable
    assert classify(429, {"Retry-After": "7"}).delay_s == 7.0
    assert classify(503, {}).retryable and classify(503, {}).delay_s is None
    assert classify(429, {"retry-after": "600"}).delay_s is None  # outside 0-60s: let Temporal back off
    assert not classify(400, {}).retryable
    assert not classify(401, {}).retryable
    assert classify(408, {}).retryable and classify(409, {}).retryable
    assert classify(400, {"x-should-retry": "true"}).retryable
    assert not classify(503, {"x-should-retry": "false"}).retryable
    assert parse_retry_after({"retry-after-ms": "1500"}) == 1.5
    assert parse_retry_after({}) is None


@pytest.mark.asyncio
async def test_chat_completion_with_mock_transport():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert request.headers["authorization"] == "Bearer k"
        if body["model"] == "bad":
            return httpx.Response(401, json={"error": "nope"})
        if body["model"] == "busy":
            return httpx.Response(429, headers={"retry-after": "2"}, json={"error": "slow down"})
        return httpx.Response(200, json={"choices": [{"message": {"content": None, "tool_calls": [{"id": "c1", "function": {"name": "search_transcript", "arguments": "{\"query\":\"x\"}"}}]}}], "usage": {"prompt_tokens": 10, "completion_tokens": 2}})

    t = httpx.MockTransport(handler)
    resp, _ = await chat_completion("https://x/v1", "k", {"model": "ok", "messages": []}, transport=t)
    msg = assistant_message(resp)
    assert msg["role"] == "assistant" and msg["tool_calls"][0]["function"]["name"] == "search_transcript"
    with pytest.raises(LlmHttpError) as e:
        await chat_completion("https://x/v1", "k", {"model": "bad"}, transport=t)
    assert not e.value.verdict.retryable
    with pytest.raises(LlmHttpError) as e:
        await chat_completion("https://x/v1", "k", {"model": "busy"}, transport=t)
    assert e.value.verdict.retryable and e.value.verdict.delay_s == 2.0


def test_faults_deterministic_and_bounded():
    assert faults.parse("llm=0.3, download=2, bad") == {"llm": 0.3, "download": 1.0}
    assert faults.should_fail("llm=1", "llm", "a", 1)
    assert not faults.should_fail("llm=1", "llm", "a", 3)  # never past attempt 2
    assert not faults.should_fail("", "llm", "a", 1)
    a = [faults.should_fail("llm=0.5", "llm", f"id{i}", 1) for i in range(200)]
    b = [faults.should_fail("llm=0.5", "llm", f"id{i}", 1) for i in range(200)]
    assert a == b and 60 < sum(a) < 140
    with pytest.raises(faults.InjectedFault):
        faults.maybe_fail("tool=1", "tool", "x", 1, "boom")
