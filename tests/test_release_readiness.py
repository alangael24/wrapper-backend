from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "verify-live-readiness.py"
REQUIRED_CHECKS = {
    "database": True,
    "google_auth": True,
    "apple_auth": True,
    "stripe": True,
    "connectors": True,
    "computers": True,
    "pi": True,
    "pi_chrome": True,
    "model_capacity": True,
}


class ReadinessHandler(BaseHTTPRequestHandler):
    ready = True

    def log_message(self, _format, *_args):
        pass

    def do_GET(self):
        payload = {
            "ready": type(self).ready,
            "environment": "production",
            "checks": {**REQUIRED_CHECKS, "stripe": type(self).ready},
        }
        body = json.dumps(payload).encode()
        self.send_response(200 if type(self).ready else 503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class TestReleaseReadinessGate(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), ReadinessHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.env = {
            **os.environ,
            "AGENTGENIA_API_BASE_URL": (
                f"http://127.0.0.1:{self.server.server_address[1]}"
            ),
        }

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def run_gate(self):
        return subprocess.run(
            [sys.executable, str(SCRIPT)],
            cwd=ROOT,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    def test_gate_accepts_complete_production_readiness(self):
        ReadinessHandler.ready = True
        result = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("release-ready", result.stdout)

    def test_gate_rejects_a_failed_required_check(self):
        ReadinessHandler.ready = False
        result = self.run_gate()
        self.assertEqual(result.returncode, 1)
        self.assertIn("readiness request failed", result.stderr.lower())


if __name__ == "__main__":
    unittest.main()
