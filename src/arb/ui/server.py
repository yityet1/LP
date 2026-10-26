"""Standalone zero-dependency HTTP REST API & Web UI Server (`python3 -m arb.ui.server`)."""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
from typing import Any, Dict

from arb.portability import run_portability_check
from arb.testing.real_data_runner import RealDataBenchmarkRunner

REPO_ROOT = Path(__file__).resolve().parents[3]
UI_HTML_PATH = Path(__file__).resolve().parent / "static" / "index.html"


class ArbPlatformRequestHandler(BaseHTTPRequestHandler):
    """Serves the Quantitative Arbitrage Command Center UI and live JSON REST endpoints."""

    def _send_json(self, payload: Dict[str, Any], status: int = 200) -> None:
        raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/", "/index.html"):
            if UI_HTML_PATH.exists():
                html_bytes = UI_HTML_PATH.read_bytes()
            else:
                html_bytes = b"<h1>Arb-Platform UI Terminal</h1>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html_bytes)))
            self.end_headers()
            self.wfile.write(html_bytes)
            return

        if self.path == "/api/v1/health":
            self._send_json({"status": "ok", "live_trading_enabled": False, "mode": "paper"})
            return

        if self.path == "/api/v1/portability":
            rep = run_portability_check()
            self._send_json(json.loads(rep.json()))
            return

        if self.path in ("/api/v1/real-data-report", "/api/v1/scan"):
            report = RealDataBenchmarkRunner.run_full_real_data_suite(REPO_ROOT)
            self._send_json(report)
            return

        self._send_json({"error": "Not found", "path": self.path}, status=404)

    def log_message(self, format: str, *args: Any) -> None:
        return


def create_ui_http_server(host: str = "127.0.0.1", port: int = 8080) -> HTTPServer:
    return HTTPServer((host, port), ArbPlatformRequestHandler)


def main() -> None:
    parser = argparse.ArgumentParser(description="Start arb-platform Web UI & REST API Server")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8080, help="Bind port (default: 8080)")
    args = parser.parse_args()

    srv = create_ui_http_server(args.host, args.port)
    print(f"[arb-platform] Quantitative Arbitrage UI Terminal listening on http://{args.host}:{args.port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.server_close()


if __name__ == "__main__":
    main()
