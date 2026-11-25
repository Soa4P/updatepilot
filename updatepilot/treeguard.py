"""Caps on the archive we are about to unpack for review (audit finding #1).

Why this exists
---------------
Review mode materialises a PR head tree with ``git archive`` → ``tar -x`` and then reads
files out of it. The archive is built from a *fetched ref* — i.e. from content a PR author
controls. Nothing bounded how much that is, so a pull request with a hundred thousand
files (or one 5 GB file) could fill the runner's disk or run out its wall clock before the
reviewer ever looked at a line of code. That is a denial-of-service on the review job, and
the reviewer is the thing that is supposed to catch problems.

Two properties matter and are checked separately:

* **size** — per member, summed, and the archive itself. A tar header states each member's
  size, so all three are known *before* a single byte is written to disk;
* **paths** — a member escaping the extraction root would write outside the temp directory.
  ``git archive`` does not produce such names, but the archive is the one input here that
  comes from somebody else, so it is validated like any other untrusted input.

The limits are deliberately generous: a large real repository is tens of thousands of files
and well under a hundred megabytes.

What this does **not** do: stop a PR from being large in *lines* (the reviewer's own byte
caps handle that), or bound CPU spent inside the model. It bounds the disk/time a single
run can be made to spend before review begins.
"""

from __future__ import annotations

import tarfile
from pathlib import Path

#: A review tree is source code. 512 MiB of archive is far beyond any real repository and
#: still small enough that a runner will not notice it.
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024

#: Total unpacked size. Higher than the archive because ``git archive`` compresses.
MAX_UNPACKED_BYTES = 1024 * 1024 * 1024

#: One member. A single file bigger than this is not something a review can read anyway.
MAX_MEMBER_BYTES = 64 * 1024 * 1024

#: Member count. Linux checks out ~80k files; a PR that doubles that is a red flag, not a
#: normal review target.
MAX_MEMBERS = 120_000

#: Wall-clock ceiling for the extraction step (seconds).
EXTRACT_TIMEOUT_SECONDS = 180


class TreeTooLargeError(RuntimeError):
    """The review tree exceeds a cap; the run refuses instead of unpacking it."""


def _unsafe(name: str) -> bool:
    if name.startswith(("/", "\\")):
        return True
    parts = name.replace("\\", "/").split("/")
    return ".." in parts


def assert_tree_within_limits(archive: Path, *, max_archive_bytes: int | None = None) -> dict:
    """Validate the archive's size profile and member names; return the measured numbers.

    Reads only tar headers, so a hostile archive is rejected without being expanded.
    """
    archive = Path(archive)
    limit = MAX_ARCHIVE_BYTES if max_archive_bytes is None else max_archive_bytes
    try:
        size = archive.stat().st_size
    except OSError as exc:
        raise TreeTooLargeError(f"cannot stat archive: {exc}") from exc
    if size > limit:
        raise TreeTooLargeError(
            f"archive is {size} bytes, over the {limit} cap "
            "(a PR this large is refused before unpacking)"
        )

    members = 0
    total = 0
    try:
        with tarfile.open(archive, "r:") as tar:
            for member in tar:
                members += 1
                if members > MAX_MEMBERS:
                    raise TreeTooLargeError(
                        f"archive has more than {MAX_MEMBERS} members; refusing to unpack"
                    )
                if _unsafe(member.name):
                    raise TreeTooLargeError(
                        f"archive member escapes the extraction root: {member.name}"
                    )
                if member.isfile():
                    if member.size > MAX_MEMBER_BYTES:
                        raise TreeTooLargeError(
                            f"member {member.name} is {member.size} bytes, "
                            f"over the {MAX_MEMBER_BYTES} per-file cap"
                        )
                    total += member.size
                    if total > MAX_UNPACKED_BYTES:
                        raise TreeTooLargeError(
                            f"archive unpacks to more than {MAX_UNPACKED_BYTES} bytes"
                        )
    except tarfile.TarError as exc:
        raise TreeTooLargeError(f"archive is not a readable tar: {exc}") from exc

    return {"archive_bytes": size, "members": members, "unpacked_bytes": total}
