"""Local Full-Stack Web UI & REST API Server for `arb-platform`.

Launch via:
    python run_ui.py
or:
    PYTHONPATH=src python -m arb.ui.server --host 127.0.0.1 --port 8080

Provides:
- `GET /`: Serves the interactive Quantitative Arbitrage Command Center Web UI (`static/index.html`)
- `GET /api/status`: Returns real-time system health, portability audit, and latest real-data benchmark report
- `POST /api/run-benchmark`: Re-runs `run_real_market_benchmark()` on the 2026-10-26 real market dataset and returns fresh results
- `POST /api/live-market`: Connects to live Binance & OKX REST APIs (with optional proxy) to pull real-time L2 order books and evaluate arbitrage strategies
"""
from __future__ import annotations

import argparse
import asyncio
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict
from urllib.parse import parse_qs, urlparse

from arb.marketdata.live_feed import LiveMarketDataFeed
from arb.portability import run_portability_check
from arb.testing.real_data_runner import run_real_market_benchmark


STATIC_DIR = Path(__file__).resolve().parent / "static"
REPO_ROOT = Path(__file__).resolve().parents[3]
REPORT_PATH = REPO_ROOT / "datasets" / "real_data_test_report.json"


class ArbPlatformRequestHandler(BaseHTTPRequestHandler):
    def _send_json(self, status_code: int, payload: Dict[str, Any]) -> None:
        raw = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            index_file = STATIC_DIR / "index.html"
            if not index_file.exists():
                self._send_json(404, {"error": "UI static/index.html not found"})
                return
            content = index_file.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return

        if parsed.path == "/api/status":
            if REPORT_PATH.exists():
                report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
            else:
                report = run_real_market_benchmark()
            self._send_json(
                200,
                {
                    "status": "OK",
                    "portability": run_portability_check(),
                    "benchmark": report,
                },
            )
            return

        if parsed.path == "/api/live-market":
            qs = parse_qs(parsed.query)
            proxy_url = qs.get("proxy", [None])[0]
            feed = LiveMarketDataFeed(proxy_url=proxy_url)
            live_data = asyncio.run(feed.fetch_and_scan_once())
            self._send_json(200, {"status": "OK", "live_market": live_data})
            return

        self._send_json(404, {"error": f"Unknown endpoint {parsed.path}"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/run-benchmark":
            report = run_real_market_benchmark()
            self._send_json(
                200,
                {
                    "status": "RECOMPUTED",
                    "portability": run_portability_check(),
                    "benchmark": report,
                },
            )
            return

        if parsed.path == "/api/live-market":
            content_len = int(self.headers.get("Content-Length", "0"))
            body_raw = self.rfile.read(content_len).decode("utf-8") if content_len > 0 else "{}"
            try:
                body = json.loads(body_raw) if body_raw.strip() else {}
            except Exception:
                body = {}
            proxy_url = body.get("proxy") or None
            feed = LiveMarketDataFeed(proxy_url=proxy_url)
            live_data = asyncio.run(feed.fetch_and_scan_once())
            self._send_json(200, {"status": "OK", "live_market": live_data})
            return

        self._send_json(404, {"error": f"Unknown POST endpoint {parsed.path}"})


def main() -> None:
    parser = argparse.ArgumentParser(description="ARB-PLATFORM Quantitative Arbitrage Command Center UI Server")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8080, help="Bind port (default: 8080)")
    args = parser.parse_args()

    run_real_market_benchmark()
    server = HTTPServer((args.host, args.port), ArbPlatformRequestHandler)
    print(f"ARB-PLATFORM UI Server running at http://{args.host}:{args.port}")
    print("  - Interactive UI:       http://127.0.0.1:8080/")
    print("  - Live Market API:      http://127.0.0.1:8080/api/live-market")
    print("  - Real-Data Benchmark:  http://127.0.0.1:8080/api/status")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down UI server...")
        server.server_close()


if __name__ == "__main__":
    main()
