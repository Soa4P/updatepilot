"""Registry lookups: "what is the newest version of X".

Only two outbound hosts in v1 (PyPI JSON API, GitHub tags API) plus the model
endpoint. Every request goes through the same guard as the reviewer: https, a
resolvable globally-routable hostname, no redirects. A registry that answers
garbage yields "no update found" — the scan keeps going rather than inventing
versions.
"""

from __future__ import annotations

import ipaddress
import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "updatepilot/1.0 (+https://github.com/Soa4P/updatepilot)"
REQUEST_TIMEOUT = 30.0

_PYPI = "https://pypi.org/pypi/{name}/json"
_GH_TAGS = "https://api.github.com/repos/{owner}/{repo}/tags"


class RegistryError(RuntimeError):
    """The registry could not be consulted; the planner skips this dependency."""


def _assert_routable_hostname(hostname: str) -> None:
    """Resolve once, refuse anything not globally routable (mirrors review.model)."""
    if not hostname:
        raise RegistryError("registry endpoint missing hostname")
    try:
        infos = socket.getaddrinfo(hostname, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise RegistryError(f"cannot resolve {hostname!r}: {exc}") from exc
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global:
            raise RegistryError(f"registry resolves to non-public address {address}")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _get_json(url: str) -> dict | list | None:
    """GET one JSON document; None on 404, RegistryError on everything else."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise RegistryError(f"registry URL must be https with a hostname: {url!r}")
    _assert_routable_hostname(parsed.hostname)
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    try:
        opener = urllib.request.build_opener(_NoRedirect())
        with opener.open(request, timeout=REQUEST_TIMEOUT) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise RegistryError(f"registry returned HTTP {exc.code} for {url}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RegistryError(f"cannot reach registry {url}: {type(exc).__name__}: {exc}") from exc
    try:
        body = json.loads(raw.decode("utf-8", "replace"))
    except ValueError as exc:
        raise RegistryError(f"registry answered non-JSON for {url}") from exc
    if not isinstance(body, (dict, list)):
        raise RegistryError(f"registry answered an unexpected shape for {url}")
    return body


def pypi_latest(name: str) -> str | None:
    """Latest stable upload for one PyPI project, or None (missing / pre-release only)."""
    body = _get_json(_PYPI.format(name=urllib.parse.quote(name)))
    if not isinstance(body, dict):
        return None
    info = body.get("info") or {}
    version = str(info.get("version") or "").strip()
    return version or None


_GITREF = re.compile(r"refs/tags/")


def github_action_latest(action: str, token: str | None) -> str | None:
    """Highest ``vN`` tag for one action repo, or None.

    Auth is optional: unauthenticated requests work for public repos, and the
    token (the caller's GITHUB_TOKEN) only lifts the rate limit.
    """
    owner, _, repo = action.partition("/")
    if not owner or not repo:
        raise RegistryError(f"malformed action slug: {action!r}")
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = _GH_TAGS.format(owner=urllib.parse.quote(owner), repo=urllib.parse.quote(repo))
    parsed = urllib.parse.urlparse(url)
    _assert_routable_hostname(parsed.hostname)
    request = urllib.request.Request(url, headers=headers)
    try:
        opener = urllib.request.build_opener(_NoRedirect())
        with opener.open(request, timeout=REQUEST_TIMEOUT) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 429):
            raise RegistryError(f"GitHub API rate limited while reading {action}") from exc
        if exc.code == 404:
            return None
        raise RegistryError(f"GitHub API returned HTTP {exc.code} for {action}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RegistryError(f"cannot reach GitHub API for {action}: {exc}") from exc
    try:
        body = json.loads(raw.decode("utf-8", "replace"))
    except ValueError as exc:
        raise RegistryError(f"GitHub API answered non-JSON for {action}") from exc
    if not isinstance(body, list):
        return None
    best_major = -1
    best_ref: str | None = None
    for item in body:
        if not isinstance(item, dict):
            continue
        ref = str(item.get("ref") or "")
        match = re.fullmatch(r"refs/tags/v?(\d+)", ref)
        if not match:
            continue
        major = int(match.group(1))
        if major > best_major:
            best_major, best_ref = major, f"v{major}"
    return best_ref
