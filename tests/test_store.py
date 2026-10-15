from chaptermill.store import JsonCache, Ledger, atomic_write_json, read_json, request_key


def test_request_key_is_order_independent():
    assert request_key({"a": 1, "b": [1, 2]}) == request_key({"b": [1, 2], "a": 1})
    assert request_key({"a": 1}) != request_key({"a": 2})


def test_cache_and_ledger(tmp_path):
    c = JsonCache(tmp_path / "c")
    assert c.get("ab" * 32) is None
    c.put("ab" * 32, {"x": 1})
    assert c.get("ab" * 32) == {"x": 1}
    led = Ledger(tmp_path / "l.jsonl")
    assert led.total_usd() == 0
    led.append({"usd": 0.25})
    led.append({"usd": 0.5})
    (tmp_path / "l.jsonl").open("a").write("not json\n")
    assert led.total_usd() == 0.75
    atomic_write_json(tmp_path / "d" / "x.json", {"k": "v"})
    assert read_json(tmp_path / "d" / "x.json") == {"k": "v"}
    assert not list((tmp_path / "d").glob(".tmp-*"))


def test_side_thread_heartbeats_while_sync_activity_is_busy():
    import time

    from temporalio.testing import ActivityEnvironment

    from chaptermill.activities import _beating_thread

    beats = []
    env = ActivityEnvironment()
    env.on_heartbeat = lambda *d: beats.append(d[0])

    def busy() -> None:
        progress = {"phase": "load_model"}
        with _beating_thread(progress, every_s=0.05):
            time.sleep(0.3)  # one long call that cannot heartbeat itself
            progress["phase"] = "transcribe"
            time.sleep(0.15)

    env.run(busy)
    assert len(beats) >= 4
    assert beats[0]["phase"] == "load_model" and beats[-1]["phase"] == "transcribe"
