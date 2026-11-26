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
#: Response cap: a registry (or something impersonating one) answering gigabytes
#: must fail loudly, not exhaust the runner's memory.
MAX_RESPONSE_BYTES = 10 * 1024 * 1024

_PYPI = "https://pypi.org/pypi/{name}/json"
_GH_TAGS = "https://api.github.com/repos/{owner}/{repo}/tags"
_GH_RELEASE = "https://api.github.com/repos/{owner}/{repo}/releases/latest"
_NPM = "https://registry.npmjs.org/{name}/latest"
#: crates.io's documented API. It requires a descriptive User-Agent (we send one)
#: and answers both the version and the repository URL, so cargo needs no second
#: egress host for changelog lookup.
_CRATES = "https://crates.io/api/v1/crates/{name}"


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
            raw = response.read(MAX_RESPONSE_BYTES)
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
            raw = response.read(MAX_RESPONSE_BYTES)
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
        # ``GET /repos/{o}/{r}/tags`` answers ``{"name": "v7.0.1", "commit": …}`` — no
        # ``ref`` field at all (that shape belongs to ``/git/refs/tags``). Reading
        # ``ref`` here meant this function returned None for *every* real action, so
        # action bumps silently never happened; the tests passed because their mock
        # used the wrong shape too. Both spellings are accepted now.
        # ``fullmatch`` with an anchored number, not ``search``: tags like
        # ``v3.2.2-node20`` end in a number that is a *node runtime* version, and a
        # trailing ``search`` reads it as major 20 — which is how a bogus
        # "upload-artifact v7 → v20" PR got opened. A release tag is a bare version.
        tag = str(item.get("name") or item.get("ref") or "")
        match = re.fullmatch(r"(?:refs/tags/)?v?(\d+(?:\.\d+)*)", tag)
        if not match:
            continue
        major = int(match.group(1).split(".", 1)[0])
        if major > best_major:
            best_major, best_ref = major, f"v{major}"
    return best_ref


def npm_latest(name: str) -> str | None:
    """Latest published version for one npm package (the ``latest`` dist-tag)."""
    body = _get_json(_NPM.format(name=urllib.parse.quote(name)))
    if not isinstance(body, dict):
        return None
    version = str(body.get("version") or "").strip()
    return version or None


def crates_latest(name: str) -> str | None:
    """Latest stable version of one crate, or None.

    ``max_stable_version`` already excludes pre-releases; ``max_version`` is the
    fallback for crates that only ever published a pre-release (in which case the
    version classifies as one and the planner drops it — the same policy pip and
    npm already get). A yanked latest shows up as ``null``, which we treat as "no
    answer" rather than guessing the previous release.
    """
    body = _get_json(_CRATES.format(name=urllib.parse.quote(name)))
    if not isinstance(body, dict):
        return None
    crate = body.get("crate")
    if not isinstance(crate, dict):
        return None
    version = str(crate.get("max_stable_version") or crate.get("max_version") or "").strip()
    return version or None


def crates_repository_slug(name: str) -> str | None:
    """``owner/repo`` from a crate's ``repository`` field, GitHub-hosted only.

    Same rule as npm: the changelog fetch goes to api.github.com, an
    already-allowed host, so a crate whose repository lives anywhere else gets no
    notes instead of widening the egress set.
    """
    body = _get_json(_CRATES.format(name=urllib.parse.quote(name)))
    if not isinstance(body, dict):
        return None
    crate = body.get("crate")
    if not isinstance(crate, dict):
        return None
    repo = crate.get("repository")
    if not isinstance(repo, str):
        return None
    match = re.search(r"github\.com[/:]([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$", repo)
    return match.group(1) if match else None


def npm_repository_slug(name: str) -> str | None:
    """``owner/repo`` from the package's repository field, GitHub-hosted only.

    The changelog fetch goes to api.github.com — an already-allowed host — so a
    package whose repository points anywhere else simply gets no notes rather
    than widening the egress set.
    """
    body = _get_json(_NPM.format(name=urllib.parse.quote(name)))
    if not isinstance(body, dict):
        return None
    repo = body.get("repository")
    if isinstance(repo, dict):
        repo = repo.get("url")
    if not isinstance(repo, str):
        return None
    match = re.search(r"github\.com[/:]([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$", repo)
    return match.group(1) if match else None


_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_UNCLOSED_COMMENT = re.compile(r"<!--.*$", re.DOTALL)
_HTML_TAG = re.compile(r"<\s*/?\s*[a-zA-Z][^>]*>")
#: Zero-width and bidi-control characters, spelled with escapes rather than the literal
#: code points: an invisible character in this file is a hazard for the next reader.
_INVISIBLE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u206f]")
_EXCESS_BLANK_LINES = re.compile(r"\n{3,}")


def sanitize_release_notes(text: str, *, limit: int = 1200) -> str:
    """Make upstream release notes safe to put in a PR body and a review prompt.

    The text is written by whoever publishes the package — an arbitrary third party —
    and it ends up in a PR this bot signs and in the reviewer's prompt (audit S-UP-06).
    Two payloads matter and neither is visible when the PR is read on GitHub:

    * HTML comments, which render as nothing but are still *in the body* — the classic
      place to hide an instruction that only the model and a diff reader will see;
    * inline HTML (``<img src=...>``, ``<a>``), which can phone home when previewed.

    Zero-width and bidi controls go too: they make text read differently from its bytes,
    which is exactly what a hostile changelog wants. What is left is plain Markdown, and
    the caller labels it as unvetted upstream text.
    """
    if not text:
        return ""
    cleaned = _HTML_COMMENT.sub("", text)
    cleaned = _UNCLOSED_COMMENT.sub("", cleaned)
    cleaned = _HTML_TAG.sub("", cleaned)
    cleaned = _INVISIBLE.sub("", cleaned)
    cleaned = _EXCESS_BLANK_LINES.sub("\n\n", cleaned)
    return cleaned.strip()[:limit]


def github_latest_release_notes(slug: str, token: str | None) -> str | None:
    """Body of the latest GitHub release for ``owner/repo``, bounded and plain.

    Powers the changelog excerpt in bot PR bodies. Anything the API cannot
    answer (rate limit, no releases, private) yields None — a PR without notes
    is still a PR, not a failure.
    """
    owner, _, repo = slug.partition("/")
    if not owner or not repo:
        return None
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = _GH_RELEASE.format(owner=urllib.parse.quote(owner), repo=urllib.parse.quote(repo))
    parsed = urllib.parse.urlparse(url)
    _assert_routable_hostname(parsed.hostname)
    request = urllib.request.Request(url, headers=headers)
    try:
        opener = urllib.request.build_opener(_NoRedirect())
        with opener.open(request, timeout=REQUEST_TIMEOUT) as response:
            raw = response.read(MAX_RESPONSE_BYTES)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
        return None  # notes are best-effort by contract
    try:
        body = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    text = sanitize_release_notes(str(body.get("body") or ""))
    return text or None
