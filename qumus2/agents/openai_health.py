"""Free, read-only gateway reachability probe. It does not prove model capability."""

from __future__ import annotations

import asyncio
import time
import urllib.error
import urllib.request
from typing import Any

from qumus2.agents.openai_credentials import OpenAICredentials
from qumus2.core.http import tls_context
from qumus2.core.scrub import scrub_text


class OpenAIHealth:
    def __init__(self, creds: OpenAICredentials, interval_s: float = 300.0,
                 timeout_s: float = 8.0) -> None:
        self.creds, self.interval_s, self.timeout_s = creds, interval_s, timeout_s
        self.state: dict[str, Any] = {"state": "unknown", "detail": "not checked yet",
                                      "checked_at": None}

    def _set(self, state: str, detail: str) -> dict[str, Any]:
        self.state = {"state": state, "detail": scrub_text(detail, [self.creds.api_key]),
                      "checked_at": time.time()}
        return self.state

    async def run_forever(self) -> None:
        while True:
            await asyncio.to_thread(self.check)
            await asyncio.sleep(self.interval_s)

    @property
    def url(self) -> str:
        return self.creds.base_url.rstrip("/") + "/models"

    def _request(self) -> urllib.request.Request:
        return urllib.request.Request(self.url, headers={
            "Authorization": f"Bearer {self.creds.api_key}", "Accept": "application/json"}, method="GET")

    def check(self) -> dict[str, Any]:
        host = self.creds.base_url
        try:
            with urllib.request.urlopen(self._request(), timeout=self.timeout_s,
                                        context=tls_context()) as response:
                code = response.status
        except urllib.error.HTTPError as exc:
            code = exc.code
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return self._set("unreachable", f"cannot reach {host}: {getattr(exc, 'reason', exc)}")
        if code in (401, 403):
            return self._set("auth_failed", f"{host} refused the key (HTTP {code})")
        if code == 429:
            return self._set("rate_limited", "Gateway reached; rate/credit limit (HTTP 429). "
                             "Model calls have not been verified.")
        if code >= 300:
            return self._set("error", f"models probe returned HTTP {code}; model calls unverified")
        return self._set("ok", f"{host} models endpoint reachable; model/tool/image support unverified")
