import socket
import threading
import time
import pytest
from http.server import BaseHTTPRequestHandler, HTTPServer


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class FakeWorker:
    """A minimal HTTP server mimicking garmin-mcp's /healthz and /mcp."""
    def __init__(self):
        self.port = _free_port()
        self.calls = []
        worker = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silence
                pass
            def do_GET(self):
                if self.path == "/healthz":
                    self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
                else:
                    self.send_response(404); self.end_headers()
            def do_POST(self):
                length = int(self.headers.get("content-length", 0))
                body = self.rfile.read(length)
                worker.calls.append(("POST", self.path, dict(self.headers), body))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Mcp-Session-Id", "sess-1")
                self.end_headers()
                self.wfile.write(b'{"jsonrpc":"2.0","result":{}}')

        self._httpd = HTTPServer(("127.0.0.1", self.port), H)
        self._t = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def start(self):
        self._t.start()
        return self

    def stop(self):
        self._httpd.shutdown()


@pytest.fixture
def fake_worker():
    w = FakeWorker().start()
    # wait until accepting connections
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", w.port), timeout=0.1).close()
            break
        except OSError:
            time.sleep(0.02)
    yield w
    w.stop()
