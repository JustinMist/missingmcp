import time
import pytest
from garmin_gateway import workers
from garmin_gateway.config import load_config


def _config(tmp_path, **over):
    env = {"GATEWAY_SECRET": "s" * 40, "DATA_DIR": str(tmp_path), "PUBLIC_URL": "https://x"}
    env.update({k.upper(): str(v) for k, v in over.items()})
    return load_config(env)


async def test_ensure_spawns_and_reuses(tmp_path, fake_worker):
    spawned = []

    class FakeProc:
        def __init__(self): self._alive = True
        def poll(self): return None if self._alive else 0
        def terminate(self): self._alive = False

    def spawn(key, port, token_dir):
        spawned.append((key, port, token_dir))
        return FakeProc()

    cfg = _config(tmp_path, worker_port_start=fake_worker.port, worker_port_end=fake_worker.port)
    mgr = workers.WorkerManager(cfg, spawn=spawn)
    port1 = await mgr.ensure_worker("me@x.cz", '{"t":1}')
    assert port1 == fake_worker.port
    port2 = await mgr.ensure_worker("me@x.cz", '{"t":1}')
    assert port2 == fake_worker.port
    assert len(spawned) == 1                      # reused, not respawned
    # tokens were materialized
    assert (tmp_path / "users").exists()
    mgr.shutdown()


async def test_ensure_raises_when_never_healthy(tmp_path):
    class DeadProc:
        def poll(self): return 1                  # already exited
        def terminate(self): pass

    cfg = _config(tmp_path, worker_startup_timeout=1, worker_port_start=59999, worker_port_end=59999)
    mgr = workers.WorkerManager(cfg, spawn=lambda *a: DeadProc())
    with pytest.raises(workers.WorkerStartError):
        await mgr.ensure_worker("me@x.cz", "{}")


async def test_reap_idle_terminates(tmp_path, fake_worker):
    clock = [1000.0]

    class FakeProc:
        def __init__(self): self.alive = True
        def poll(self): return None if self.alive else 0
        def terminate(self): self.alive = False

    proc = FakeProc()
    cfg = _config(tmp_path, worker_idle_ttl=10,
                  worker_port_start=fake_worker.port, worker_port_end=fake_worker.port)
    mgr = workers.WorkerManager(cfg, spawn=lambda *a: proc, clock=lambda: clock[0])
    await mgr.ensure_worker("me@x.cz", "{}")
    clock[0] = 1100.0                              # advance past idle ttl
    await mgr.reap_idle()
    assert proc.alive is False
