"""The structured-logging contract: everything must reach STDOUT as JSON with a
proper level attribute — Railway classifies plain STDERR output as
error-severity (uvicorn's default handlers did exactly that in production)."""
import io
import json
import logging
import sys
from missingmcp import log as mlog
from missingmcp.workers import _pump_worker_output


def _capture(capsys):
    return [json.loads(l) for l in capsys.readouterr().out.splitlines() if l.strip()]


def _setup_clean():
    mlog.setup_logging(path=None)


def test_stdlib_records_become_structured_stdout_json(capsys):
    _setup_clean()
    logging.getLogger("uvicorn.error").info("Started server process [1]")
    logging.getLogger("garminconnect").warning("odd response")
    events = _capture(capsys)
    uvi = next(e for e in events if e.get("logger") == "uvicorn.error")
    assert uvi["event"] == "stdlib-log" and uvi["level"] == "info"
    assert uvi["message"] == "Started server process [1]"
    warn = next(e for e in events if e.get("logger") == "garminconnect")
    assert warn["level"] == "warn"


def test_stdlib_exceptions_carry_traceback(capsys):
    _setup_clean()
    try:
        raise ValueError("boom")
    except ValueError:
        logging.getLogger("starlette").exception("handler failed")
    events = _capture(capsys)
    e = next(e for e in events if e.get("logger") == "starlette")
    assert e["level"] == "error" and "ValueError: boom" in e["traceback"]


def test_setup_logging_is_idempotent(capsys):
    _setup_clean()
    _setup_clean()   # re-setup must not duplicate handlers → one line per record
    logging.getLogger("dup-check").info("once")
    events = [e for e in _capture(capsys) if e.get("logger") == "dup-check"]
    assert len(events) == 1


def test_file_tee_writes_each_record_once_as_json(tmp_path):
    """With GATEWAY_LOG_FILE set, a bridged stdlib record must land in the tee
    file exactly once, and every line in the file must be valid JSON (the
    structured format) — no duplicate plain-text copy from a second handler."""
    logfile = tmp_path / "gateway.log"
    try:
        mlog.setup_logging(path=str(logfile))
        logging.getLogger("filetee").info("hello file")
    finally:                                   # reset global tee + handlers
        root = logging.getLogger()
        for h in list(root.handlers):
            root.removeHandler(h)
            h.close()
        if mlog._file is not None:
            mlog._file.close()
            mlog._file = None
    lines = [l for l in logfile.read_text(encoding="utf-8").splitlines() if l.strip()]
    parsed = [json.loads(l) for l in lines]    # every line must be valid JSON
    hits = [p for p in parsed if p.get("message") == "hello file"]
    assert len(hits) == 1                       # exactly once, not twice
    assert hits[0]["event"] == "stdlib-log" and hits[0]["logger"] == "filetee"


def test_worker_pump_emits_structured_lines_with_severity(capsys):
    lines = io.StringIO(
        "INFO:     Uvicorn running on http://127.0.0.1:9000\n"
        "\n"
        "ERROR:    something broke\n"
        "plain progress line\n"
    )
    _pump_worker_output(lines, "me@x.cz")
    events = _capture(capsys)
    assert [e["event"] for e in events] == ["worker-log"] * 3   # blank line dropped
    assert all(e["account"] == "me@x.cz" for e in events)
    assert events[0]["level"] == "info"
    assert events[1]["level"] == "error"      # ERROR heuristic elevates severity
    assert events[2]["level"] == "info"


def test_third_party_password_and_tokens_are_redacted(capsys):
    lines = io.StringIO(
        'password=hunter2 access_token="access-secret" '
        'di_refresh_token: refresh-secret Authorization: Bearer bearer-secret\n'
    )
    _pump_worker_output(lines, "me@x.cz")
    event = _capture(capsys)[0]
    rendered = json.dumps(event)
    assert "hunter2" not in rendered
    assert "access-secret" not in rendered
    assert "refresh-secret" not in rendered
    assert "bearer-secret" not in rendered
    assert rendered.count("[REDACTED]") == 4


def test_quoted_and_structured_secrets_are_redacted_everywhere(
        tmp_path, capsys):
    logfile = tmp_path / "secrets.log"
    sink_records = []
    secrets = [
        "json-password-secret", "dict-refresh-secret",
        "structured-client-secret", "structured-mfa-secret",
        "trace-password-secret", "escaped-quote-secret-suffix",
        "basic-authorization-secret", "digest-authorization-secret",
        "digest-response-secret", "trace-digest-response-secret",
    ]
    try:
        mlog.setup_logging(path=str(logfile))
        mlog.set_sink(sink_records.append)
        escaped = json.dumps({
            "password": 'prefix"escaped-quote-secret-suffix',
            "safe": "ok",
        })
        _pump_worker_output(io.StringIO(
            '{"password":"json-password-secret", '
            "'di_refresh_token': 'dict-refresh-secret', \"safe\":\"ok\"}\n"
            + escaped + "\n"
            + "Authorization: Basic basic-authorization-secret\n"
            + "Authorization=Digest digest-authorization-secret\n"
            + ('Authorization: Digest username="u", realm="r", nonce="n", '
               'response="digest-response-secret"\n')
        ), "me@x.cz")
        mlog.log("structured-secret-fields",
                 client_secret="structured-client-secret",
                 mfa_code="structured-mfa-secret", safe="still-visible")
        try:
            raise RuntimeError(
                '{"password":"trace-password-secret"}\n'
                'Authorization: Digest username="u", realm="r", nonce="n", '
                'response="trace-digest-response-secret"')
        except RuntimeError:
            logging.getLogger("secret-traceback").exception("upstream failed")
    finally:
        mlog.set_sink(None)
        root = logging.getLogger()
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()
        if mlog._file is not None:
            mlog._file.close()
            mlog._file = None

    outputs = [
        capsys.readouterr().out,
        logfile.read_text(encoding="utf-8"),
        json.dumps(sink_records),
    ]
    for output in outputs:
        assert all(secret not in output for secret in secrets)
        assert "[REDACTED]" in output
        assert "still-visible" in output
