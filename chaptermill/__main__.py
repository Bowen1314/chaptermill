"""Command line: python -m chaptermill {worker,run,status,raise-budget,result}"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import timedelta

from temporalio.client import Client
from temporalio.service import RPCError

from .config import TASK_QUEUE_IO, load_settings
from .payloads import CatalogInput


async def _client() -> Client:
    return await Client.connect(load_settings().temporal_address)


async def cmd_run(a: argparse.Namespace) -> None:
    client = await _client()
    wid = a.id or f"catalog-{time.strftime('%Y%m%d-%H%M%S')}"
    inp = CatalogInput(
        feed_url=a.feed,
        only=[x for x in (a.only or "").split(",") if x],
        limit=a.limit,
        budget_usd=a.budget,
        cap_usd_per_episode=a.cap,
        concurrency=a.concurrency,
        model=a.model or "",
    )
    handle = await client.start_workflow(
        "CatalogWorkflow", inp, id=wid, task_queue=TASK_QUEUE_IO, task_timeout=timedelta(seconds=60)
    )
    print(f"started {wid}")
    if a.wait:
        print(json.dumps(await handle.result(), indent=1))


async def cmd_status(a: argparse.Namespace) -> None:
    client = await _client()
    h = client.get_workflow_handle(a.id)
    prog = await h.query("progress")
    for key, ep in prog["episodes"].items():
        if ep.get("status") == "running":
            try:
                ep["detail"] = await client.get_workflow_handle(f"{a.id}--{key}").query("progress")
            except RPCError:
                pass
    print(json.dumps(prog, indent=1))


async def cmd_raise_budget(a: argparse.Namespace) -> None:
    client = await _client()
    await client.get_workflow_handle(a.id).signal("raise_budget", a.usd)
    print(f"signalled raise_budget({a.usd}) to {a.id}")


async def cmd_result(a: argparse.Namespace) -> None:
    client = await _client()
    print(json.dumps(await client.get_workflow_handle(a.id).result(), indent=1))


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="chaptermill")
    sub = p.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("worker", help="run workers")
    w.add_argument("--queues", default="io,cpu", help="io, cpu or io,cpu")
    r = sub.add_parser("run", help="start a back-catalog run")
    r.add_argument("--feed", required=True)
    r.add_argument("--only", help="comma-separated episode keys, e.g. hpr4746,hpr4743")
    r.add_argument("--limit", type=int, default=5)
    r.add_argument("--budget", type=float, default=0.25, help="USD for the whole run")
    r.add_argument("--cap", type=float, default=0.05, help="USD per episode")
    r.add_argument("--concurrency", type=int, default=2)
    r.add_argument("--model", default="")
    r.add_argument("--id")
    r.add_argument("--wait", action="store_true")
    s = sub.add_parser("status")
    s.add_argument("--id", required=True)
    b = sub.add_parser("raise-budget")
    b.add_argument("--id", required=True)
    b.add_argument("--usd", type=float, required=True)
    res = sub.add_parser("result")
    res.add_argument("--id", required=True)
    a = p.parse_args(argv)
    if a.cmd == "worker":
        from .worker import main as worker_main

        worker_main(a.queues)
        return
    fn = {"run": cmd_run, "status": cmd_status, "raise-budget": cmd_raise_budget, "result": cmd_result}[a.cmd]
    asyncio.run(fn(a))


if __name__ == "__main__":
    main(sys.argv[1:])
