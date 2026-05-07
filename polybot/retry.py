"""Async exponential-backoff retry for transient HTTP / RPC failures."""

from __future__ import annotations

import asyncio
import functools
import random
from typing import Awaitable, Callable, Tuple, Type, TypeVar

import aiohttp
import requests.exceptions as req_exc

from .logger import get_logger

log = get_logger(__name__)

T = TypeVar("T")

# Errors worth retrying. Anything else (4xx auth, validation, etc) bubbles up.
RETRYABLE: Tuple[Type[BaseException], ...] = (
    aiohttp.ClientError,
    asyncio.TimeoutError,
    req_exc.ConnectionError,
    req_exc.Timeout,
    req_exc.ChunkedEncodingError,
    ConnectionError,
    TimeoutError,
)


def with_retries(
    *, attempts: int = 5, base_delay: float = 1.0, max_delay: float = 30.0,
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    def decorator(fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs) -> T:
            last_exc: BaseException | None = None
            for attempt in range(attempts):
                try:
                    return await fn(*args, **kwargs)
                except RETRYABLE as exc:
                    last_exc = exc
                    if attempt == attempts - 1:
                        break
                    delay = min(base_delay * (2 ** attempt), max_delay)
                    delay += random.uniform(0, delay * 0.25)
                    log.warning(
                        "%s failed (%s: %s). Retry %d/%d in %.1fs",
                        fn.__name__, type(exc).__name__, exc,
                        attempt + 1, attempts, delay,
                    )
                    await asyncio.sleep(delay)
            assert last_exc is not None
            log.error("%s exhausted %d retries: %s", fn.__name__, attempts, last_exc)
            raise last_exc

        return wrapper

    return decorator
