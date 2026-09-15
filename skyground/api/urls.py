"""Turning storage URLs into something a client can actually call."""

from __future__ import annotations

from urllib.parse import urljoin

from fastapi import Request


def absolute(request: Request, url: str) -> str:
    """Resolve a storage URL against the host the request came in on.

    The local backend signs paths — `/media/blob/...` — because it has no idea
    what name the studio answers to; R2 signs a full address at the bucket.
    A client holding only the response cannot tell the difference and should not
    have to: it gets something it can hand straight to an HTTP call.

    The application runs behind Render's proxy with `--proxy-headers`, so the
    scheme and host here are the public ones, not the container's.
    """
    if not url or "://" in url:
        return url
    return urljoin(str(request.base_url), url.lstrip("/"))
