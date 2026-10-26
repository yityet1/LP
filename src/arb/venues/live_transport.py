"""Concrete zero-dependency HTTP transport for live CEX/DEX REST APIs using Python stdlib `urllib.request`.

Supports:
- Standard HTTPS requests with configurable timeout (`timeout_ms`).
- Automatic HTTP/HTTPS proxy support via `proxy_url` parameter or `HTTPS_PROXY` / `HTTP_PROXY` environment variables.
- Automatic Binance mirror fallback (`https://api.binance.com` -> `https://data-api.binance.vision`) when geo-blocked or unreachable.
- Full compatibility with `BinanceSpotRestAdapter`, `OkxSpotRestAdapter`, `BinanceUsdmDerivativesAdapter`, and `OkxSwapDerivativesAdapter`.
"""
from __future__ import annotations

import asyncio
import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from arb.venues.binance_spot.rest import HttpResponsePayload, HttpTransport


class UrllibHttpTransport(HttpTransport):
    """Production-ready stdlib HTTP transport requiring zero third-party packages."""

    BINANCE_MIRRORS: List[str] = [
        "https://data-api.binance.vision",
        "https://api1.binance.com",
        "https://api2.binance.com",
        "https://api3.binance.com",
    ]

    def __init__(
        self,
        proxy_url: Optional[str] = None,
        user_agent: str = "arb-platform/1.0 (Quantitative-Arbitrage-Engine)",
        verify_ssl: bool = True,
        enable_binance_mirror_fallback: bool = True,
    ) -> None:
        self._proxy_url = (
            proxy_url
            or os.environ.get("ARB_PROXY_URL")
            or os.environ.get("HTTPS_PROXY")
            or os.environ.get("HTTP_PROXY")
        )
        self._user_agent = user_agent
        self._verify_ssl = verify_ssl
        self._enable_binance_mirror_fallback = enable_binance_mirror_fallback

    def _sync_request(
        self,
        method: str,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout_ms: int = 5000,
    ) -> HttpResponsePayload:
        full_url = url
        if params:
            query_str = urllib.parse.urlencode(params)
            sep = "&" if "?" in full_url else "?"
            full_url = f"{full_url}{sep}{query_str}"

        req_headers = {
            "User-Agent": self._user_agent,
            "Accept": "application/json",
        }
        if headers:
            req_headers.update(headers)

        handlers: List[urllib.request.BaseHandler] = []
        if self._proxy_url:
            handlers.append(
                urllib.request.ProxyHandler(
                    {"http": self._proxy_url, "https": self._proxy_url}
                )
            )

        ssl_ctx = ssl.create_default_context()
        if not self._verify_ssl:
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE
        handlers.append(urllib.request.HTTPSHandler(context=ssl_ctx))

        opener = urllib.request.build_opener(*handlers)
        req = urllib.request.Request(full_url, headers=req_headers, method=method.upper())
        timeout_sec = max(0.5, float(timeout_ms) / 1000.0)

        t0 = time.monotonic()
        try:
            with opener.open(req, timeout=timeout_sec) as resp:
                raw_bytes = resp.read()
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                text_body = raw_bytes.decode("utf-8", errors="replace")
                resp_headers = {k: v for k, v in resp.headers.items()}
                status_code = int(getattr(resp, "status", 200))
                try:
                    json_body = json.loads(text_body) if text_body.strip() else None
                except Exception:
                    json_body = None
                return HttpResponsePayload(
                    status_code=status_code,
                    json_body=json_body,
                    text_body=text_body,
                    headers=resp_headers,
                    elapsed_ms=elapsed_ms,
                )
        except urllib.error.HTTPError as http_err:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            raw_bytes = http_err.read() if http_err.fp else b""
            text_body = raw_bytes.decode("utf-8", errors="replace")
            resp_headers = {k: v for k, v in http_err.headers.items()} if http_err.headers else {}
            try:
                json_body = json.loads(text_body) if text_body.strip() else None
            except Exception:
                json_body = None
            return HttpResponsePayload(
                status_code=int(http_err.code),
                json_body=json_body,
                text_body=text_body,
                headers=resp_headers,
                elapsed_ms=elapsed_ms,
            )
        except urllib.error.URLError as url_err:
            reason = str(getattr(url_err, "reason", url_err))
            if "timed out" in reason.lower() or isinstance(getattr(url_err, "reason", None), TimeoutError):
                raise TimeoutError(f"HTTP request timed out after {timeout_ms}ms: {full_url}") from url_err
            raise ConnectionError(f"Network connection failed for {full_url}: {reason}") from url_err
        except TimeoutError as te:
            raise TimeoutError(f"HTTP request timed out after {timeout_ms}ms: {full_url}") from te
        except Exception as exc:
            raise ConnectionError(f"Transport error for {full_url}: {exc}") from exc

    async def request(
        self,
        method: str,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout_ms: int = 5000,
    ) -> HttpResponsePayload:
        # Attempt primary URL first; if it's api.binance.com and fails or returns 451/403, try public mirror
        try:
            payload = await asyncio.to_thread(
                self._sync_request, method, url, params, headers, timeout_ms
            )
            if (
                self._enable_binance_mirror_fallback
                and "://api.binance.com" in url
                and payload.status_code in (403, 451, 502, 503, 504)
            ):
                for mirror in self.BINANCE_MIRRORS:
                    mirror_url = url.replace("https://api.binance.com", mirror)
                    try:
                        m_payload = await asyncio.to_thread(
                            self._sync_request, method, mirror_url, params, headers, timeout_ms
                        )
                        if m_payload.status_code == 200:
                            return m_payload
                    except Exception:
                        continue
            return payload
        except (TimeoutError, ConnectionError):
            if self._enable_binance_mirror_fallback and "://api.binance.com" in url:
                for mirror in self.BINANCE_MIRRORS:
                    mirror_url = url.replace("https://api.binance.com", mirror)
                    try:
                        return await asyncio.to_thread(
                            self._sync_request, method, mirror_url, params, headers, timeout_ms
                        )
                    except Exception:
                        continue
            raise
