"""Shared HTTP transport for Polymarket's public read APIs."""

import asyncio
import logging
from typing import Any

import httpx
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.config.settings import get_settings

logger = logging.getLogger(__name__)

USER_AGENT = "polybot-research-collector/0.1 (+read-only, no orders, no wallet)"


class RetryableHTTPStatus(Exception):
    """A 429 or 5xx response - transient, worth retrying."""

    def __init__(self, status_code: int, url: str) -> None:
        self.status_code = status_code
        self.url = url
        super().__init__(f"retryable HTTP {status_code} from {url}")


class PolymarketHTTP:
    """One shared async client for all Polymarket collectors.

    Accepts an injected httpx.AsyncClient so tests can pass one built on
    httpx.MockTransport; otherwise builds and owns its own (and is
    responsible for closing it).
    """

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        settings = get_settings()
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=settings.http_timeout_seconds,
            headers={"User-Agent": USER_AGENT},
        )
        self._semaphore = asyncio.Semaphore(settings.http_max_concurrency)

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, RetryableHTTPStatus)),
        wait=wait_exponential(multiplier=0.5, max=30),
        stop=stop_after_attempt(5),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    async def get_json(self, url: str, params: Any = None) -> Any:
        """GET url, returning parsed JSON. Retries transient failures only."""
        async with self._semaphore:
            response = await self._client.get(url, params=params)

        if response.status_code == 429 or response.status_code >= 500:
            raise RetryableHTTPStatus(response.status_code, url)
        # Other 4xx (400/401/403/404/...) is our bug, not a transient fault -
        # fail immediately, no retry.
        response.raise_for_status()
        return response.json()

    async def aclose(self) -> None:
        """Close the client, but only if this instance created it."""
        if self._owns_client:
            await self._client.aclose()
