"""HTTP calls that keep credentials where they belong, and answers that
can't run the bot out of memory.

Redirects: aiohttp follows them by itself, and when one leads to another
host it drops the Authorization and Cookie headers -- but keeps custom
headers such as X-Api-Key, re-sends a form or JSON body (a password, on a
307), and puts back a session's own login. request() follows a redirect
only within the same host (http -> https is fine) and refuses any other,
naming where it pointed, so the address can be corrected instead.

Sizes: aiohttp unpacks compressed answers as it reads them, so a few
kilobytes on the wire can become gigabytes. read_*() stop at a limit far
past anything these services really send.
"""

from __future__ import annotations

import json
from typing import Any, List, Union

import aiohttp
from yarl import URL

REDIRECTS = frozenset({301, 302, 303, 307, 308})
MAX_REDIRECTS = 5
MAX_BODY = 16 * 1024 * 1024


class RedirectRefused(aiohttp.ClientError):
    """A redirect to another host (or from https down to http)."""


class TooBig(aiohttp.ClientError):
    """An answer far bigger than the service ever sends."""


async def request(session: aiohttp.ClientSession, method: str,
                  url: Union[str, URL], **kwargs: Any
                  ) -> aiohttp.ClientResponse:
    """session.request(), following same-host redirects only. Use as
    `async with await request(...) as response:`."""
    target = url if isinstance(url, URL) else URL(url)
    for _ in range(MAX_REDIRECTS + 1):
        response = await session.request(method, target,
                                         allow_redirects=False, **kwargs)
        if response.status not in REDIRECTS:
            return response
        location = response.headers.get("Location", "")
        response.release()
        following = target.join(URL(location)) if location else None
        if following is None or following.host != target.host or (
                target.scheme == "https" and following.scheme != "https"):
            where = following.host if following is not None else "nowhere"
            raise RedirectRefused(
                f"{target.host} redirects to {where} -- set the address to "
                "where it really is")
        if following.query_string:
            kwargs.pop("params", None)      # the new address carries them
        if response.status == 303 and method != "HEAD" or (
                response.status in (301, 302) and method == "POST"):
            # "See other": fetch it, don't post to it again (what browsers
            # and aiohttp do). 307 and 308 keep the method and body.
            method = "GET"
            kwargs.pop("data", None)
            kwargs.pop("json", None)
        target = following
    raise RedirectRefused(f"{target.host} redirects too many times")


async def read_bytes(response: aiohttp.ClientResponse,
                     limit: int = MAX_BODY) -> bytes:
    if (response.content_length or 0) > limit:
        raise TooBig(f"an answer far bigger than expected "
                     f"({response.content_length} bytes)")
    chunks: List[bytes] = []
    total = 0
    async for chunk in response.content.iter_chunked(64 * 1024):
        total += len(chunk)
        if total > limit:
            raise TooBig(f"an answer far bigger than expected (over "
                         f"{limit} bytes)")
        chunks.append(chunk)
    return b"".join(chunks)


async def read_text(response: aiohttp.ClientResponse,
                    limit: int = MAX_BODY) -> str:
    body = await read_bytes(response, limit)
    try:
        encoding = response.get_encoding()
    except Exception:          # no charset given and nothing to guess from
        encoding = "utf-8"
    return body.decode(encoding or "utf-8", "replace")


async def read_json(response: aiohttp.ClientResponse,
                    limit: int = MAX_BODY) -> Any:
    """Parsed JSON. Raises ValueError when it isn't JSON, TooBig when it's
    far too big."""
    return json.loads(await read_text(response, limit))
