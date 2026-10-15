"""Chaos run: start a catalog, then keep SIGKILLing the worker (and once the
Temporal server) until the catalog finishes. Linux + systemd, run as root.

  CHAPTERMILL_ROOT=/srv/chaptermill python scripts/chaos.py --id chaos-1 --data /srv/chaptermill/data-chaos \
      --feed https://hackerpublicradio.org/hpr_rss.php --only hpr4747,hpr4743 \
      --kill-every 45:110 --server-kill 3 --faults "llm=0.3,download=0.5,tool=0.15" \
      --out chaos_log.json

$CHAPTERMILL_ROOT holds bin/temporal (the Temporal CLI), temporal.db, and
run-worker.sh (sets CHAPTERMILL_LLM_API_KEY and execs `python -m chaptermill worker`).
The worker runs as a transient systemd unit with no restart policy, so it stays
dead until this script starts it again. Everything that happens is logged with
timestamps, together with the stage each episode was in when the kill landed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import socket
import subprocess
import time
from datetime import timedelta
from pathlib import Path

from temporalio.client import Client, WorkflowExecutionStatus

from chaptermill.config import TASK_QUEUE_IO
from chaptermill.payloads import CatalogInput

WORKER_UNIT = "chaptermill-chaos-worker"
SERVER_UNIT = "chaptermill-temporal"
ROOT = Path(os.environ.get("CHAPTERMILL_ROOT", "/srv/chaptermill"))


def sh(*cmd: str, check: bool = False) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"{cmd}: {r.stderr}")
    return r.stdout.strip()


def port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


def start_server() -> None:
    sh("systemctl", "reset-failed", SERVER_UNIT)
    sh("systemd-run", f"--unit={SERVER_UNIT}", "-p", "MemoryMax=500M", "-p", "Nice=10",
       str(ROOT / "bin/temporal"), "server", "start-dev", "--db-filename", str(ROOT / "temporal.db"),
       # WAL + synchronous=normal: a killed server process loses nothing (only a power
       # cut could drop the last transactions), and far fewer fsyncs on a busy HDD.
       "--sqlite-pragma", "journal_mode=wal", "--sqlite-pragma", "synchronous=normal",
       "--ip", "127.0.0.1", "--ui-ip", "127.0.0.1", "--log-level", "warn", check=True)
    for _ in range(60):
        if port_open(7233):
            return
        time.sleep(1)
    raise RuntimeError("temporal server did not come back")


def kill_server() -> None:
    sh("systemctl", "kill", "-s", "SIGKILL", SERVER_UNIT)
    for _ in range(30):
        if not port_open(7233):
            return
        time.sleep(0.5)


def start_worker(data: str, faults: str) -> None:
    sh("systemctl", "reset-failed", WORKER_UNIT)
    sh("systemd-run", f"--unit={WORKER_UNIT}", "-p", "MemoryMax=1300M", "-p", "Nice=10", "-p", "CPUQuota=200%",
       f"--setenv=CHAPTERMILL_DATA={data}", f"--setenv=CHAPTERMILL_FAULTS={faults}",
       str(ROOT / "run-worker.sh"), check=True)


def kill_worker() -> None:
    sh("systemctl", "kill", "-s", "SIGKILL", WORKER_UNIT)


async def stages(client: Client, wid: str) -> dict:
    try:
        prog = await asyncio.wait_for(client.get_workflow_handle(wid).query("progress"), 10)
    except Exception as e:  # noqa: BLE001
        return {"error": type(e).__name__}
    out = {}
    for key, ep in prog["episodes"].items():
        if ep.get("status") == "running":
            try:
                d = await asyncio.wait_for(client.get_workflow_handle(f"{wid}--{key}").query("progress"), 10)
                out[key] = f"{d['stage']} (chunks {d['chunks']}, turns {d['turns']})"
            except Exception as e:  # noqa: BLE001
                out[key] = f"running ({type(e).__name__})"
        else:
            out[key] = ep.get("status")
    return out


async def closed(client: Client, wid: str) -> str | None:
    try:
        d = await client.get_workflow_handle(wid).describe()
    except Exception:  # noqa: BLE001 - server may be down right now
        return None
    return None if d.status == WorkflowExecutionStatus.RUNNING else d.status.name


async def connect() -> Client:
    for _ in range(60):
        try:
            return await Client.connect("localhost:7233")
        except Exception:  # noqa: BLE001
            await asyncio.sleep(2)
    raise RuntimeError("cannot connect to temporal")


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--id", required=True)
    p.add_argument("--data", required=True)
    p.add_argument("--feed", required=True)
    p.add_argument("--only", required=True)
    p.add_argument("--kill-every", default="45:110")
    p.add_argument("--down", default="5:20")
    p.add_argument("--server-kill", type=int, default=3, help="also kill the server on this kill number (0 = never)")
    p.add_argument("--faults", default="")
    p.add_argument("--budget", type=float, default=0.30)
    p.add_argument("--cap", type=float, default=0.06)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--max-kills", type=int, default=12)
    p.add_argument("--out", required=True)
    a = p.parse_args()
    rng = random.Random(a.seed)
    lo, hi = (float(x) for x in a.kill_every.split(":"))
    dlo, dhi = (float(x) for x in a.down.split(":"))
    log: dict = {"id": a.id, "seed": a.seed, "faults": a.faults, "kill_every_s": [lo, hi], "events": []}
    t0 = time.time()

    def note(action: str, **kw) -> None:
        ev = {"t": round(time.time() - t0, 1), "wall": time.strftime("%H:%M:%S"), "action": action, **kw}
        log["events"].append(ev)
        print(json.dumps(ev), flush=True)

    if not port_open(7233):
        start_server()
    start_worker(a.data, a.faults)
    client = await connect()
    await client.start_workflow(
        "CatalogWorkflow",
        CatalogInput(feed_url=a.feed, only=a.only.split(","), budget_usd=a.budget, cap_usd_per_episode=a.cap, concurrency=2),
        id=a.id, task_queue=TASK_QUEUE_IO, task_timeout=timedelta(seconds=60),
    )
    note("start", only=a.only)
    kills = 0
    status = None
    while status is None:
        wait = rng.uniform(lo, hi)
        deadline = time.time() + wait
        while time.time() < deadline and status is None:
            await asyncio.sleep(3)
            status = await closed(client, a.id)
        if status is not None or kills >= a.max_kills:
            break
        kills += 1
        snapshot = await stages(client, a.id)
        kill_worker()
        also_server = a.server_kill and kills == a.server_kill
        if also_server:
            kill_server()
        note("kill", n=kills, server_too=bool(also_server), stages=snapshot)
        await asyncio.sleep(rng.uniform(dlo, dhi))
        if also_server:
            start_server()
            client = await connect()
            note("server_restarted")
        start_worker(a.data, a.faults)
        note("worker_restarted")
    while status is None:
        await asyncio.sleep(5)
        status = await closed(client, a.id)
    note("closed", status=status)
    log["kills"] = kills
    log["wall_clock_s"] = round(time.time() - t0, 1)
    kill_worker()
    Path(a.out).write_text(json.dumps(log, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
