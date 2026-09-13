from __future__ import annotations
import asyncio
import hashlib
import json
import os
import re
import subprocess
import tempfile
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
import httpx
from .log import log, log_error, log_exc

_SAFE = re.compile(r"[^A-Za-z0-9_.@-]")


def account_dir_name(key: str) -> str:
    """Collision-resistant, filesystem-safe directory for an opaque account key."""
    return "v2-" + hashlib.sha256(key.encode("utf-8")).hexdigest()

# A freed port stays out of _alloc_port until its previous owner is observed
# dead. Escalate to SIGKILL if SIGTERM is ignored. An unconfirmed process keeps
# its port reserved indefinitely: reusing it would risk routing
# a new account to an old listener.
_COOLING_KILL_S = 5.0
_KILL_CONFIRM_S = 1.0
_PENDING_CAPTURE_FILE = ".missingmcp-pending-capture.json"
# Worker lines that indicate a real problem get error severity so Railway
# surfaces them; everything else is info. Deliberately loose — false negatives
# just stay info-level and remain searchable.
_WORKER_ERROR = re.compile(r"\b(ERROR|CRITICAL|Traceback|Exception)\b")
# The one deliberate exception to that loose filter: the worker's uvicorn
# prints this on every routine MCP session teardown (the client hung up its
# open listen stream, the gateway stopped reading) — not a fault, and at
# production volume it alone kept the pager loud (reliability ticket 10).
_WORKER_ROUTINE = "ASGI callable returned without completing response"


def _pump_worker_output(stream, account: str, classify=None, gate=None) -> None:
    """Forward a worker's merged stdout/stderr line-by-line into the structured
    log (event `worker-log`, filterable by account in Railway). Runs on a daemon
    thread until the pipe closes; replaces the old per-user worker.log files on
    the volume (unbounded, only reachable over ssh). When the forward strategy
    can classify sign-in log lines (`classify`), the first classified line fills
    the spawn's LoginGate — ensure_worker's login gate blocks on it."""
    try:
        for raw in stream:
            line = raw.rstrip()
            if not line:
                continue
            elevated = _WORKER_ERROR.search(line) and _WORKER_ROUTINE not in line
            emit = log_error if elevated else log
            emit("worker-log", account=account, line=line)
            if gate is not None and gate.outcome is None:
                outcome = classify(line)
                if outcome is not None:
                    gate.outcome = outcome
    except Exception:  # noqa: BLE001 - a logging pump must never take anything down
        pass
    finally:
        try:
            stream.close()
        except Exception:  # noqa: BLE001
            pass


class WorkerStartError(Exception):
    """The worker could not be brought up and an operator may need to look:
    the spawn itself failed, no port was free, or the process stayed alive but
    never answered /healthz within `worker_startup_timeout`."""


class WorkerCredentialsRejected(WorkerStartError):
    """The worker came up and decided it can't serve this account — the stored
    tokens went stale. Expected and self-healing: the account needs a fresh
    sign-in, not an operator. Kept a subclass of WorkerStartError so any
    `except WorkerStartError` still catches it and the caller's re-auth handling
    stays a single path.

    Two signals mean this, matching two generations of `garmin_mcp`: a *clean*
    exit (rc 0) during startup — "OAuth tokens not found ... Exiting." before
    the worker logged in ahead of serving — and, since the login moved to a
    background thread (garmin_mcp #255), a "failed to initialize" log line from
    a worker that keeps running and answers /healthz regardless (the login gate,
    `_wait_login`).

    A non-zero or signalled exit is deliberately NOT this — that's a crash, and
    it stays a plain WorkerStartError so it keeps reaching the ops alert."""


class LoginGate:
    """One-shot, per-spawn slot the output pump fills with the worker's sign-in
    outcome ("ok"/"failed") when the forward strategy can classify its log lines
    (`forward.login_outcome`). Needed because the worker answers /healthz before
    its background Garmin sign-in has resolved (garmin_mcp #255) — health alone
    no longer proves the account is serviceable, and without the gate a stale
    token surfaces as a confusing per-call tool error instead of a re-auth 401."""

    __slots__ = ("outcome",)

    def __init__(self):
        self.outcome: str | None = None


@dataclass
class WorkerHandle:
    key: str
    port: int
    process: object
    last_active: float
    inflight: int = 0          # proxied requests currently streaming through


class WorkerManager:
    def __init__(self, config, forward, spawn=None, clock=time.monotonic, persist=None,
                 load=None):
        self._cfg = config
        self._forward = forward
        self._clock = clock
        self._spawn_fn = spawn or self._default_spawn
        # (key, new_blob, expected_blob) -> bool; compare-and-swap store write
        self._persist = persist
        # key -> current durable blob. When supplied, the request's blob is only
        # a hint: it may have waited outside this manager's per-account lock.
        self._load = load
        self._workers: dict[str, WorkerHandle] = {}
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._reserved: set[int] = set()   # ports being spawned but not yet registered
        self._port_cursor = config.worker_port_start
        # Ports of terminated workers, held until their process is observed
        # dead: a SIGTERMed uvicorn still answers /healthz for a moment, and
        # validating a fresh spawn against its predecessor's listener hands
        # the forward a dead port (reliability ticket 12).
        self._cooling: dict[int, tuple] = {}   # port -> (process, since)
        # A process that failed during startup is not in _workers. If its exit
        # cannot be confirmed, retain it by account so a later request cannot
        # rematerialize the same token directory while it may still be writing.
        self._orphaned: dict[str, tuple[object, int]] = {}
        # Stopped startup workers whose final rotation hit a temporary persist
        # failure. Never rematerialize their workdir until capture is retried.
        self._pending_capture: set[str] = set()
        # Last blob known to be in the store, per account — the baseline a
        # worker-rewritten token file is compared against. Process-local: after
        # a restart the first materialize re-seeds it from the store's blob.
        self._persisted: dict[str, str] = {}

    # --- public ---------------------------------------------------------

    async def ensure_worker(self, key: str, blob: str) -> int:
        async with self._locks[key]:
            orphan = self._orphaned.get(key)
            if orphan is not None:
                proc, orphan_port = orphan
                if not await self._stop_spawned_async(key, proc, orphan_port, "retry"):
                    raise WorkerStartError(
                        f"worker for {key[:3]}*** did not stop; refusing to reuse token files")
            # Compare pending capture ownership with durable state *before*
            # trying to parse the old disk file. A verified re-login or account
            # deletion invalidates that capture even when the old file is torn,
            # missing or has a damaged sidecar and can never reach the CAS.
            blob = self._authoritative_blob(key, blob)
            self._discard_stale_pending_capture(key, blob)
            if key in self._pending_capture:
                self._read_back_and_persist(
                    key, "startup-capture-retry", retry_on_failure=True)
                if key in self._pending_capture:
                    raise WorkerStartError(
                        f"worker for {key[:3]}*** has an unpersisted token rotation")
                # A successful retry may have advanced the store generation.
                blob = self._authoritative_blob(key, blob)
            # Re-read only after acquiring the key lock. A proxy request may
            # have queued with an older generation while another worker or
            # browser login advanced the durable account blob.
            blob = self._recover_durable_capture(key, blob)
            h = self._workers.get(key)
            last = self._persisted.get(key)
            # A live worker without a trusted baseline is never reusable. This
            # can happen when a queued request observes account deletion and a
            # same-key login is then created before the old process exits.
            # Treat the missing baseline as a credential-generation change.
            credentials_changed = h is not None and (last is None or blob != last)
            if h is not None and h.process.poll() is None and not credentials_changed:
                # Hold the worker busy across the awaited /healthz probe so a
                # concurrent reap_idle / _enforce_cap (neither takes this key's
                # lock) can't evict it during the yield (TOCTOU). Always released
                # in `finally`, so no accounting is leaked.
                h.inflight += 1
                try:
                    healthy = await self._healthy(h.port)
                finally:
                    h.inflight -= 1
                # Reuse when healthy, OR when a request is still streaming through
                # it: a momentarily slow /healthz on a busy worker must not kill
                # the live stream (mirrors the inflight guard in reap_idle /
                # _enforce_cap). Only an idle *and* unhealthy worker is replaced.
                if healthy or h.inflight > 0:
                    h.last_active = self._clock()
                    return h.port
            if h is not None:
                # Never replace a busy worker underneath an active stream. The
                # next request must retry after the in-flight request completes;
                # it must not be routed through the old credential generation.
                if credentials_changed and h.inflight > 0:
                    raise WorkerStartError(
                        f"worker for {key[:3]}*** is draining old credentials")
                if not await self._stop_and_wait_async(h):
                    raise WorkerStartError(
                        f"worker for {key[:3]}*** did not stop; refusing to replace credentials")
                self._workers.pop(key, None)
            await self._enforce_cap()
            # The previous worker may have rotated its tokens after the caller
            # read `blob` from the store (dead worker, or a replace) — persist
            # the rotation and materialize IT; writing the stale argument would
            # replay a token the upstream already retired.
            if credentials_changed:
                # The store now contains a verified browser re-login. Never
                # read the old worker generation back over it.
                self._persisted.pop(key, None)
                rotated = None
            else:
                rotated = self._read_back_and_persist(
                    key, "respawn", retry_on_failure=True)
                if key in self._pending_capture:
                    raise WorkerStartError(
                        f"worker for {key[:3]}*** has an unpersisted token rotation")
            if rotated is not None:
                blob = rotated
            # A CAS rejection means another writer won after our first load.
            # Refresh again before touching disk; stale request state is never
            # authoritative while a loader is available.
            blob = self._authoritative_blob(key, blob)
            token_dir = self._materialize(key, blob)
            # Reserve the port across the awaited spawn/health-check. Without this,
            # a concurrent ensure_worker for a *different* key (own lock) would see
            # the same lowest free port — _alloc_port reads _workers, which isn't
            # updated until after the awaits below — and collide (EADDRINUSE).
            port = self._alloc_port()
            self._reserved.add(port)
            try:
                t0 = self._clock()
                log("worker-spawn", port=port, account=key,
                    cmd=" ".join(self._forward.command()), token_dir=token_dir)
                try:
                    proc = self._spawn_fn(key, port, token_dir)
                except Exception as e:  # noqa: BLE001 - spawn failed (e.g. binary not on PATH)
                    log_exc("worker-spawn-failed", e, error=str(e),
                            cmd=" ".join(self._forward.command()))
                    raise WorkerStartError(f"spawn failed: {type(e).__name__}") from e
                # One budget for the whole boot — health AND sign-in outcome —
                # so the bump to background login didn't widen the startup SLA.
                deadline = self._clock() + self._cfg.worker_startup_timeout
                try:
                    outcome = await self._wait_healthy(port, proc, deadline)
                    gate = getattr(proc, "login_gate", None)
                    if outcome == "healthy" and gate is not None:
                        login = await self._wait_login(gate, proc, deadline)
                        if login == "failed":
                            if not await self._stop_spawned_async(
                                    key, proc, port, "login-rejected"):
                                raise WorkerStartError(
                                    f"worker for {key[:3]}*** did not stop after login rejection")
                            log("worker-login-rejected", port=port, account=key)
                            raise WorkerCredentialsRejected(
                                f"worker for {key[:3]}*** reported a failed sign-in during startup")
                        if login == "timeout":
                            if not await self._stop_spawned_async(
                                    key, proc, port, "login-timeout"):
                                raise WorkerStartError(
                                    f"worker for {key[:3]}*** did not stop after login timeout")
                            log("worker-login-timeout", port=port,
                                startup_timeout=self._cfg.worker_startup_timeout)
                            raise WorkerStartError(
                                f"worker for {key[:3]}*** did not resolve its sign-in in time")
                        if login == "exited":
                            outcome = "exited"   # shared exit handling below
                except asyncio.CancelledError:
                    # The caller's request vanished mid-boot (a client disconnect
                    # cancels the handler task). The process is not yet registered,
                    # and `finally` below un-reserves its port — left running it
                    # would hold a port the allocator considers free. Stop it so
                    # the port cools down like every other terminated worker's.
                    await self._stop_spawned_async(
                        key, proc, port, "startup-cancelled")
                    raise
                if outcome != "healthy":
                    rc = proc.poll()
                    # Confirm a bound-but-unhealthy process is gone before its
                    # workdir or port can be reused.
                    if not await self._stop_spawned_async(
                            key, proc, port, "startup-failed"):
                        raise WorkerStartError(
                            f"worker for {key[:3]}*** did not stop after startup failure")
                    # Two very different failures used to share one event (and one
                    # error-level alert): a worker that quit by itself because the
                    # account's credentials are stale — routine, the user fixes it
                    # by signing in again — and a worker that broke. Keep them apart
                    # so the noisy one can't drown out the one worth waking up for.
                    #
                    # Only a CLEAN exit is the routine one: garmin_mcp prints
                    # "OAuth tokens not found ... Exiting." and returns 0. A non-zero
                    # or signalled exit (crash, rc=137 OOM kill, segfault) is a real
                    # fault and must stay loud — filing it as stale credentials would
                    # hide exactly the outage this split exists to surface.
                    if outcome == "exited" and rc == 0:
                        log("worker-exited-early", port=port, returncode=rc, account=key)
                        raise WorkerCredentialsRejected(
                            f"worker for {key[:3]}*** exited during startup (rc={rc})")
                    if outcome == "exited":
                        log("worker-died", port=port, returncode=rc, account=key)
                        raise WorkerStartError(
                            f"worker for {key[:3]}*** died during startup (rc={rc})")
                    log("worker-unhealthy", port=port, returncode=rc,
                        startup_timeout=self._cfg.worker_startup_timeout)
                    raise WorkerStartError(f"worker for {key[:3]}*** failed to become healthy")
                self._workers[key] = WorkerHandle(key, port, proc, self._clock())
                log("worker-started", port=port, account=key,
                    ms=int((self._clock() - t0) * 1000))
                self.write_snapshot()
                return port
            finally:
                self._reserved.discard(port)

    async def persist_rotated(self) -> None:
        """Capture worker-written token rotations into the store — the periodic
        tick of the read-back path (driven from the lifespan loop, like
        reap_idle). Takes the same per-account lock ensure_worker holds so a
        read can't interleave with a materialize; a held lock is skipped, not
        awaited — a spawn in progress does its own read-back."""
        # Startup workers are never registered in `_workers`. If their final
        # post-TERM capture hit a temporary store failure, the periodic tick is
        # the retry path even when no new request arrives before shutdown.
        for key in list(self._pending_capture):
            lock = self._locks[key]
            if lock.locked():
                continue
            async with lock:
                if key in self._pending_capture:
                    try:
                        current = self._authoritative_blob(
                            key, self._persisted.get(key, ""))
                    except WorkerCredentialsRejected:
                        # Account deletion invalidates the old capture; the
                        # helper has already cleared it.
                        continue
                    except WorkerStartError as e:
                        # Store read failure is not evidence that the capture
                        # is stale. Keep it for the next tick.
                        log_exc("worker-tokens-persist-failed", e, account=key,
                                trigger="periodic-capture-generation-check",
                                error=str(e))
                        continue
                    self._discard_stale_pending_capture(key, current)
                if key in self._pending_capture:
                    self._read_back_and_persist(
                        key, "periodic-startup-capture", retry_on_failure=True)
        for key in list(self._workers):
            lock = self._locks[key]
            if lock.locked():
                continue
            async with lock:
                if key in self._workers:
                    self._read_back_and_persist(key, "periodic")

    async def reap_idle(self) -> None:
        now = self._clock()
        reaped = False
        for key, snapshot in list(self._workers.items()):
            lock = self._locks[key]
            # A spawn/replacement owns both this registry entry and its token
            # directory. It performs the same final capture itself, so the
            # background reaper can safely defer this account to the next tick.
            if lock.locked():
                continue
            async with lock:
                h = self._workers.get(key)
                if h is None or h is not snapshot:
                    continue
                dead = h.process.poll() is not None
                idle_expired = now - h.last_active > self._cfg.worker_idle_ttl
                # Reap dead processes always; reap idle ones only when no request
                # is streaming through them. Re-check after acquiring the lock.
                if not (dead or (idle_expired and h.inflight == 0)):
                    continue
                if not await self._stop_and_wait_async(h):
                    log_error("worker-stop-timeout", port=h.port, account=h.key,
                              trigger="reap")
                    continue
                if self._workers.get(key) is h:
                    self._workers.pop(key, None)
                # The account lock stays held across exit confirmation, token
                # capture and registry removal, so another request cannot
                # rematerialize this workdir while the old process is writing.
                self._read_back_and_persist(
                    key, "reap", retry_on_failure=True)
                log("worker-reaped", port=h.port, account=h.key)
                reaped = True
        if reaped:
            self.write_snapshot()

    def active_count(self) -> int:
        """Number of per-user workers currently running (for monitoring)."""
        return len(self._workers)

    def request_started(self, key: str) -> None:
        """Mark a proxied request in-flight for a worker so reap/evict won't kill
        it mid-stream; also refreshes its activity timestamp."""
        h = self._workers.get(key)
        if h is not None:
            h.inflight += 1
            h.last_active = self._clock()

    def request_finished(self, key: str) -> None:
        h = self._workers.get(key)
        if h is not None:
            h.inflight = max(0, h.inflight - 1)
            h.last_active = self._clock()

    def snapshot(self) -> list[dict]:
        """Per-worker state for monitoring: account, port, pid, alive, idle secs."""
        now = self._clock()
        return [
            {
                "key": h.key,
                "port": h.port,
                "pid": getattr(h.process, "pid", None),
                "alive": h.process.poll() is None,
                "inflight": h.inflight,
                "idle_seconds": round(now - h.last_active, 1),
            }
            for h in self._workers.values()
        ]

    def write_snapshot(self) -> None:
        """Persist worker state to DATA_DIR/workers.json (atomic) for monitoring
        (scripts/status.py). Best-effort — never raises into the caller."""
        path = os.path.join(self._cfg.data_dir, "workers.json")
        data = {"updated": time.strftime("%H:%M:%S"), "workers": self.snapshot()}
        tmp = f"{path}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, path)
        except OSError:
            pass

    def shutdown(self) -> None:
        for h in list(self._workers.values()):
            stopped = self._stop_and_wait(h)
            # Last chance before the restart: an uncaptured rotation would make
            # the next boot materialize a spent token from the store. No lock
            # guard — the server is past accepting requests, and a concurrent
            # materialize would only make this a no-op (file == store blob).
            if stopped:
                self._read_back_and_persist(
                    h.key, "shutdown", retry_on_failure=True)
                self._workers.pop(h.key, None)
            else:
                log_error("worker-stop-timeout", port=h.port, account=h.key,
                          trigger="shutdown")
        for key, (proc, port) in list(self._orphaned.items()):
            self._stop_spawned(key, proc, port, "shutdown-orphan")
        for key in list(self._pending_capture):
            self._read_back_and_persist(
                key, "shutdown-capture-retry", retry_on_failure=True)
        self.write_snapshot()

    # --- internals ------------------------------------------------------

    async def _enforce_cap(self) -> None:
        # Count in-flight spawns (ports reserved but not yet registered in
        # _workers) toward the cap — mirrors _alloc_port's union of _reserved —
        # so concurrent distinct-key spawns can't overshoot MAX_WORKERS.
        while len(self._workers) + len(self._reserved) >= self._cfg.max_workers:
            idle = [h for h in self._workers.values() if h.inflight == 0]
            if not idle:
                # Every worker is mid-request; evicting one would abort a live
                # stream. Let the pool exceed the cap transiently instead.
                log("worker-cap-all-busy", workers=len(self._workers))
                break
            oldest = min(idle, key=lambda h: h.last_active)
            lock = self._locks[oldest.key]
            async with lock:
                current = self._workers.get(oldest.key)
                if current is None or current is not oldest:
                    continue
                if current.inflight > 0:
                    # It became busy while we waited for its account lock. Do
                    # not spin on it or abort the newly active stream.
                    log("worker-cap-all-busy", workers=len(self._workers))
                    break
                if not await self._stop_and_wait_async(current):
                    log_error("worker-stop-timeout", port=current.port,
                              account=current.key, trigger="evict")
                    break
                if self._workers.get(current.key) is current:
                    self._workers.pop(current.key, None)
                self._read_back_and_persist(
                    current.key, "evict", retry_on_failure=True)
                log("worker-evicted", port=current.port, account=current.key)

    def _workdir(self, key: str) -> str:
        return os.path.join(self._cfg.data_dir, "users", account_dir_name(key), "tokens")

    def _materialize(self, key: str, blob: str) -> str:
        workdir = self._workdir(key)
        user_dir = os.path.dirname(workdir)
        os.makedirs(workdir, exist_ok=True)
        os.chmod(user_dir, 0o700)
        os.chmod(workdir, 0o700)
        self._forward.materialize(blob, workdir)
        self._persisted[key] = blob        # the file now mirrors the store
        return workdir

    def _read_back_and_persist(self, key: str, trigger: str,
                               *, retry_on_failure: bool = False) -> str | None:
        """Persist the worker-rewritten token file to the store when it differs
        from the last store state this process knows (the WHOOP persist-before-use
        rule, worker edition). Returns the captured content, else None. With no
        known baseline (fresh process) it does nothing: a differing file may be
        OLDER than a re-login that just reached the store, so the store wins —
        repairing pre-fix drift is the explicit backfill's job, never this path's."""
        read_back = getattr(self._forward, "read_back", None)
        if self._persist is None or read_back is None:
            if retry_on_failure:
                self._clear_pending_capture(key)
            return None
        last = self._persisted.get(key)
        if last is None:
            if retry_on_failure:
                self._clear_pending_capture(key)
            return None
        try:
            prepare_read_back = getattr(self._forward, "prepare_read_back", None)
            if prepare_read_back is not None:
                prepare_read_back(last, self._workdir(key))
            content = read_back(self._workdir(key))
        except Exception as e:  # noqa: BLE001 - callers are batch contexts (tick, evict inside
            # another account's spawn, shutdown): one account's disk problem is
            # logged and skipped, never propagated into the batch.
            log_exc("worker-tokens-persist-failed", e, account=key,
                    trigger=trigger, error=str(e))
            if retry_on_failure:
                self._retain_pending_capture(key, last)
            return None
        if content is None:
            # A stopped worker's file should exist and parse. Treat missing or
            # torn content as retryable, not as "unchanged": rematerializing the
            # DB baseline here could destroy its only completed rotation.
            if retry_on_failure:
                self._retain_pending_capture(key, last)
            return None
        if content == last:
            if retry_on_failure:
                self._clear_pending_capture(key)
            return None
        try:
            persisted = self._persist(key, content, last)
        except Exception as e:  # noqa: BLE001 - store hiccup: keep the baseline, retry next tick
            log_exc("worker-tokens-persist-failed", e, account=key,
                    trigger=trigger, error=str(e))
            if retry_on_failure:
                self._retain_pending_capture(key, last)
            return None
        if persisted is False:
            # A browser re-login (or deletion) changed the durable generation.
            # Keep our old baseline so ensure_worker can detect and replace it;
            # never resurrect/overwrite the account from this worker.
            log("worker-tokens-persist-skipped", account=key, trigger=trigger,
                reason="store-generation-changed")
            if retry_on_failure:
                self._clear_pending_capture(key)
            return None
        self._persisted[key] = content
        self._clear_pending_capture(key)
        log("worker-tokens-persisted", account=key, trigger=trigger)
        return content

    def _pending_capture_path(self, key: str) -> str:
        return os.path.join(self._workdir(key), _PENDING_CAPTURE_FILE)

    @staticmethod
    def _blob_digest(blob: str) -> str:
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def _retain_pending_capture(self, key: str, expected_blob: str) -> None:
        """Keep failed stopped-worker capture recoverable across a restart.

        The marker contains only a digest of the DB generation the stopped
        process started from, never token material. A fresh manager may trust
        the different token file only while the durable DB still matches this
        digest; a browser re-login makes the marker stale and the DB wins.
        """
        self._pending_capture.add(key)
        workdir = self._workdir(key)
        try:
            os.makedirs(workdir, exist_ok=True)
            # `_materialize` normally created both directories already, but a
            # capture retry after an operator restore may have to recreate
            # them.  Keep token-bearing directories private on that path too.
            os.chmod(os.path.dirname(workdir), 0o700)
            os.chmod(workdir, 0o700)
            fd, tmp = tempfile.mkstemp(
                prefix=".missingmcp-pending-", dir=workdir, text=True)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump({"v": 1,
                               "expected_sha256": self._blob_digest(expected_blob)}, f)
                os.replace(tmp, self._pending_capture_path(key))
                os.chmod(self._pending_capture_path(key), 0o600)
            except Exception:
                try:
                    os.close(fd)
                except OSError:
                    pass
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError as e:
            # In-memory state still prevents reuse in this process. The marker
            # failure is operator-visible because restart recovery is degraded.
            log_exc("worker-capture-marker-failed", e, account=key,
                    error=str(e))

    def _clear_pending_capture(self, key: str) -> None:
        self._pending_capture.discard(key)
        try:
            os.unlink(self._pending_capture_path(key))
        except FileNotFoundError:
            pass
        except OSError as e:
            log_exc("worker-capture-marker-clear-failed", e, account=key,
                    error=str(e))

    def _discard_stale_pending_capture(self, key: str,
                                       current_blob: str) -> None:
        """Drop an old capture once durable credentials have advanced.

        In-process pending captures retain their expected generation in
        `_persisted`; the durable marker is the restart equivalent. Checking
        this before parsing disk is what lets a successful re-login recover
        from a torn/missing/invalid old file without ever persisting it.
        """
        if key not in self._pending_capture:
            return
        expected = self._persisted.get(key)
        if expected is not None:
            matches = self._blob_digest(expected) == self._blob_digest(current_blob)
        else:
            try:
                with open(self._pending_capture_path(key), encoding="utf-8") as f:
                    marker = json.load(f)
                digest = marker.get("expected_sha256") if isinstance(marker, dict) else None
            except (OSError, ValueError):
                # An unreadable marker without an in-memory baseline cannot
                # prove either lineage. Preserve it and fail closed.
                return
            matches = digest == self._blob_digest(current_blob)
        if matches:
            return
        self._clear_pending_capture(key)
        self._persisted.pop(key, None)
        log("worker-tokens-persist-skipped", account=key,
            trigger="pending-generation-check",
            reason="store-generation-changed")

    def _recover_durable_capture(self, key: str, current_blob: str) -> str:
        """Recover a proven stopped-worker rotation left by an earlier process."""
        path = self._pending_capture_path(key)
        try:
            with open(path, encoding="utf-8") as f:
                marker = json.load(f)
        except FileNotFoundError:
            return current_blob
        except (OSError, ValueError) as e:
            raise WorkerStartError(
                f"invalid pending token capture for {key[:3]}***") from e
        if (not isinstance(marker, dict) or marker.get("v") != 1
                or not isinstance(marker.get("expected_sha256"), str)):
            raise WorkerStartError(
                f"invalid pending token capture for {key[:3]}***")
        if marker["expected_sha256"] != self._blob_digest(current_blob):
            # A verified login or another writer advanced the store. The old
            # stopped worker must never overwrite that generation.
            self._clear_pending_capture(key)
            return current_blob
        self._persisted[key] = current_blob
        self._pending_capture.add(key)
        captured = self._read_back_and_persist(
            key, "restart-capture", retry_on_failure=True)
        if key in self._pending_capture:
            raise WorkerStartError(
                f"worker for {key[:3]}*** has an unpersisted token rotation")
        return self._authoritative_blob(key, captured or current_blob)

    def _authoritative_blob(self, key: str, fallback: str) -> str:
        if self._load is None:
            return fallback
        try:
            current = self._load(key)
        except Exception as e:  # noqa: BLE001 - never replay a hint on store failure
            raise WorkerStartError(
                f"could not load current credentials for {key[:3]}***") from e
        if current is None:
            # Deletion is an authoritative generation change. An old capture
            # must not keep the account in a re-auth loop or resurrect it. Keep
            # a live worker's old baseline until that worker is retired: if the
            # same account key is recreated, it must compare unequal and force
            # replacement rather than inheriting the old authenticated process.
            self._clear_pending_capture(key)
            if key not in self._workers:
                self._persisted.pop(key, None)
            raise WorkerCredentialsRejected(
                f"account {key[:3]}*** no longer has stored credentials")
        return current

    def _alloc_port(self) -> int:
        # Round-robin, not lowest-free-first: the lowest free port is usually
        # the one this very spawn's _enforce_cap just freed, whose owner is
        # still dying. Cooling ports stay out of the pool entirely.
        self._purge_cooling()
        used = ({h.port for h in self._workers.values()} | self._reserved
                | set(self._cooling))
        start, end = self._cfg.worker_port_start, self._cfg.worker_port_end
        span = end - start + 1
        for i in range(span):
            p = start + (self._port_cursor - start + i) % span
            if p not in used:
                self._port_cursor = start + (p - start + 1) % span
                return p
        raise WorkerStartError("no free worker port")

    def _purge_cooling(self) -> None:
        now = self._clock()
        for port, (proc, since) in list(self._cooling.items()):
            try:
                dead = proc.poll() is not None
            except Exception:  # noqa: BLE001 - exit is not safely confirmed
                dead = False
            if dead:
                self._cooling.pop(port, None)
            elif now - since > _COOLING_KILL_S:
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass

    def _default_spawn(self, key: str, port: int, workdir: str):
        env = dict(os.environ)
        sanitize_env = getattr(self._forward, "sanitize_env", None)
        if sanitize_env is not None:
            env = sanitize_env(env)
        env.update(self._forward.env(port, workdir))
        proc = subprocess.Popen(self._forward.command(), env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, errors="replace", bufsize=1)
        # Arm the login gate only when the forward can classify sign-in lines
        # AND a pump exists to feed it — an injected test spawn has neither, and
        # ensure_worker skips the gate when the proc carries no `login_gate`.
        classify = getattr(self._forward, "login_outcome", None)
        gate = None
        if classify is not None:
            gate = LoginGate()
            proc.login_gate = gate
        threading.Thread(target=_pump_worker_output,
                         args=(proc.stdout, key, classify, gate),
                         name=f"worker-log-{key[:8]}", daemon=True).start()
        return proc

    def _stop_and_wait(self, h: WorkerHandle) -> bool:
        return self._stop_and_wait_process(h.process, h.port)

    async def _stop_and_wait_async(self, h: WorkerHandle) -> bool:
        return await self._stop_and_wait_process_async(h.process, h.port)

    def _stop_spawned(self, key: str, proc, port: int, trigger: str) -> bool:
        """Stop an unregistered startup process, retaining it if unconfirmed."""
        # Establish ownership before any stop work. The async counterpart can
        # be cancelled while it waits; recording first guarantees the account
        # directory remains fail-closed even after repeated cancellation.
        self._orphaned[key] = (proc, port)
        stopped = self._stop_and_wait_process(proc, port)
        if stopped:
            self._read_back_and_persist(
                key, f"{trigger}-capture", retry_on_failure=True)
            current = self._orphaned.get(key)
            if current is None or current == (proc, port):
                self._orphaned.pop(key, None)
            return True
        log_error("worker-stop-timeout", port=port, account=key, trigger=trigger)
        return False

    async def _stop_spawned_async(self, key: str, proc, port: int,
                                  trigger: str) -> bool:
        """Async startup cleanup that yields while the process exits."""
        # Publish stable account -> process ownership *before* the first await.
        # If this cleanup is cancelled, ensure_worker releases its account lock
        # and port reservation, but the next request still sees the orphan and
        # cannot rematerialize the shared token directory.
        self._orphaned[key] = (proc, port)
        stopped = await self._stop_and_wait_process_async(proc, port)
        if stopped:
            self._read_back_and_persist(
                key, f"{trigger}-capture", retry_on_failure=True)
            current = self._orphaned.get(key)
            if current is None or current == (proc, port):
                self._orphaned.pop(key, None)
            return True
        log_error("worker-stop-timeout", port=port, account=key, trigger=trigger)
        return False

    @staticmethod
    def _wait_process_exit(proc, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if proc.poll() is not None:
                    return True
            except Exception:  # noqa: BLE001 - unpollable means not safely confirmed
                return False
            time.sleep(0.01)
        try:
            return proc.poll() is not None
        except Exception:  # noqa: BLE001
            return False

    def _stop_and_wait_process(self, proc, port: int) -> bool:
        """Stop a process and confirm exit before its token files are touched."""
        try:
            if proc.poll() is not None:
                self._cooling.pop(port, None)
                return True
        except Exception:  # noqa: BLE001 - unpollable is not confirmed dead
            self._cooling[port] = (proc, self._clock())
            return False
        self._cooling[port] = (proc, self._clock())
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001
            return False
        if self._wait_process_exit(proc, _COOLING_KILL_S):
            self._cooling.pop(port, None)
            return True
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            return False
        stopped = self._wait_process_exit(proc, _KILL_CONFIRM_S)
        if stopped:
            self._cooling.pop(port, None)
        return stopped

    @staticmethod
    async def _wait_process_exit_async(proc, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if proc.poll() is not None:
                    return True
            except Exception:  # noqa: BLE001 - unpollable is not confirmed dead
                return False
            await asyncio.sleep(0.01)
        try:
            return proc.poll() is not None
        except Exception:  # noqa: BLE001
            return False

    async def _stop_and_wait_process_async(self, proc, port: int) -> bool:
        """Async counterpart used by request/reaper paths."""
        try:
            if proc.poll() is not None:
                self._cooling.pop(port, None)
                return True
        except Exception:  # noqa: BLE001 - unpollable is not confirmed dead
            self._cooling[port] = (proc, self._clock())
            return False
        self._cooling[port] = (proc, self._clock())
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001
            return False
        if await self._wait_process_exit_async(proc, _COOLING_KILL_S):
            self._cooling.pop(port, None)
            return True
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            return False
        stopped = await self._wait_process_exit_async(proc, _KILL_CONFIRM_S)
        if stopped:
            self._cooling.pop(port, None)
        return stopped

    async def _healthy(self, port: int) -> bool:
        try:
            async with httpx.AsyncClient(timeout=2.0) as c:
                r = await c.get(f"http://127.0.0.1:{port}/healthz")
                return r.status_code == 200
        except (httpx.HTTPError, OSError):
            return False

    async def _wait_healthy(self, port: int, proc, deadline: float) -> str:
        """Poll /healthz until the worker answers, dies, or the deadline passes.
        Returns *why* it stopped waiting — `healthy`, `exited` (the process is
        gone, so waiting longer is pointless) or `timeout` (still running, still
        silent) — because the caller reports those as different failures."""
        while self._clock() < deadline:
            if proc.poll() is not None:
                return "exited"
            if await self._healthy(port):
                return "healthy"
            await asyncio.sleep(0.25)
        return "timeout"

    async def _wait_login(self, gate: LoginGate, proc, deadline: float) -> str:
        """Poll the pump-fed login gate until the sign-in outcome lands, the
        worker dies, or the (shared) startup deadline passes. _wait_healthy one
        boot stage later: same deadline, same reasons-out, plus the outcome
        itself (`ok`/`failed`). The explicit outcome is checked before the
        process, so a worker that reported and then exited keeps its verdict."""
        while self._clock() < deadline:
            if gate.outcome is not None:
                return gate.outcome
            if proc.poll() is not None:
                return "exited"
            await asyncio.sleep(0.25)
        return gate.outcome or "timeout"
