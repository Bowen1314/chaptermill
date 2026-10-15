# chaptermill

Point it at a podcast RSS feed and leave it running on an ordinary PC. It turns the back catalog into transcripts, chapters, pull quotes that are checked word for word against the transcript, and show notes ready to paste (the demo run did 4.3 hours of audio in 44 minutes on a 2015-era desktop). Open-weight models do the work. [Temporal](https://temporal.io) makes it durable: a crash, a dropped connection or a rate-limited model API means it carries on from where it stopped, without redoing finished work or re-buying model replies it already has.

**Live demo:** https://bowen1314.github.io/chaptermill/ (nine Hacker Public Radio episodes, plus the report from a run where I kept killing the worker and the Temporal server)

Built for the DEV Hacktoberfest Open-Source AI Challenge, week 2 (Side Hustle), Temporal category.

## Who it is for

Freelance podcast producers who write show notes for clients, and independent shows sitting on hundreds of old episodes with no chapters and no transcripts. Hosted tools charge by the audio hour (see the cost table on the demo page); an archive of a few hundred episodes is exactly where that adds up. chaptermill runs on a machine you already own and pays only for the language-model calls, a few cents per audio hour.

Apple Podcasts and Spotify now generate chapters automatically for some shows. chaptermill is for the producer who needs the files: a `chapters.json` to embed in the feed, YouTube chapter text, SRT/VTT transcripts and Markdown show notes, for the whole archive, in one run.

## What comes out, per episode

| File | What it is |
|---|---|
| `show-notes.md` | summary, chapters, 2–4 pull quotes, topics, credit line |
| `chapters.json` | [Podcasting 2.0 chapters](https://github.com/Podcastindex-org/podcast-namespace/blob/main/docs/examples/chapters/jsonChapters.md) (version 1.2.0) |
| `youtube-chapters.txt` | `00:00 Title` lines for a video description |
| `transcript.srt`, `transcript.vtt`, `transcript.json` | timestamped transcript |
| `notes.json` | everything above plus how it was made (turns, tool calls, tokens, cost) |

## How it works

```
CatalogWorkflow (one per run)
  fetch_feed ──► for each episode, 2 at a time, inside a $ budget (signal raise_budget to extend):
      EpisodeWorkflow (child)
        download_audio        resumable, cached on disk
        probe_audio
        transcribe_chunk ×N   5-minute chunks, faster-whisper base.en on CPU, own task queue
        merge_transcript
        agent loop ≤12 turns  llm_turn ─► read_transcript / search_transcript / submit_notes
                              submit_notes ─► validate_notes (rejections go back to the model)
        write_outputs
```

* **Transcription** is local: [faster-whisper](https://github.com/SYSTRAN/faster-whisper) `base.en`, int8, two CPU threads. Each 5-minute chunk is one activity on a separate `chaptermill-cpu` task queue that runs one chunk at a time, so transcription never starves the rest of the worker.
* **The agent** is an open-weight model behind any OpenAI-compatible endpoint. The demo used [Qwen3-235B-A22B-Instruct-2507](https://huggingface.co/Qwen/Qwen3-235B-A22B-Instruct-2507) (Apache-2.0) on [Nebius Token Factory](https://tokenfactory.nebius.com). It sees a one-line-per-minute outline, reads the transcript through tools, and submits notes.
* **The validator** checks every submission against the transcript: quotes must appear verbatim (their timestamps are corrected if they are off), chapters must start at 00:00, be in order, at least 45 s apart and inside the episode. A failed check is returned to the model as the tool result; the model fixes it and resubmits.

## Durability: what each failure costs

| Failure | What happens | Where |
|---|---|---|
| Worker process killed mid-run | Temporal re-dispatches the in-flight activity; finished activities are never re-run. A heartbeat timeout notices a dead worker in 30–90 s instead of waiting for the start-to-close timeout. | `workflows.py`, `activities.py` (`_heartbeating`, `_beating_thread`) |
| Killed while transcribing | Only the chunk in progress is lost; finished chunks are files on disk, and a re-run chunk is skipped if its file exists. Whisper runs at temperature 0, so a re-run produces the same text. | `transcribe_chunk` |
| Killed while waiting for the model | The request is re-sent on retry. Every reply is written to a ledger and cached by a hash of the request *before* the activity returns, so a reply that was received is never bought again, even if the worker dies right after. | `llm_turn`, `store.py` |
| Model API returns 429/5xx | Retried with backoff; `Retry-After` is honoured as the next retry delay. 400/401/403/404 fail fast (non-retryable). The HTTP client's own retries are off, so Temporal is the only retry layer. | `llm.py` (`classify`) |
| Model invents a quote | Not a crash: the validator rejects it, and the agent loop continues in the same workflow. | `notes.py` |
| Budget used up | Episodes that would exceed the run budget wait, durably and for as long as it takes, until a `raise_budget` signal sets a higher total (`pause`/`resume` signals hold and release new starts). An episode that hits its own cap mid-loop waits up to a day for `raise_cap`. A hard cap per data directory stops all paid calls regardless. | `CatalogWorkflow._can_start`, `EpisodeWorkflow._wait_for_cap`, `CHAPTERMILL_HARD_CAP_USD` |
| Temporal server killed | Workflows resume from the server's SQLite file when it comes back. | `temporal server start-dev --db-filename …` |
| One episode fails for good | Recorded as failed with its root cause; the other episodes finish. | `CatalogWorkflow._one` |

Audio, transcripts and chunk results stay on disk; workflow history carries only file paths and the agent's conversation, so histories stay small.

## Results from the demo runs (2026-10-15)

On a shared home server (Intel i3-6100, 3.8 GB RAM, one spinning disk):

* **Clean run:** 9 Hacker Public Radio episodes, 4.3 h of audio, in 44 minutes. Transcription ran at 9.8× real time. 74 model replies, none bought twice, $0.125 in total ($0.029 per audio hour). The validator rejected 4 submissions; the model fixed each one in its next submission. The first run finished 7 of 9: on the two longest episodes the agent used all its turns reading and never submitted, so I added the turn-budget warning (`agent.WARN_AT_TURNS_LEFT`) and re-ran those two, with every chunk and the first nine replies of each coming from cache.
* **Chaos run:** 3 episodes, 9 SIGKILLs of the worker (one together with the Temporal server), plus injected 503s, cut-off downloads and failing tool calls. 3 of 3 finished in 12.5 minutes with no manual help; 88 activity attempts for 67 activities; transcripts identical, segment for segment, to the clean run; 18 model replies, none bought twice; 1 request was in flight at a kill and had to be asked again.
* **Transcript difference** from the transcripts Hacker Public Radio publishes: 12.6% of words overall (`scripts/agreement.py`). HPR's transcripts are also Whisper output and not proofread, so this is not an error rate.

The raw reports are in `demo/run.json`, `demo/chaos.json` and `demo/compare.json`.

## Run it

Requirements: Python 3.10+, `ffmpeg`, the [Temporal CLI](https://docs.temporal.io/cli), and an OpenAI-compatible endpoint whose model supports tool calling (a hosted open-weight model, or your own vLLM / llama.cpp server).

```bash
pip install -e ".[transcribe]"
temporal server start-dev --db-filename temporal.db \
    --sqlite-pragma journal_mode=wal --sqlite-pragma synchronous=normal

export CHAPTERMILL_LLM_BASE_URL=https://api.tokenfactory.nebius.com/v1   # or http://localhost:8000/v1
export CHAPTERMILL_LLM_MODEL=Qwen/Qwen3-235B-A22B-Instruct-2507
export CHAPTERMILL_LLM_API_KEY=...          # only this variable is read
export CHAPTERMILL_DATA=./data
export CHAPTERMILL_HARD_CAP_USD=1.00        # no paid call is made past this, per data dir

python -m chaptermill worker &              # io + cpu task queues
python -m chaptermill run --feed https://hackerpublicradio.org/hpr_rss.php --limit 5 --budget 0.25 --cap 0.05
python -m chaptermill status --id catalog-...        # progress query
python -m chaptermill raise-budget --id catalog-... --usd 0.50   # new total for the run
python -m chaptermill result --id catalog-...
```

Outputs land in `$CHAPTERMILL_DATA/episodes/<key>/`. Other settings: `CHAPTERMILL_WHISPER_MODEL` (default `base.en`), `CHAPTERMILL_WHISPER_THREADS` (2), `CHAPTERMILL_MODELS` (where Whisper weights are cached), `CHAPTERMILL_TEMPORAL` (`localhost:7233`). `CHAPTERMILL_FAULTS=llm=0.3,download=0.5,tool=0.15` injects failures on the first two attempts of those activities, for testing.

## Tests

```bash
pip install -e ".[dev]"
python -m pytest -q
```

Pure tests (timestamps, chunking, transcript search, the validator, renderers, feed parsing, HTTP error classification) and workflow tests on Temporal's time-skipping test server with fake activities: the happy path, a model API that fails twice on every call, an invented quote that gets rejected and fixed, deterministic replay of a recorded history, the budget gate waiting for a signal, one episode failing without sinking the catalog, and an agent that never submits.

`scripts/chaos.py` is the run behind the demo's durability section: it starts a catalog and SIGKILLs the worker at random intervals (and the Temporal server once), restarting each after a pause. `scripts/durability_report.py` reads the workflow histories and the model-call ledger afterwards; `scripts/export_demo.py` compares the chaos run's transcripts with the undisturbed run's.

## Limits, honestly

* Quotes are verified against the machine transcript, not against the audio. When Whisper mishears a word, the quote carries the mishearing (one demo quote says "scanned" where the host said "scant").
* English only as configured (`base.en`). Larger Whisper models are more accurate and slower.
* The demo's language model ran on a hosted endpoint, not on my machine; any OpenAI-compatible server with tool calling should work, but I only tested Nebius Token Factory. Gemma 3 27B on that endpoint ignored the tool definitions, so the agent needs a model with working tool calls.
* The demo machine is a shared home server (Intel i3-6100, 3.8 GB RAM, one spinning disk) that was also running other services; timings will differ elsewhere.

## License

Code: MIT (see `LICENSE`). The demo content under `demo/` is derived from [Hacker Public Radio](https://hackerpublicradio.org/) episodes released under CC BY-SA 4.0 and is shared under the same license; each file credits its episode and host. chaptermill is not affiliated with Hacker Public Radio.
