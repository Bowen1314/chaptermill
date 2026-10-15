"""Run the workers. One process can serve both task queues:

  chaptermill-io   workflows + network/disk activities (downloads, LLM turns, agent tools)
  chaptermill-cpu  transcription, one chunk at a time in a single thread
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging

from temporalio.client import Client
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

from .activities import Activities
from .config import TASK_QUEUE_CPU, TASK_QUEUE_IO, Settings, load_settings
from .workflows import CatalogWorkflow, EpisodeWorkflow


# Pass our own (side-effect free) modules and their dependencies through the
# sandbox instead of re-importing them for every workflow run; on a slow disk the
# re-import alone could exceed the workflow task timeout.
RUNNER = SandboxedWorkflowRunner(
    restrictions=SandboxRestrictions.default.with_passthrough_modules(
        "chaptermill", "httpx", "httpcore", "anyio", "sniffio", "h11", "idna", "certifi", "numpy"
    )
)


async def run_workers(settings: Settings, queues: set[str]) -> None:
    client = await Client.connect(settings.temporal_address)
    acts = Activities(settings)
    workers = []
    if "io" in queues:
        workers.append(
            Worker(
                client,
                task_queue=TASK_QUEUE_IO,
                workflows=[CatalogWorkflow, EpisodeWorkflow],
                activities=[
                    acts.fetch_feed, acts.download_audio, acts.probe_audio, acts.merge_transcript,
                    acts.llm_turn, acts.read_transcript, acts.search_transcript, acts.validate_notes,
                    acts.write_outputs,
                ],
                # sync (disk-bound) activities run here, off the event loop
                activity_executor=concurrent.futures.ThreadPoolExecutor(max_workers=4),
                max_concurrent_activities=4,
                workflow_runner=RUNNER,
                # In our first full run one eagerly dispatched activity was never
                # executed and only recovered at its start_to_close timeout; plain
                # polling avoids that path on this single-host setup.
                disable_eager_activity_execution=True,
            )
        )
    if "cpu" in queues:
        workers.append(
            Worker(
                client,
                task_queue=TASK_QUEUE_CPU,
                activities=[acts.transcribe_chunk],
                activity_executor=concurrent.futures.ThreadPoolExecutor(max_workers=1),
                max_concurrent_activities=1,
            )
        )
    logging.info("chaptermill worker up: queues=%s data=%s model=%s", sorted(queues), settings.data_dir, settings.llm_model)
    await asyncio.gather(*(w.run() for w in workers))


def main(queues: str = "io,cpu") -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(run_workers(load_settings(), {q.strip() for q in queues.split(",") if q.strip()}))
