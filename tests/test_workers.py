import asyncio
import hashlib
import json
import os
import stat
import time
import pytest
from missingmcp import store, workers
from missingmcp.adapters.garmin import GarminWorkerForward
from missingmcp.adapters.garmin.blob import pack_blob, unpack_blob
from missingmcp.config import load_config


def _config(tmp_path, **over):
    env = {"GATEWAY_SECRET": "s" * 40, "DATA_DIR": str(tmp_path), "PUBLIC_URL": "https://x"}
    env.update({k.upper(): str(v) for k, v in over.items()})
    return load_config(env)


def _blob(token: int, region: str = "global") -> str:
    return pack_blob(_tokens(token), region)


def _tokens(token: int) -> str:
    return json.dumps({
        "di_token": f"access-{token}",
        "di_refresh_token": f"refresh-{token}",
        "di_client_id": f"client-{token}",
    }, sort_keys=True, separators=(",", ":"))


async def _async_value(value):
    return value


class _StoppableProc:
    def __init__(self):
        self.alive = True

    def poll(self):
        return None if self.alive else 0

    def terminate(self):
        self.alive = False

    def kill(self):
        self.alive = False


class _DelayedFinalWriteProc(_StoppableProc):
    def __init__(self, path, content, delay=0.03):
        super().__init__()
        self.path = path
        self.content = content
        self.delay = delay

    def terminate(self):
        import threading

        def finish():
            self.path.write_text(self.content)
            self.alive = False

        threading.Timer(self.delay, finish).start()


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
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg), spawn=spawn)
    port1 = await mgr.ensure_worker("me@x.cz", _blob(1))
    assert port1 == fake_worker.port
    port2 = await mgr.ensure_worker("me@x.cz", _blob(1))
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
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg), spawn=lambda *a: DeadProc())
    with pytest.raises(workers.WorkerStartError):
        await mgr.ensure_worker("me@x.cz", _blob(1))


async def test_self_exit_during_startup_is_credentials_rejected(tmp_path):
    # A worker that quits by itself has judged the account unserviceable — for
    # garmin_mcp, stale tokens ("OAuth tokens not found ... Exiting.", rc 0). That's
    # the user's re-sign-in, not an operator's incident, so it must be its own
    # exception type and must not wait out the whole startup timeout.
    class SelfExitedProc:
        def poll(self): return 0                  # clean exit, exactly what garmin_mcp does
        def terminate(self): pass

    cfg = _config(tmp_path, worker_startup_timeout=30, worker_port_start=59995, worker_port_end=59995)
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg), spawn=lambda *a: SelfExitedProc())
    t0 = time.monotonic()
    with pytest.raises(workers.WorkerCredentialsRejected):
        await mgr.ensure_worker("me@x.cz", _blob(1))
    assert time.monotonic() - t0 < 5              # gave up on exit, didn't sit out the 30s


@pytest.mark.parametrize("rc", [1, 137, -11])
async def test_crashed_worker_is_not_filed_as_stale_credentials(tmp_path, rc):
    # A worker that dies non-zero has crashed — rc 1 on a traceback, 137 on an OOM
    # kill, negative on a signal. Filing those as stale credentials would silence
    # the ops alert on a real outage (e.g. a bad GARMIN_MCP_REF bump crashing every
    # worker), which is the opposite of what the event split is for.
    class CrashedProc:
        def poll(self): return rc
        def terminate(self): pass

    cfg = _config(tmp_path, worker_startup_timeout=30, worker_port_start=59993, worker_port_end=59993)
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg), spawn=lambda *a: CrashedProc())
    with pytest.raises(workers.WorkerStartError) as exc:
        await mgr.ensure_worker("me@x.cz", _blob(1))
    assert not isinstance(exc.value, workers.WorkerCredentialsRejected)
    assert str(rc) in str(exc.value)              # the code is in the message, for triage


async def test_hanging_worker_is_a_plain_start_error(tmp_path):
    # Alive but silent on /healthz is the other failure: something is genuinely
    # wrong with the worker, and it must NOT be filed as stale credentials.
    class AliveProc:
        def __init__(self): self.alive = True
        def poll(self): return None if self.alive else 0
        def terminate(self): self.alive = False

    cfg = _config(tmp_path, worker_startup_timeout=1, worker_port_start=59994, worker_port_end=59994)
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg), spawn=lambda *a: AliveProc())
    with pytest.raises(workers.WorkerStartError) as exc:
        await mgr.ensure_worker("me@x.cz", _blob(1))
    assert not isinstance(exc.value, workers.WorkerCredentialsRejected)


class _GatedProc:
    """Healthy-process stand-in carrying a login gate, as _default_spawn's procs
    do since garmin_mcp's login moved to a background thread (#255): the worker
    answers /healthz regardless of whether the Garmin sign-in succeeded, so the
    gate is the only startup signal that the tokens still work."""
    def __init__(self, outcome=None):
        self.alive = True
        self.login_gate = workers.LoginGate()
        self.login_gate.outcome = outcome
    def poll(self): return None if self.alive else 0
    def terminate(self): self.alive = False


async def test_login_failure_line_is_credentials_rejected(tmp_path, fake_worker):
    # A healthy worker whose pump classified "Garmin Connect client failed to
    # initialize" has rejected the account's stored tokens — same self-heal path
    # as the old clean startup exit: re-auth, not an operator.
    procs = []
    def spawn(key, port, token_dir):
        procs.append(_GatedProc(outcome="failed"))
        return procs[-1]

    cfg = _config(tmp_path, worker_port_start=fake_worker.port, worker_port_end=fake_worker.port)
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg), spawn=spawn)
    with pytest.raises(workers.WorkerCredentialsRejected):
        await mgr.ensure_worker("me@x.cz", _blob(1))
    assert procs[0].alive is False                # the useless worker was stopped
    assert mgr.active_count() == 0                # and never registered


async def test_login_ok_line_admits_worker(tmp_path, fake_worker):
    cfg = _config(tmp_path, worker_port_start=fake_worker.port, worker_port_end=fake_worker.port)
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg),
                                spawn=lambda *a: _GatedProc(outcome="ok"))
    port = await mgr.ensure_worker("me@x.cz", _blob(1))
    assert port == fake_worker.port
    mgr.shutdown()


async def test_login_gate_silence_is_a_plain_start_error(tmp_path, fake_worker):
    # Healthy but never reporting a sign-in outcome (login wedged, or the worker
    # stopped printing the lines): genuinely wrong, must NOT be filed as stale
    # credentials — mirrors test_hanging_worker one boot stage later.
    cfg = _config(tmp_path, worker_startup_timeout=1,
                  worker_port_start=fake_worker.port, worker_port_end=fake_worker.port)
    proc = _GatedProc(outcome=None)
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg),
                                spawn=lambda *a: proc)
    t0 = time.monotonic()
    with pytest.raises(workers.WorkerStartError) as exc:
        await mgr.ensure_worker("me@x.cz", _blob(1))
    assert not isinstance(exc.value, workers.WorkerCredentialsRejected)
    assert time.monotonic() - t0 < 5              # bounded by the startup budget
    assert proc.alive is False                     # timed-out process was cleaned up
    assert mgr.active_count() == 0


async def test_login_gate_outcome_arriving_late_is_honored(tmp_path, fake_worker):
    # The outcome lands only after /healthz already answered — the gate must
    # keep polling instead of reading the slot once.
    proc = _GatedProc(outcome=None)
    cfg = _config(tmp_path, worker_startup_timeout=10,
                  worker_port_start=fake_worker.port, worker_port_end=fake_worker.port)
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg), spawn=lambda *a: proc)

    async def flip():
        await asyncio.sleep(0.5)
        proc.login_gate.outcome = "ok"
    flip_task = asyncio.ensure_future(flip())
    port = await mgr.ensure_worker("me@x.cz", _blob(1))
    assert port == fake_worker.port
    await flip_task
    mgr.shutdown()


async def test_health_and_login_share_one_startup_deadline(tmp_path):
    # Health must not consume one full timeout and then give sign-in another one:
    # both stages are one boot and stay inside the existing startup SLA.
    clock = [100.0]
    proc = _GatedProc(outcome="ok")
    cfg = _config(tmp_path, worker_startup_timeout=7,
                  worker_port_start=59992, worker_port_end=59992)
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg),
                                spawn=lambda *a: proc, clock=lambda: clock[0])
    deadlines = []

    async def wait_healthy(port, spawned, deadline):
        deadlines.append(deadline)
        clock[0] = 105.0                         # health used most of the budget
        return "healthy"

    async def wait_login(gate, spawned, deadline):
        deadlines.append(deadline)
        return "ok"

    mgr._wait_healthy = wait_healthy
    mgr._wait_login = wait_login
    assert await mgr.ensure_worker("me@x.cz", _blob(1)) == 59992
    assert deadlines == [107.0, 107.0]            # no fresh deadline after health
    mgr.shutdown()


@pytest.mark.parametrize(
    ("rc", "expected"),
    [(0, workers.WorkerCredentialsRejected),
     (1, workers.WorkerStartError),
     (-9, workers.WorkerStartError)],
)
async def test_exit_during_login_wait_keeps_startup_exit_semantics(
        tmp_path, rc, expected):
    # A process can disappear after /healthz succeeds but before its sign-in
    # verdict arrives. Preserve the established clean-exit/stale-token split;
    # crashes and signals must remain operator-visible failures.
    class ExitedGatedProc:
        def __init__(self):
            self.login_gate = workers.LoginGate()

        def poll(self):
            return rc

        def terminate(self):
            raise AssertionError("an exited process must not be terminated again")

    cfg = _config(tmp_path, worker_port_start=59991, worker_port_end=59991)
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg),
                                spawn=lambda *a: ExitedGatedProc())

    async def healthy(*args):
        return "healthy"

    async def exited(*args):
        return "exited"

    mgr._wait_healthy = healthy
    mgr._wait_login = exited
    with pytest.raises(expected) as exc:
        await mgr.ensure_worker("me@x.cz", _blob(1))
    if rc != 0:
        assert not isinstance(exc.value, workers.WorkerCredentialsRejected)
    assert mgr.active_count() == 0


async def test_login_verdict_wins_over_a_simultaneous_process_exit(tmp_path):
    # The pump may publish its final verdict just before the worker exits. The
    # verdict is stronger evidence than poll(), so it must not be lost to the
    # race between those two observations.
    gate = workers.LoginGate()
    gate.outcome = "failed"

    class ExitedProc:
        def poll(self):
            return 1

    cfg = _config(tmp_path)
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg))
    assert await mgr._wait_login(gate, ExitedProc(), mgr._clock() + 1) == "failed"


async def test_cancelled_spawn_stops_the_orphan_worker(tmp_path, fake_worker):
    # A client disconnect cancels the request task mid-boot. The spawned process
    # is not yet registered and `finally` un-reserves its port — left running it
    # would hold a port the allocator considers free (CodeRabbit, PR #27).
    proc = _GatedProc(outcome=None)               # healthy, parks in the login wait
    cfg = _config(tmp_path, worker_startup_timeout=30,
                  worker_port_start=fake_worker.port, worker_port_end=fake_worker.port)
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg), spawn=lambda *a: proc)
    task = asyncio.ensure_future(mgr.ensure_worker("me@x.cz", _blob(1)))
    await asyncio.sleep(0.6)                      # past /healthz, into _wait_login
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert proc.alive is False                    # stopped, port cooling — not orphaned
    assert mgr.active_count() == 0


async def test_cancelled_startup_captures_final_rotation_and_region(
        tmp_path, fake_worker):
    key = "cn:me@x.cz"
    durable = {key: _blob(1, "cn")}

    def load(account):
        return durable.get(account)

    def persist(account, value, expected):
        if durable.get(account) != expected:
            return False
        durable[account] = value
        return True

    class FinalWriteGatedProc(_GatedProc):
        def terminate(self):
            import threading

            def finish():
                _token_file(tmp_path, key).write_text(_tokens(2))
                self.alive = False

            threading.Timer(0.03, finish).start()

    proc = FinalWriteGatedProc(outcome=None)
    cfg = _config(tmp_path, worker_startup_timeout=30,
                  worker_port_start=fake_worker.port,
                  worker_port_end=fake_worker.port)
    mgr = workers.WorkerManager(
        cfg, GarminWorkerForward(cfg), spawn=lambda *a: proc,
        persist=persist, load=load)
    task = asyncio.ensure_future(mgr.ensure_worker(key, _blob(1, "cn")))
    await asyncio.sleep(0.6)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert unpack_blob(durable[key]) == ("cn", _tokens(2))
    assert key not in mgr._orphaned
    assert key not in mgr._pending_capture

    # A fresh manager must consume the captured generation instead of
    # rematerializing the stale request hint after a gateway restart.
    restarted = workers.WorkerManager(
        cfg, GarminWorkerForward(cfg), spawn=lambda *a: _StoppableProc(),
        persist=persist, load=load)
    assert await restarted.ensure_worker(key, _blob(1, "cn")) == fake_worker.port
    assert _token_file(tmp_path, key).read_text() == _tokens(2)
    restarted.shutdown()


async def test_cancelled_startup_retries_temporary_capture_failure_before_reuse(
        tmp_path, fake_worker):
    key = "global:me@x.cz"
    durable = {key: _blob(1)}
    attempts = []

    def persist(account, value, expected):
        attempts.append((account, value, expected))
        if len(attempts) == 1:
            raise OSError("temporary database outage")
        if durable.get(account) != expected:
            return False
        durable[account] = value
        return True

    class FinalWriteGatedProc(_GatedProc):
        def terminate(self):
            _token_file(tmp_path, key).write_text(_tokens(2))
            self.alive = False

    procs = [FinalWriteGatedProc(outcome=None), _GatedProc(outcome="ok")]
    cfg = _config(tmp_path, worker_startup_timeout=30,
                  worker_port_start=fake_worker.port,
                  worker_port_end=fake_worker.port)
    mgr = workers.WorkerManager(
        cfg, GarminWorkerForward(cfg), spawn=lambda *a: procs.pop(0),
        persist=persist, load=lambda account: durable.get(account))
    task = asyncio.ensure_future(mgr.ensure_worker(key, _blob(1)))
    await asyncio.sleep(0.6)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert key in mgr._pending_capture
    assert durable[key] == _blob(1)

    await mgr.persist_rotated()  # periodic retry works without another request
    assert durable[key] == _blob(2)
    assert key not in mgr._pending_capture
    assert await mgr.ensure_worker(key, _blob(1)) == fake_worker.port
    assert durable[key] == _blob(2)
    assert _token_file(tmp_path, key).read_text() == _tokens(2)
    mgr.shutdown()


async def test_failed_startup_captures_rotation_after_process_exit(tmp_path):
    key = "global:me@x.cz"
    durable = {key: _blob(1)}

    def persist(account, value, expected):
        if durable.get(account) != expected:
            return False
        durable[account] = value
        return True

    proc = _DelayedFinalWriteProc(
        _token_file(tmp_path, key), _tokens(2), delay=0.03)
    cfg = _config(tmp_path, worker_port_start=59989, worker_port_end=59989)
    mgr = workers.WorkerManager(
        cfg, GarminWorkerForward(cfg), spawn=lambda *a: proc,
        persist=persist, load=lambda account: durable.get(account))
    mgr._wait_healthy = lambda *_args: _async_value("timeout")

    with pytest.raises(workers.WorkerStartError, match="failed to become healthy"):
        await mgr.ensure_worker(key, _blob(1))
    assert proc.alive is False
    assert durable[key] == _blob(2)
    assert key not in mgr._pending_capture


async def test_repeated_cancellation_during_startup_cleanup_keeps_orphan_owned(
        tmp_path):
    key = "cn:probe@example.com"
    durable = {key: _blob(1, "cn")}
    spawned = []

    def persist(account, value, expected):
        if durable.get(account) != expected:
            return False
        durable[account] = value
        return True

    class SlowExitProc:
        def __init__(self):
            self.alive = True
            self.stop_calls = 0
            self.first_stop = asyncio.Event()
            self.second_stop = asyncio.Event()

        def poll(self):
            return None if self.alive else 0

        def terminate(self):
            self.stop_calls += 1
            if self.stop_calls == 1:
                self.first_stop.set()
            if self.stop_calls == 2:
                self.second_stop.set()

        def kill(self):
            self.alive = False

    old = SlowExitProc()

    def spawn(*_args):
        proc = old if not spawned else _StoppableProc()
        spawned.append(proc)
        return proc

    cfg = _config(tmp_path, worker_startup_timeout=30,
                  worker_port_start=59987, worker_port_end=59988)
    mgr = workers.WorkerManager(
        cfg, GarminWorkerForward(cfg), spawn=spawn,
        persist=persist, load=lambda account: durable.get(account))
    mgr._wait_healthy = lambda *_args: _async_value("timeout")

    first = asyncio.create_task(mgr.ensure_worker(key, durable[key]))
    await old.first_stop.wait()  # health timeout cleanup is inside exit wait
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert mgr._orphaned[key][0] is old
    assert old.alive and key not in mgr._workers

    durable[key] = _blob(9, "cn")  # verified browser login wins meanwhile
    second = asyncio.create_task(mgr.ensure_worker(key, durable[key]))
    await old.second_stop.wait()
    second.cancel()  # cancellation while retrying cleanup must remain owned too
    with pytest.raises(asyncio.CancelledError):
        await second
    assert mgr._orphaned[key][0] is old
    assert len(spawned) == 1

    # The old G1 worker's final G2 write lands only after both cancellations.
    _token_file(tmp_path, key).write_text(_tokens(2))
    old.alive = False
    mgr._wait_healthy = lambda *_args: _async_value("healthy")
    await mgr.ensure_worker(key, durable[key])

    assert len(spawned) == 2
    assert key not in mgr._orphaned
    assert durable[key] == _blob(9, "cn")
    assert mgr._persisted[key] == _blob(9, "cn")
    assert _token_file(tmp_path, key).read_text() == _tokens(9)
    mgr.shutdown()


def test_garmin_login_outcome_classifier(tmp_path):
    fwd = GarminWorkerForward(_config(tmp_path))
    assert fwd.login_outcome(
        "Garmin Connect client initialized successfully.") == "ok"
    assert fwd.login_outcome(
        "Garmin Connect client failed to initialize (see errors above). "
        "Tool calls will fail until this is fixed; run 'garmin-mcp-auth' "
        "and restart the server.") == "failed"
    # the pre-#255 wording (worker exits after printing it) stays covered
    assert fwd.login_outcome("Failed to initialize Garmin Connect client. Exiting.") == "failed"
    assert fwd.login_outcome("INFO: Uvicorn running on http://127.0.0.1:9000") is None
    assert fwd.login_outcome("Trying to login to Garmin Connect using token data...") is None


def test_pump_fills_login_gate_from_stream(tmp_path):
    # The wiring _default_spawn sets up: merged worker output flows through the
    # pump, and the first classified sign-in line lands in the gate exactly once.
    import io
    fwd = GarminWorkerForward(_config(tmp_path))
    gate = workers.LoginGate()
    stream = io.StringIO(
        "INFO: Started server process\n"
        "\n"
        "Trying to login to Garmin Connect using token data from directory '/x'...\n"
        "Garmin Connect client failed to initialize (see errors above).\n"
        "Garmin Connect client initialized successfully.\n"   # later line must not overwrite
    )
    workers._pump_worker_output(stream, "me@x.cz", classify=fwd.login_outcome, gate=gate)
    assert gate.outcome == "failed"


def test_default_spawn_arms_gate_only_when_forward_can_classify_login(
        tmp_path, monkeypatch):
    # Pin the actual Popen -> pump wiring as well as backward compatibility for
    # worker adapters that become healthy only after login and expose no hook.
    spawned = []
    threads = []

    class Proc:
        def __init__(self):
            self.stdout = object()

    class Thread:
        def __init__(self, *, target, args, name, daemon):
            threads.append((target, args, name, daemon))

        def start(self):
            pass

    def popen(*args, **kwargs):
        proc = Proc()
        spawned.append((proc, args, kwargs))
        return proc

    monkeypatch.setattr(workers.subprocess, "Popen", popen)
    monkeypatch.setattr(workers.threading, "Thread", Thread)
    inherited_credentials = {
        "GARMIN_EMAIL": "account-b@example.com",
        "GARMIN_PASSWORD": "account-b-password",
        "GARMIN_EMAIL_FILE": "/secrets/account-b-email",
        "GARMIN_PASSWORD_FILE": "/secrets/account-b-password",
        "GARMINTOKENS_BASE64": "account-b-token-archive",
    }
    for name, value in inherited_credentials.items():
        monkeypatch.setenv(name, value)
    cfg = _config(tmp_path)

    garmin = GarminWorkerForward(cfg)
    garmin_proc = workers.WorkerManager(cfg, garmin)._default_spawn(
        "garmin@example.com", 9000, str(tmp_path))
    assert isinstance(garmin_proc.login_gate, workers.LoginGate)
    target, args, name, daemon = threads[-1]
    assert target is workers._pump_worker_output
    assert args == (garmin_proc.stdout, "garmin@example.com",
                    garmin.login_outcome, garmin_proc.login_gate)
    assert name == "worker-log-garmin@e" and daemon is True
    garmin_env = spawned[0][2]["env"]
    assert garmin_env["GARMIN_IS_CN"] == "false"
    assert garmin_env["GARMINTOKENS"] == str(tmp_path)
    assert not inherited_credentials.keys() & garmin_env.keys()

    class ForwardWithoutLoginHook:
        def command(self):
            return ["legacy-worker"]

        def env(self, port, workdir):
            return {}

    legacy_proc = workers.WorkerManager(
        cfg, ForwardWithoutLoginHook())._default_spawn(
            "legacy@example.com", 9001, str(tmp_path))
    assert not hasattr(legacy_proc, "login_gate")
    assert threads[-1][1] == (legacy_proc.stdout, "legacy@example.com", None, None)
    # Environment filtering is Garmin-specific; generic worker adapters retain
    # the gateway environment unless they explicitly provide their own policy.
    legacy_env = spawned[1][2]["env"]
    assert all(legacy_env[name] == value
               for name, value in inherited_credentials.items())
    assert [item[0] for item in spawned] == [garmin_proc, legacy_proc]


async def test_reap_idle_terminates(tmp_path, fake_worker):
    clock = [1000.0]

    class FakeProc:
        def __init__(self): self.alive = True
        def poll(self): return None if self.alive else 0
        def terminate(self): self.alive = False

    proc = FakeProc()
    cfg = _config(tmp_path, worker_idle_ttl=10,
                  worker_port_start=fake_worker.port, worker_port_end=fake_worker.port)
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg), spawn=lambda *a: proc, clock=lambda: clock[0])
    await mgr.ensure_worker("me@x.cz", _blob(1))
    clock[0] = 1100.0                              # advance past idle ttl
    await mgr.reap_idle()
    assert proc.alive is False


async def test_reap_stop_wait_yields_to_other_gateway_work(tmp_path, fake_worker):
    clock = [1000.0]
    proc = _DelayedFinalWriteProc(
        _token_file(tmp_path), _tokens(2), delay=0.2)
    cfg = _config(tmp_path, worker_idle_ttl=10,
                  worker_port_start=fake_worker.port,
                  worker_port_end=fake_worker.port)
    mgr = workers.WorkerManager(
        cfg, GarminWorkerForward(cfg), spawn=lambda *a: proc,
        clock=lambda: clock[0])
    await mgr.ensure_worker("me@x.cz", _blob(1))
    clock[0] = 1100.0

    progressed = asyncio.Event()

    async def unrelated_gateway_request():
        await asyncio.sleep(0.01)
        progressed.set()

    other = asyncio.create_task(unrelated_gateway_request())
    reap = asyncio.create_task(mgr.reap_idle())
    await asyncio.wait_for(progressed.wait(), timeout=0.1)
    assert not reap.done()  # worker is still in its delayed TERM shutdown
    await reap
    await other
    assert proc.alive is False


async def test_reap_idle_spares_busy_worker(tmp_path, fake_worker):
    clock = [1000.0]

    class FakeProc:
        def __init__(self): self.alive = True
        def poll(self): return None if self.alive else 0
        def terminate(self): self.alive = False

    proc = FakeProc()
    cfg = _config(tmp_path, worker_idle_ttl=10,
                  worker_port_start=fake_worker.port, worker_port_end=fake_worker.port)
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg), spawn=lambda *a: proc, clock=lambda: clock[0])
    await mgr.ensure_worker("me@x.cz", _blob(1))
    mgr.request_started("me@x.cz")                 # a request is streaming
    clock[0] = 1100.0                              # past idle ttl
    await mgr.reap_idle()
    assert proc.alive is True                      # not reaped while busy
    mgr.request_finished("me@x.cz")                # refreshes last_active
    clock[0] = 1200.0                              # idle again past ttl
    await mgr.reap_idle()
    assert proc.alive is False                     # reaped once idle


async def test_enforce_cap_spares_busy_worker(tmp_path):
    cfg = _config(tmp_path, max_workers=1)
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg), spawn=lambda *a: None)

    class P:
        def __init__(self): self.killed = False
        def poll(self): return 0 if self.killed else None
        def terminate(self): self.killed = True

    busy = P()
    mgr._workers["a@x.cz"] = workers.WorkerHandle("a@x.cz", 9000, busy, 1.0, inflight=1)
    await mgr._enforce_cap()                       # at cap, but A is mid-request
    assert "a@x.cz" in mgr._workers and busy.killed is False
    mgr._workers["a@x.cz"].inflight = 0
    await mgr._enforce_cap()                       # now idle -> evictable
    assert "a@x.cz" not in mgr._workers and busy.killed is True


async def test_busy_worker_not_replaced_on_healthz_miss(tmp_path):
    # A worker mid-stream (inflight>0) whose /healthz momentarily misses (2s
    # timeout on a slow, busy worker) must NOT be terminated/replaced — that
    # would abort the live request it's serving. Keep serving it instead.
    spawned = []

    class FakeProc:
        def __init__(self): self.alive = True
        def poll(self): return None if self.alive else 0
        def terminate(self): self.alive = False

    busy = FakeProc()
    dead_port = 59998                              # nothing listening -> /healthz fails fast
    cfg = _config(tmp_path, worker_port_start=dead_port, worker_port_end=dead_port)

    def spawn(key, port, token_dir):
        spawned.append(port)
        return FakeProc()

    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg), spawn=spawn)
    mgr._workers["me@x.cz"] = workers.WorkerHandle("me@x.cz", dead_port, busy, 1.0, inflight=1)
    mgr._persisted["me@x.cz"] = _blob(1)
    port = await mgr.ensure_worker("me@x.cz", _blob(1))
    assert port == dead_port                       # reused the busy worker
    assert busy.alive is True                      # NOT terminated
    assert spawned == []                           # NOT respawned


async def test_idle_worker_replaced_on_healthz_miss(tmp_path):
    # Counterpart: an *idle* worker (inflight==0) that fails /healthz is a genuinely
    # broken worker and must be replaced.
    spawned = []

    class FakeProc:
        def __init__(self): self.alive = True
        def poll(self): return None if self.alive else 0
        def terminate(self): self.alive = False

    stale = FakeProc()
    dead_port = 59998
    cfg = _config(tmp_path, worker_port_start=dead_port, worker_port_end=dead_port,
                  worker_startup_timeout=1)

    def spawn(key, port, token_dir):
        spawned.append(port)
        return FakeProc()                          # new proc, also never healthy on dead_port

    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg), spawn=spawn)
    mgr._workers["me@x.cz"] = workers.WorkerHandle("me@x.cz", dead_port, stale, 1.0, inflight=0)
    with pytest.raises(workers.WorkerStartError):
        await mgr.ensure_worker("me@x.cz", _blob(1))
    assert stale.alive is False                    # the broken idle worker was terminated
    assert spawned == [dead_port]                  # a replacement was attempted


async def test_worker_not_reaped_during_health_check(tmp_path):
    # TOCTOU: while ensure_worker is validating an existing worker (awaiting
    # /healthz), a concurrent reap_idle must not pop it out from under the caller
    # even though it is past its idle TTL.
    clock = [1000.0]

    class FakeProc:
        def __init__(self): self.alive = True
        def poll(self): return None if self.alive else 0
        def terminate(self): self.alive = False

    proc = FakeProc()
    cfg = _config(tmp_path, worker_idle_ttl=10,
                  worker_port_start=59997, worker_port_end=59997)
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg),
                                spawn=lambda *a: proc, clock=lambda: clock[0])
    mgr._workers["me@x.cz"] = workers.WorkerHandle("me@x.cz", 59997, proc, 1000.0, inflight=0)
    mgr._persisted["me@x.cz"] = _blob(1)
    clock[0] = 2000.0                              # far past the idle TTL

    observed = {}

    async def healthy_that_triggers_reap(port):
        # Fire the reaper during the validation await, then report survival.
        await mgr.reap_idle()
        observed["survived"] = "me@x.cz" in mgr._workers
        return True

    mgr._healthy = healthy_that_triggers_reap
    port = await mgr.ensure_worker("me@x.cz", _blob(1))
    assert observed["survived"] is True            # not reaped mid-validation
    assert proc.alive is True
    assert port == 59997
    assert mgr._workers["me@x.cz"].inflight == 0   # temp hold released -> no leak


async def test_enforce_cap_counts_reserved_spawns(tmp_path):
    # An in-flight spawn holds a reserved port not yet registered in _workers; it
    # must count toward MAX_WORKERS so concurrent distinct-key spawns don't
    # overshoot the cap.
    cfg = _config(tmp_path, max_workers=2)
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg), spawn=lambda *a: None)

    class P:
        def __init__(self): self.killed = False
        def poll(self): return 0 if self.killed else None
        def terminate(self): self.killed = True

    idle = P()
    mgr._workers["a@x.cz"] = workers.WorkerHandle("a@x.cz", 9000, idle, 1.0, inflight=0)
    mgr._reserved.add(9001)                        # a distinct-key spawn in flight
    await mgr._enforce_cap()                       # 1 worker + 1 reserved == cap(2) -> free a slot
    assert "a@x.cz" not in mgr._workers and idle.killed is True


def test_alloc_port_excludes_reserved(tmp_path):
    cfg = _config(tmp_path, worker_port_start=9000, worker_port_end=9001)
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg), spawn=lambda *a: None)
    mgr._reserved.add(9000)
    assert mgr._alloc_port() == 9001               # 9000 reserved -> next free

    class P:
        def poll(self): return None

    mgr._workers["a"] = workers.WorkerHandle("a", 9001, P(), 1.0)
    with pytest.raises(workers.WorkerStartError):
        mgr._alloc_port()                          # 9000 reserved + 9001 used -> none free


async def test_materialize_tokens_sets_secure_perms(tmp_path):
    cfg = _config(tmp_path)
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg), spawn=lambda *a: None)
    token_dir = mgr._materialize("global:Me@X.cz", _blob(1))
    tok_file = os.path.join(token_dir, "garmin_tokens.json")
    marker = os.path.join(token_dir, ".garmin_region")
    assert stat.S_IMODE(os.stat(tok_file).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(marker).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(token_dir).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(os.path.dirname(token_dir)).st_mode) == 0o700


def _token_file(tmp_path, key="me@x.cz"):
    return (tmp_path / "users" / workers.account_dir_name(key) /
            "tokens" / "garmin_tokens.json")


def _pending_capture_file(tmp_path, key="me@x.cz"):
    return (_token_file(tmp_path, key).parent /
            ".missingmcp-pending-capture.json")


async def test_persist_rotated_captures_worker_rotation(tmp_path, fake_worker):
    # Garmin rotates the refresh token; the worker (garth) writes the rotation
    # to its token file. The manager must persist that back to the store —
    # otherwise the next materialize replays a spent token (the ticket-02 bug).
    persisted = []

    cfg = _config(tmp_path, worker_port_start=fake_worker.port, worker_port_end=fake_worker.port)
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg), spawn=lambda *a: _StoppableProc(),
                                persist=lambda k, b, expected: persisted.append((k, b)))
    await mgr.ensure_worker("me@x.cz", _blob(1))
    await mgr.persist_rotated()
    assert persisted == []                                # untouched file — nothing rotated
    _token_file(tmp_path).write_text(_tokens(2))           # the worker rotated its tokens
    await mgr.persist_rotated()
    assert persisted == [("me@x.cz", _blob(2))]
    await mgr.persist_rotated()
    assert persisted == [("me@x.cz", _blob(2))]           # unchanged since — no re-persist
    mgr.shutdown()


async def test_legacy_raw_global_worker_starts_and_refresh_upgrades_blob(
        tmp_path, fake_worker):
    persisted = []

    cfg = _config(tmp_path, worker_port_start=fake_worker.port,
                  worker_port_end=fake_worker.port)
    forward = GarminWorkerForward(cfg)
    mgr = workers.WorkerManager(
        cfg, forward, spawn=lambda *a: _StoppableProc(),
        persist=lambda key, value, expected: persisted.append((key, value, expected)))
    legacy = _tokens(1)
    await mgr.ensure_worker("me@x.cz", legacy)
    workdir = mgr._workdir("me@x.cz")
    assert forward.env(fake_worker.port, workdir)["GARMIN_IS_CN"] == "false"
    _token_file(tmp_path).write_text(_tokens(2))
    await mgr.persist_rotated()
    assert len(persisted) == 1
    assert persisted[0][0] == "me@x.cz" and persisted[0][2] == legacy
    assert unpack_blob(persisted[0][1]) == ("global", _tokens(2))
    mgr.shutdown()


async def test_persist_rotated_skips_torn_file_until_it_parses(tmp_path, fake_worker):
    # garth may not write atomically; a half-written file must never reach the
    # store. The next tick picks the rotation up once the file parses again.
    persisted = []

    cfg = _config(tmp_path, worker_port_start=fake_worker.port, worker_port_end=fake_worker.port)
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg), spawn=lambda *a: _StoppableProc(),
                                persist=lambda k, b, expected: persisted.append((k, b)))
    await mgr.ensure_worker("me@x.cz", _blob(1))
    _token_file(tmp_path).write_text(_tokens(2)[:-1])      # torn mid-write
    await mgr.persist_rotated()
    assert persisted == []
    _token_file(tmp_path).write_text(_tokens(2))           # write completed
    await mgr.persist_rotated()
    assert persisted == [("me@x.cz", _blob(2))]
    mgr.shutdown()


@pytest.mark.parametrize("invalid", [
    "{}",
    '{"unrelated":"value"}',
    '{"di_token":"access","di_refresh_token":"refresh"}',
    '{"di_token":"","di_refresh_token":"refresh","di_client_id":"client"}',
])
async def test_read_back_never_persists_structurally_invalid_tokens(
        tmp_path, fake_worker, invalid):
    persisted = []
    cfg = _config(tmp_path, worker_port_start=fake_worker.port,
                  worker_port_end=fake_worker.port)
    mgr = workers.WorkerManager(
        cfg, GarminWorkerForward(cfg), spawn=lambda *a: _StoppableProc(),
        persist=lambda k, b, expected: persisted.append((k, b)))
    await mgr.ensure_worker("me@x.cz", _blob(1))
    _token_file(tmp_path).write_text(invalid)
    await mgr.persist_rotated()
    assert persisted == []
    mgr.shutdown()


async def test_reap_idle_captures_last_rotation(tmp_path, fake_worker):
    # A rotation written after the last periodic tick must be captured when the
    # worker is reaped — once it leaves the registry no tick will see it again.
    persisted = []
    clock = [1000.0]

    cfg = _config(tmp_path, worker_idle_ttl=10,
                  worker_port_start=fake_worker.port, worker_port_end=fake_worker.port)
    mgr = workers.WorkerManager(
        cfg, GarminWorkerForward(cfg),
        spawn=lambda *a: _DelayedFinalWriteProc(_token_file(tmp_path), _tokens(2)),
                                clock=lambda: clock[0],
                                persist=lambda k, b, expected: persisted.append((k, b)))
    await mgr.ensure_worker("me@x.cz", _blob(1))
    clock[0] = 1100.0                                     # past the idle TTL
    await mgr.reap_idle()
    assert "me@x.cz" not in [h.key for h in mgr._workers.values()]
    assert persisted == [("me@x.cz", _blob(2))]


async def test_respawn_waits_for_final_write_after_terminate(tmp_path, fake_worker):
    persisted = []
    spawned = []
    cfg = _config(tmp_path, worker_port_start=fake_worker.port,
                  worker_port_end=fake_worker.port)

    def spawn(*_args):
        if not spawned:
            proc = _DelayedFinalWriteProc(_token_file(tmp_path), _tokens(2))
        else:
            proc = _StoppableProc()
            mgr._healthy = lambda _port: _async_value(True)
        spawned.append(proc)
        return proc

    async def unhealthy(_port):
        return False

    mgr = workers.WorkerManager(
        cfg, GarminWorkerForward(cfg), spawn=spawn,
        persist=lambda k, b, expected: persisted.append((k, b)))
    await mgr.ensure_worker("me@x.cz", _blob(1))
    mgr._healthy = unhealthy
    assert await mgr.ensure_worker("me@x.cz", _blob(1)) == fake_worker.port
    assert persisted == [("me@x.cz", _blob(2))]
    assert _token_file(tmp_path).read_text() == _tokens(2)
    mgr.shutdown()


async def test_respawn_recovers_rotation_from_dead_worker(tmp_path, fake_worker):
    # The worker rotated its tokens and then died. The caller still holds the
    # blob it read from the store BEFORE the rotation was captured — replaying
    # that spent blob is exactly the ticket-02 bug. The respawn must persist
    # the rotation and materialize IT, not the stale argument.
    persisted = []
    procs = []

    class FakeProc:
        def __init__(self): self.rc = None
        def poll(self): return self.rc
        def terminate(self): self.rc = 0
        def kill(self): self.rc = -9

    def spawn(key, port, token_dir):
        procs.append(FakeProc())
        return procs[-1]

    cfg = _config(tmp_path, worker_port_start=fake_worker.port, worker_port_end=fake_worker.port)
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg), spawn=spawn,
                                persist=lambda k, b, expected: persisted.append((k, b)))
    await mgr.ensure_worker("me@x.cz", _blob(1))
    _token_file(tmp_path).write_text(_tokens(2))           # worker rotated...
    procs[0].rc = 0                                       # ...and died
    await mgr.ensure_worker("me@x.cz", _blob(1))          # caller's blob is pre-rotation
    assert persisted == [("me@x.cz", _blob(2))]
    assert _token_file(tmp_path).read_text() == _tokens(2)
    mgr.shutdown()


@pytest.mark.parametrize("browser_relogin", [False, True])
async def test_respawn_persist_failure_never_overwrites_only_rotation(
        tmp_path, fake_worker, browser_relogin):
    key = "global:me@x.cz"
    durable = {key: _blob(1)}
    fail_once = [True]
    procs = []

    def persist(account, value, expected):
        if fail_once[0]:
            fail_once[0] = False
            raise OSError("temporary store failure")
        if durable.get(account) != expected:
            return False
        durable[account] = value
        return True

    def spawn(*_args):
        proc = _StoppableProc()
        procs.append(proc)
        return proc

    cfg = _config(tmp_path, worker_port_start=fake_worker.port,
                  worker_port_end=fake_worker.port)
    mgr = workers.WorkerManager(
        cfg, GarminWorkerForward(cfg), spawn=spawn,
        persist=persist, load=lambda account: durable.get(account))
    await mgr.ensure_worker(key, durable[key])
    _token_file(tmp_path, key).write_text(_tokens(2))
    procs[0].alive = False

    with pytest.raises(workers.WorkerStartError, match="unpersisted token rotation"):
        await mgr.ensure_worker(key, _blob(1))
    assert len(procs) == 1
    assert _token_file(tmp_path, key).read_text() == _tokens(2)
    assert _pending_capture_file(tmp_path, key).exists()

    if browser_relogin:
        durable[key] = _blob(9)
    await mgr.ensure_worker(key, _blob(1))
    expected = 9 if browser_relogin else 2
    assert durable[key] == _blob(expected)
    assert mgr._persisted[key] == _blob(expected)
    assert _token_file(tmp_path, key).read_text() == _tokens(expected)
    assert not _pending_capture_file(tmp_path, key).exists()
    mgr.shutdown()


@pytest.mark.parametrize("retirement", ["reap", "evict"])
async def test_retirement_capture_failure_is_retried_without_file_loss(
        tmp_path, fake_worker, retirement):
    key = "global:me@x.cz"
    durable = {key: _blob(1)}
    fail_once = [True]
    clock = [1000.0]

    def persist(account, value, expected):
        if fail_once[0]:
            fail_once[0] = False
            raise OSError("temporary store failure")
        if durable.get(account) != expected:
            return False
        durable[account] = value
        return True

    cfg = _config(tmp_path, max_workers=1, worker_idle_ttl=10,
                  worker_port_start=fake_worker.port,
                  worker_port_end=fake_worker.port)
    mgr = workers.WorkerManager(
        cfg, GarminWorkerForward(cfg),
        spawn=lambda *a: _DelayedFinalWriteProc(
            _token_file(tmp_path, key), _tokens(2)),
        clock=lambda: clock[0], persist=persist,
        load=lambda account: durable.get(account))
    await mgr.ensure_worker(key, durable[key])
    if retirement == "reap":
        clock[0] = 1100.0
        await mgr.reap_idle()
    else:
        mgr._reserved.add(fake_worker.port + 1)
        await mgr._enforce_cap()

    assert key not in mgr._workers
    assert key in mgr._pending_capture
    assert _token_file(tmp_path, key).read_text() == _tokens(2)
    assert _pending_capture_file(tmp_path, key).exists()
    await mgr.persist_rotated()
    assert durable[key] == _blob(2)
    assert key not in mgr._pending_capture
    assert not _pending_capture_file(tmp_path, key).exists()


async def test_shutdown_failure_is_recovered_from_private_generation_marker(
        tmp_path, fake_worker):
    key = "cn:me@x.cz"
    durable = {key: _blob(1, "cn")}
    cfg = _config(tmp_path, worker_port_start=fake_worker.port,
                  worker_port_end=fake_worker.port)
    first = workers.WorkerManager(
        cfg, GarminWorkerForward(cfg),
        spawn=lambda *a: _DelayedFinalWriteProc(
            _token_file(tmp_path, key), _tokens(2)),
        persist=lambda *_args: (_ for _ in ()).throw(
            OSError("store remains unavailable during shutdown")),
        load=lambda account: durable.get(account))
    await first.ensure_worker(key, durable[key])
    first.shutdown()
    marker = _pending_capture_file(tmp_path, key)
    assert marker.exists()
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600
    assert _token_file(tmp_path, key).read_text() == _tokens(2)

    def persist(account, value, expected):
        if durable.get(account) != expected:
            return False
        durable[account] = value
        return True

    restarted = workers.WorkerManager(
        cfg, GarminWorkerForward(cfg), spawn=lambda *a: _StoppableProc(),
        persist=persist, load=lambda account: durable.get(account))
    await restarted.ensure_worker(key, _blob(1, "cn"))
    assert durable[key] == _blob(2, "cn")
    assert restarted._persisted[key] == _blob(2, "cn")
    assert not marker.exists()
    restarted.shutdown()


async def test_stale_restart_marker_cannot_override_verified_login(
        tmp_path, fake_worker):
    key = "global:me@x.cz"
    old = _blob(1)
    verified = _blob(9)
    cfg = _config(tmp_path, worker_port_start=fake_worker.port,
                  worker_port_end=fake_worker.port)
    token_file = _token_file(tmp_path, key)
    token_file.parent.mkdir(parents=True)
    token_file.write_text(_tokens(2))
    marker = _pending_capture_file(tmp_path, key)
    marker.write_text(json.dumps({
        "v": 1,
        "expected_sha256": hashlib.sha256(old.encode()).hexdigest(),
    }))

    persisted = []
    restarted = workers.WorkerManager(
        cfg, GarminWorkerForward(cfg), spawn=lambda *a: _StoppableProc(),
        persist=lambda *args: persisted.append(args),
        load=lambda _account: verified)
    await restarted.ensure_worker(key, old)

    assert persisted == []
    assert restarted._persisted[key] == verified
    assert token_file.read_text() == _tokens(9)
    assert not marker.exists()
    restarted.shutdown()


@pytest.mark.parametrize("region", ["cn", "global"])
@pytest.mark.parametrize("marker_damage", ["missing", "opposite", "invalid"])
async def test_restart_capture_requires_db_owned_region_before_persist(
        tmp_path, fake_worker, region, marker_damage):
    key = f"{region}:me@x.cz"
    old = _blob(1, region)
    durable = {key: old}
    cfg = _config(tmp_path, worker_port_start=fake_worker.port,
                  worker_port_end=fake_worker.port)
    first = workers.WorkerManager(
        cfg, GarminWorkerForward(cfg),
        spawn=lambda *a: _DelayedFinalWriteProc(
            _token_file(tmp_path, key), _tokens(2)),
        persist=lambda *_args: (_ for _ in ()).throw(
            OSError("store unavailable during shutdown")),
        load=lambda account: durable.get(account))
    await first.ensure_worker(key, old)
    first.shutdown()

    region_file = _token_file(tmp_path, key).parent / ".garmin_region"
    if marker_damage == "missing":
        region_file.unlink()
    elif marker_damage == "opposite":
        region_file.write_text("global" if region == "cn" else "cn")
    else:
        region_file.write_text("not-a-region")

    persist_calls = []
    spawn_envs = []
    forward = GarminWorkerForward(cfg)

    def persist(account, value, expected):
        persist_calls.append((account, value, expected))
        if durable.get(account) != expected:
            return False
        durable[account] = value
        return True

    def spawn(_key, port, token_dir):
        spawn_envs.append(forward.env(port, token_dir))
        return _StoppableProc()

    restarted = workers.WorkerManager(
        cfg, forward, spawn=spawn, persist=persist,
        load=lambda account: durable.get(account))
    with pytest.raises(workers.WorkerStartError,
                       match="unpersisted token rotation"):
        await restarted.ensure_worker(key, old)

    assert persist_calls == []
    assert durable[key] == old
    assert unpack_blob(durable[key]) == (region, _tokens(1))
    assert _token_file(tmp_path, key).read_text() == _tokens(2)
    assert _pending_capture_file(tmp_path, key).exists()
    assert key in restarted._pending_capture
    assert spawn_envs == []

    # Repairing the sidecar to agree with the encrypted DB allows recovery,
    # and the spawned worker still receives that account's original region.
    region_file.write_text(region)
    await restarted.ensure_worker(key, old)
    assert unpack_blob(durable[key]) == (region, _tokens(2))
    assert spawn_envs[-1]["GARMIN_IS_CN"] == (
        "true" if region == "cn" else "false")
    assert not _pending_capture_file(tmp_path, key).exists()
    restarted.shutdown()


@pytest.mark.parametrize("damage", ["torn", "missing", "region-invalid"])
@pytest.mark.parametrize("durable_change", ["relogin", "deleted", "unchanged"])
async def test_damaged_pending_capture_obeys_current_store_generation(
        tmp_path, fake_worker, damage, durable_change):
    key = "cn:me@x.cz"
    old = _blob(1, "cn")
    durable = {key: old}
    persisted = []
    procs = []
    cfg = _config(tmp_path, worker_port_start=fake_worker.port,
                  worker_port_end=fake_worker.port)

    def persist(account, value, expected):
        persisted.append((account, value, expected))
        if durable.get(account) != expected:
            return False
        durable[account] = value
        return True

    def spawn(*_args):
        proc = _StoppableProc()
        procs.append(proc)
        return proc

    mgr = workers.WorkerManager(
        cfg, GarminWorkerForward(cfg), spawn=spawn, persist=persist,
        load=lambda account: durable.get(account))
    await mgr.ensure_worker(key, old)
    token_file = _token_file(tmp_path, key)
    region_file = token_file.parent / ".garmin_region"
    token_file.write_text(_tokens(2))
    if damage == "torn":
        token_file.write_text("{")
    elif damage == "missing":
        token_file.unlink()
    else:
        region_file.write_text("not-a-region")
    procs[0].alive = False
    await mgr.reap_idle()
    assert key in mgr._pending_capture
    assert _pending_capture_file(tmp_path, key).exists()

    if durable_change == "relogin":
        verified = _blob(9, "cn")
        durable[key] = verified
        await mgr.persist_rotated()
        assert key not in mgr._pending_capture
        await mgr.ensure_worker(key, old)
        assert durable[key] == verified
        assert unpack_blob(durable[key]) == ("cn", _tokens(9))
        assert token_file.read_text() == _tokens(9)
        assert len(procs) == 2
    elif durable_change == "deleted":
        durable.pop(key)
        await mgr.persist_rotated()
        assert key not in mgr._pending_capture
        with pytest.raises(workers.WorkerCredentialsRejected):
            await mgr.ensure_worker(key, old)
        assert key not in durable
        assert len(procs) == 1
    else:
        await mgr.persist_rotated()
        assert key in mgr._pending_capture
        with pytest.raises(workers.WorkerStartError,
                           match="unpersisted token rotation"):
            await mgr.ensure_worker(key, old)
        assert durable[key] == old
        assert key in mgr._pending_capture
        assert _pending_capture_file(tmp_path, key).exists()
        # The only potentially recoverable G2 is retained when just its region
        # sidecar is invalid; no DB change authorizes overwriting it.
        if damage == "region-invalid":
            assert token_file.read_text() == _tokens(2)

    assert persisted == []
    if durable_change != "unchanged":
        assert key not in mgr._pending_capture
        assert not _pending_capture_file(tmp_path, key).exists()
    mgr.shutdown()


async def test_stale_queued_blob_cannot_roll_back_replacement_generation(
        tmp_path, fake_worker):
    key = "global:me@x.cz"
    durable = {key: _blob(1)}
    procs = []

    def load(account):
        return durable.get(account)

    def persist(account, value, expected):
        if durable.get(account) != expected:
            return False
        durable[account] = value
        return True

    class Proc(_StoppableProc):
        def __init__(self):
            super().__init__()
            self.rc = None

        def poll(self):
            return self.rc if self.rc is not None else (
                None if self.alive else 0)

    def spawn(*_args):
        proc = Proc()
        procs.append(proc)
        return proc

    cfg = _config(tmp_path, worker_port_start=fake_worker.port,
                  worker_port_end=fake_worker.port)
    mgr = workers.WorkerManager(
        cfg, GarminWorkerForward(cfg), spawn=spawn,
        persist=persist, load=load)
    stale_request_blob = _blob(1)
    await mgr.ensure_worker(key, stale_request_blob)

    _token_file(tmp_path, key).write_text(_tokens(2))
    procs[0].rc = 0
    await mgr.ensure_worker(key, stale_request_blob)
    assert durable[key] == _blob(2)
    assert mgr._persisted[key] == _blob(2)

    # The replacement advances again while another request is still carrying
    # G1. Acquiring the account lock must reload durable G2, reuse the current
    # worker, and leave its G3 file intact.
    _token_file(tmp_path, key).write_text(_tokens(3))
    await mgr.ensure_worker(key, stale_request_blob)
    assert _token_file(tmp_path, key).read_text() == _tokens(3)
    assert len(procs) == 2
    await mgr.persist_rotated()
    assert durable[key] == _blob(3)
    assert mgr._persisted[key] == _blob(3)
    mgr.shutdown()


async def test_cas_rejection_reloads_winning_generation_before_materialize(
        tmp_path, fake_worker):
    key = "global:me@x.cz"
    durable = {key: _blob(1)}
    procs = []
    cas_calls = []

    def persist(account, value, expected):
        cas_calls.append((account, value, expected))
        # Simulate a verified browser login winning after the manager's first
        # authoritative read but before the dead worker's rotation CAS.
        durable[account] = _blob(9)
        return False

    def spawn(*_args):
        proc = _StoppableProc()
        procs.append(proc)
        return proc

    cfg = _config(tmp_path, worker_port_start=fake_worker.port,
                  worker_port_end=fake_worker.port)
    mgr = workers.WorkerManager(
        cfg, GarminWorkerForward(cfg), spawn=spawn, persist=persist,
        load=lambda account: durable.get(account))
    await mgr.ensure_worker(key, _blob(1))
    _token_file(tmp_path, key).write_text(_tokens(2))
    procs[0].alive = False

    await mgr.ensure_worker(key, _blob(1))
    assert cas_calls == [(key, _blob(2), _blob(1))]
    assert durable[key] == _blob(9)
    assert mgr._persisted[key] == _blob(9)
    assert _token_file(tmp_path, key).read_text() == _tokens(9)
    mgr.shutdown()


async def test_authoritative_browser_relogin_beats_old_worker_rotation(
        tmp_path, fake_worker):
    key = "global:me@x.cz"
    durable = {key: _blob(1)}
    persisted = []
    procs = []

    def persist(account, value, expected):
        persisted.append((account, value, expected))
        return False

    def spawn(*_args):
        proc = _StoppableProc()
        procs.append(proc)
        return proc

    cfg = _config(tmp_path, worker_port_start=fake_worker.port,
                  worker_port_end=fake_worker.port)
    mgr = workers.WorkerManager(
        cfg, GarminWorkerForward(cfg), spawn=spawn, persist=persist,
        load=lambda account: durable.get(account))
    await mgr.ensure_worker(key, _blob(1))
    _token_file(tmp_path, key).write_text(_tokens(99))
    durable[key] = _blob(2)  # verified browser login outside the manager lock

    await mgr.ensure_worker(key, _blob(1))
    assert procs[0].alive is False
    assert persisted == []
    assert durable[key] == _blob(2)
    assert mgr._persisted[key] == _blob(2)
    assert _token_file(tmp_path, key).read_text() == _tokens(2)
    mgr.shutdown()


async def test_deleted_account_then_same_key_login_retires_old_worker(
        tmp_path, fake_worker):
    """A queued deletion observation must not erase the live worker baseline.

    This is the exact delete -> queued request -> same-key re-login sequence
    that previously reused the already-authenticated G1 process for G9.
    """
    key = "cn:me@x.cz"
    secret = "s" * 40
    conn = store.init_db(str(tmp_path / "state.db"))
    old = _blob(1, "cn")
    relogin = _blob(9, "cn")
    store.upsert_account(conn, "garmin", key, old, secret)
    old_bearer = "old-device-bearer"
    old_hash = store.hash_token(old_bearer)
    store.create_access_token(conn, old_hash, "garmin", key, "client")
    procs = []

    def spawn(*_args):
        proc = _StoppableProc()
        procs.append(proc)
        return proc

    def load(account):
        return store.get_account_tokens(conn, "garmin", account, secret)

    def persist(account, value, expected):
        return store.update_account_if_matches(
            conn, "garmin", account, expected, value, secret)

    cfg = _config(tmp_path, worker_port_start=fake_worker.port,
                  worker_port_end=fake_worker.port)
    mgr = workers.WorkerManager(
        cfg, GarminWorkerForward(cfg), spawn=spawn,
        persist=persist, load=load)
    await mgr.ensure_worker(key, old)
    assert mgr._persisted[key] == old

    # Hold the account lock so this request has captured G1 but only observes
    # durable state after the account and its bearer have been revoked.
    await mgr._locks[key].acquire()
    queued = asyncio.create_task(mgr.ensure_worker(key, old))
    await asyncio.sleep(0)
    store.revoke_account(conn, "garmin", key)
    store.delete_account(conn, "garmin", key)
    mgr._locks[key].release()
    with pytest.raises(workers.WorkerCredentialsRejected):
        await queued

    assert procs[0].alive is True
    assert mgr._persisted[key] == old
    assert store.account_key_for_token_hash(conn, old_hash) is None

    store.upsert_account(conn, "garmin", key, relogin, secret)
    await mgr.ensure_worker(key, relogin)
    assert procs[0].alive is False
    assert len(procs) == 2
    assert mgr._persisted[key] == relogin
    assert _token_file(tmp_path, key).read_text() == _tokens(9)

    # The replacement owns the new CAS baseline and can persist its refresh.
    _token_file(tmp_path, key).write_text(_tokens(10))
    await mgr.persist_rotated()
    assert store.get_account_tokens(
        conn, "garmin", key, secret) == _blob(10, "cn")
    assert mgr._persisted[key] == _blob(10, "cn")
    mgr.shutdown()
    conn.close()


async def test_relogin_never_routes_new_request_through_busy_old_worker(
        tmp_path, fake_worker):
    durable = {"global:me@x.cz": _blob(1)}
    proc = _StoppableProc()
    cfg = _config(tmp_path, worker_port_start=fake_worker.port,
                  worker_port_end=fake_worker.port)
    mgr = workers.WorkerManager(
        cfg, GarminWorkerForward(cfg), spawn=lambda *_args: proc,
        load=lambda account: durable.get(account))
    await mgr.ensure_worker("global:me@x.cz", durable["global:me@x.cz"])
    mgr.request_started("global:me@x.cz")
    durable["global:me@x.cz"] = _blob(2)

    with pytest.raises(workers.WorkerStartError, match="draining old credentials"):
        await mgr.ensure_worker("global:me@x.cz", durable["global:me@x.cz"])

    assert proc.alive is True
    assert mgr._workers["global:me@x.cz"].inflight == 1
    mgr.request_finished("global:me@x.cz")
    mgr.shutdown()


async def test_verified_relogin_replaces_old_worker_without_reading_it_back(
        tmp_path, fake_worker):
    persisted = []
    procs = []

    class FakeProc:
        def __init__(self):
            self.alive = True
        def poll(self):
            return None if self.alive else 0
        def terminate(self):
            self.alive = False

    def spawn(*_args):
        proc = FakeProc()
        procs.append(proc)
        return proc

    cfg = _config(tmp_path, worker_port_start=fake_worker.port,
                  worker_port_end=fake_worker.port)
    mgr = workers.WorkerManager(
        cfg, GarminWorkerForward(cfg), spawn=spawn,
        persist=lambda key, value, expected: persisted.append((key, value, expected)))
    await mgr.ensure_worker("global:me@x.cz", _blob(1))
    _token_file(tmp_path, "global:me@x.cz").write_text(_tokens(99))

    await mgr.ensure_worker("global:me@x.cz", _blob(2))

    assert procs[0].alive is False
    assert persisted == []                       # old rotation never overwrote re-login
    assert _token_file(tmp_path, "global:me@x.cz").read_text() == _tokens(2)
    mgr.shutdown()


async def test_same_email_regions_have_independent_workers_and_files(tmp_path):
    cfg = _config(tmp_path, worker_port_start=59100, worker_port_end=59101)
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg),
                                spawn=lambda *a: _StoppableProc())

    async def healthy(_port):
        return True

    mgr._healthy = healthy
    cn_port = await mgr.ensure_worker("cn:me@x.cz", _blob(1, "cn"))
    global_port = await mgr.ensure_worker("global:me@x.cz", _blob(2, "global"))
    assert cn_port != global_port
    assert mgr._workdir("cn:me@x.cz") != mgr._workdir("global:me@x.cz")
    assert (_token_file(tmp_path, "cn:me@x.cz").parent / ".garmin_region").read_text() == "cn"
    assert (_token_file(tmp_path, "global:me@x.cz").parent / ".garmin_region").read_text() == "global"
    mgr.shutdown()


async def test_stale_worker_refresh_is_rejected_after_store_changes(tmp_path, fake_worker):
    calls = []

    def reject_cas(key, value, expected):
        calls.append((key, value, expected))
        return False

    cfg = _config(tmp_path, worker_port_start=fake_worker.port,
                  worker_port_end=fake_worker.port)
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg),
                                spawn=lambda *a: _StoppableProc(), persist=reject_cas)
    old = _blob(1)
    await mgr.ensure_worker("global:me@x.cz", old)
    _token_file(tmp_path, "global:me@x.cz").write_text(_tokens(2))
    await mgr.persist_rotated()
    assert calls == [("global:me@x.cz", _blob(2), old)]
    assert mgr._persisted["global:me@x.cz"] == old
    mgr.shutdown()


async def test_fresh_manager_trusts_store_over_disk(tmp_path, fake_worker):
    # After a process restart the manager has no baseline: a differing file may
    # be an old generation, not a rotation — e.g. the user re-signed in while
    # the process was down. The store must win; repairing pre-fix drift is the
    # explicit backfill's job (reliability ticket 05), never this path's.
    persisted = []

    cfg = _config(tmp_path, worker_port_start=fake_worker.port, worker_port_end=fake_worker.port)
    _token_file(tmp_path).parent.mkdir(parents=True)
    _token_file(tmp_path).write_text(_tokens(1))
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg),
                                spawn=lambda *a: _StoppableProc(),
                                persist=lambda k, b, expected: persisted.append((k, b)))
    await mgr.ensure_worker("me@x.cz", _blob(2))
    assert persisted == []
    assert _token_file(tmp_path).read_text() == _tokens(2)
    mgr.shutdown()


async def test_shutdown_captures_rotations(tmp_path, fake_worker):
    # Deploys are frequent: a rotation written since the last tick must survive
    # the restart, or the next boot materializes a spent token from the store.
    persisted = []

    cfg = _config(tmp_path, worker_port_start=fake_worker.port, worker_port_end=fake_worker.port)
    mgr = workers.WorkerManager(
        cfg, GarminWorkerForward(cfg),
        spawn=lambda *a: _DelayedFinalWriteProc(_token_file(tmp_path), _tokens(2)),
                                persist=lambda k, b, expected: persisted.append((k, b)))
    await mgr.ensure_worker("me@x.cz", _blob(1))
    mgr.shutdown()
    assert persisted == [("me@x.cz", _blob(2))]


async def test_evicted_worker_rotation_is_captured(tmp_path, fake_worker):
    # An eviction (cap pressure) forgets the worker just like a reap does — its
    # last rotation must be captured on the way out.
    persisted = []

    cfg = _config(tmp_path, max_workers=1,
                  worker_port_start=fake_worker.port, worker_port_end=fake_worker.port)
    mgr = workers.WorkerManager(
        cfg, GarminWorkerForward(cfg),
        spawn=lambda *a: _DelayedFinalWriteProc(_token_file(tmp_path), _tokens(2)),
                                persist=lambda k, b, expected: persisted.append((k, b)))
    await mgr.ensure_worker("me@x.cz", _blob(1))
    mgr._reserved.add(fake_worker.port + 1)               # a distinct-key spawn in flight
    await mgr._enforce_cap()                              # cap reached -> evict me@x.cz
    assert "me@x.cz" not in mgr._workers
    assert persisted == [("me@x.cz", _blob(2))]


async def test_read_back_error_does_not_break_the_batch(tmp_path):
    # The contract doesn't promise read_back never raises, and the capture
    # points are batch contexts (periodic tick, eviction inside another
    # account's spawn, shutdown) — one account's disk problem must be logged
    # and skipped, never propagated into the batch.
    persisted = []

    class ExplodingReadBack(GarminWorkerForward):
        def read_back(self, workdir):
            if workers.account_dir_name("a@x.cz") in workdir:
                raise RuntimeError("disk went away")
            return super().read_back(workdir)

    cfg = _config(tmp_path, worker_port_start=59900, worker_port_end=59901)
    mgr = workers.WorkerManager(cfg, ExplodingReadBack(cfg),
                                spawn=lambda *a: _StoppableProc(),
                                persist=lambda k, b, expected: persisted.append((k, b)))

    async def always_healthy(port):
        return True

    mgr._healthy = always_healthy
    await mgr.ensure_worker("a@x.cz", _blob(1))
    await mgr.ensure_worker("b@x.cz", _blob(1))
    _token_file(tmp_path, "a@x.cz").write_text(_tokens(2))
    _token_file(tmp_path, "b@x.cz").write_text(_tokens(2))
    await mgr.persist_rotated()                           # must not raise
    assert persisted == [("b@x.cz", _blob(2))]            # A skipped, B still captured
    mgr.shutdown()                                        # must not raise either


def test_read_back_returns_current_token_file(tmp_path):
    # The worker (garth) rewrites garmin_tokens.json when Garmin rotates the
    # refresh token; read_back is how the gateway learns the current content.
    cfg = _config(tmp_path)
    fwd = GarminWorkerForward(cfg)
    assert fwd.read_back(str(tmp_path)) is None            # no file yet
    fwd.materialize(pack_blob(_tokens(1), "cn"), str(tmp_path))
    assert unpack_blob(fwd.read_back(str(tmp_path))) == ("cn", _tokens(1))
    # garth may not write atomically — a torn (unparseable) file must never
    # be persisted; report "nothing to read" and let the next tick retry.
    (tmp_path / "garmin_tokens.json").write_text(_tokens(1)[:-1])
    assert fwd.read_back(str(tmp_path)) is None


def test_account_workdirs_do_not_collide(tmp_path):
    cfg = _config(tmp_path)
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg))
    pairs = [
        ("a+b@example.com", "a_b@example.com"),
        ("cn:person@example.com", "cn_person@example.com"),
        ("global:person@example.com", "global_person@example.com"),
    ]
    for left, right in pairs:
        assert mgr._workdir(left) != mgr._workdir(right)


def test_global_worker_overrides_parent_cn_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("GARMIN_IS_CN", "true")
    fwd = GarminWorkerForward(_config(tmp_path))
    fwd.materialize(_blob(1, "global"), str(tmp_path))
    assert fwd.env(9000, str(tmp_path))["GARMIN_IS_CN"] == "false"


def test_cn_region_marker_tampering_fails_closed(tmp_path):
    fwd = GarminWorkerForward(_config(tmp_path))
    fwd.materialize(_blob(1, "cn"), str(tmp_path))
    (tmp_path / ".garmin_region").write_text("global")
    with pytest.raises(ValueError):
        fwd.env(9000, str(tmp_path))
    assert fwd.read_back(str(tmp_path)) is None


async def test_manager_delegates_to_forward(tmp_path, fake_worker):
    calls = []

    class FakeForward:
        def command(self):
            return ["fake-worker"]
        def env(self, port, workdir):
            calls.append(("env", port, workdir))
            return {"FAKE": "1"}
        def materialize(self, blob, workdir):
            calls.append(("materialize", blob, workdir))

    cfg = _config(tmp_path, worker_port_start=fake_worker.port, worker_port_end=fake_worker.port)
    mgr = workers.WorkerManager(cfg, FakeForward(), spawn=lambda *a: _StoppableProc())
    await mgr.ensure_worker("me@x.cz", '{"blob":1}')
    assert ("materialize", '{"blob":1}', calls[0][2]) == calls[0]   # forward wrote the credentials
    assert calls[0][2].endswith("/tokens")                          # into the manager-owned workdir
    mgr.shutdown()


def test_pump_demotes_routine_stream_teardown_line(capsys):
    # The worker's uvicorn prints "ASGI callable returned without completing
    # response" on every routine MCP session teardown (the client hung up its
    # listen stream) — a real fault it is not, so it must stay info despite
    # matching the deliberately-loose _WORKER_ERROR filter (reliability 10).
    import json as jsonlib
    workers._pump_worker_output(iter([
        "ERROR:    ASGI callable returned without completing response.\n",
        "ERROR: something actually broke\n",
    ]), "me@x.cz")
    events = [jsonlib.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    levels = {e["line"][:22]: e["level"] for e in events if e["event"] == "worker-log"}
    assert levels["ERROR:    ASGI callabl"] == "info"
    assert levels["ERROR: something actua"] == "error"


# --- port hygiene: never hand a freed port to the next spawn while its ------
# --- previous owner may still be dying (reliability tickets 12/14) ----------

class LingeringProc:
    """SIGTERM was sent but the process is still shutting down — the state in
    which a real worker's uvicorn keeps answering /healthz for a moment."""
    def __init__(self):
        self.terminated = False
        self.killed = False
        self.dead = False
    def poll(self): return 0 if self.dead else None
    def terminate(self): self.terminated = True
    def kill(self):
        self.killed = True
        self.dead = True


def test_alloc_port_round_robins(tmp_path):
    # Lowest-free-first hands the next spawn exactly the port the eviction it
    # just triggered freed up; a rotating cursor spaces reuses out instead.
    cfg = _config(tmp_path, worker_port_start=9000, worker_port_end=9002)
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg), spawn=lambda *a: None)
    assert mgr._alloc_port() == 9000
    assert mgr._alloc_port() == 9001               # advanced, though 9000 is free
    assert mgr._alloc_port() == 9002
    assert mgr._alloc_port() == 9000               # wraps


def test_stop_waits_for_confirmed_exit_before_port_reuse(tmp_path):
    import threading

    cfg = _config(tmp_path, worker_port_start=9000, worker_port_end=9000)
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg), spawn=lambda *a: None)

    class DelayedExitProc(LingeringProc):
        def terminate(self):
            super().terminate()
            threading.Timer(0.05, lambda: setattr(self, "dead", True)).start()

    proc = DelayedExitProc()
    h = workers.WorkerHandle("a@x.cz", 9000, proc, 1000.0)
    mgr._workers["a@x.cz"] = h
    t0 = time.monotonic()
    assert mgr._stop_and_wait(h)
    assert time.monotonic() - t0 >= 0.04
    mgr._workers.pop("a@x.cz")
    assert mgr._alloc_port() == 9000


def test_stop_escalates_to_kill_and_confirms(tmp_path, monkeypatch):
    monkeypatch.setattr(workers, "_COOLING_KILL_S", 0.01)
    cfg = _config(tmp_path, worker_port_start=9000, worker_port_end=9000)
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg), spawn=lambda *a: None)
    proc = LingeringProc()
    h = workers.WorkerHandle("a@x.cz", 9000, proc, 1.0)
    assert mgr._stop_and_wait(h)
    assert proc.terminated and proc.killed and proc.dead


def test_unconfirmed_stop_keeps_port_reserved(tmp_path, monkeypatch):
    monkeypatch.setattr(workers, "_COOLING_KILL_S", 0.01)
    monkeypatch.setattr(workers, "_KILL_CONFIRM_S", 0.01)
    cfg = _config(tmp_path, worker_port_start=9000, worker_port_end=9000)
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg), spawn=lambda *a: None)
    proc = LingeringProc()
    proc.kill = lambda: setattr(proc, "killed", True)
    h = workers.WorkerHandle("a@x.cz", 9000, proc, 1.0)
    assert not mgr._stop_and_wait(h)
    with pytest.raises(workers.WorkerStartError):
        mgr._alloc_port()


async def test_spawn_not_validated_against_unconfirmed_predecessor(tmp_path, fake_worker,
                                                                   monkeypatch):
    # THE ticket-12 regression: account A's evicted worker still answers
    # /healthz on its port while dying. A spawn for account B must not be
    # handed that port — the old code validated B's half-booted worker against
    # A's dying listener ("worker-started ms=6") and the forward then hit a
    # dead port (ConnectError -> 502).
    monkeypatch.setattr(workers, "_COOLING_KILL_S", 0.01)
    monkeypatch.setattr(workers, "_KILL_CONFIRM_S", 0.01)
    cfg = _config(tmp_path, worker_port_start=fake_worker.port,
                  worker_port_end=fake_worker.port + 1, worker_startup_timeout=5)
    mgr = workers.WorkerManager(cfg, GarminWorkerForward(cfg),
                                spawn=lambda *_args: _StoppableProc())
    dying = LingeringProc()
    dying.kill = lambda: setattr(dying, "killed", True)
    h = workers.WorkerHandle("a@x.cz", fake_worker.port, dying, 1.0)
    mgr._workers["a@x.cz"] = h
    mgr._persisted["a@x.cz"] = _blob(1)
    with pytest.raises(workers.WorkerStartError, match="did not stop"):
        await mgr.ensure_worker("a@x.cz", _blob(2))
    assert mgr._workers["a@x.cz"] is h


async def test_unconfirmed_failed_start_blocks_token_directory_reuse(
        tmp_path, monkeypatch):
    monkeypatch.setattr(workers, "_COOLING_KILL_S", 0.01)
    monkeypatch.setattr(workers, "_KILL_CONFIRM_S", 0.01)

    class UnstoppableProc:
        def poll(self): return None
        def terminate(self): pass
        def kill(self): pass

    proc = UnstoppableProc()
    cfg = _config(tmp_path, worker_port_start=59991, worker_port_end=59991)
    mgr = workers.WorkerManager(
        cfg, GarminWorkerForward(cfg), spawn=lambda *_args: proc)
    mgr._wait_healthy = lambda *_args: _async_value("timeout")

    with pytest.raises(workers.WorkerStartError, match="did not stop"):
        await mgr.ensure_worker("global:a@x.cz", _blob(1))
    token_file = _token_file(tmp_path, "global:a@x.cz")
    token_file.write_text(_tokens(9))

    with pytest.raises(workers.WorkerStartError, match="refusing to reuse"):
        await mgr.ensure_worker("global:a@x.cz", _blob(2))
    assert token_file.read_text() == _tokens(9)
