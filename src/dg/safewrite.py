"""Atomically write worker-generated content to one file."""
from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path


_OPENING_FENCE = re.compile(br"```[^`\r\n]*")


def strip_outer_fence(content: bytes) -> bytes:
    """Remove one enclosing triple-backtick fence without touching the body."""
    lines = content.splitlines(keepends=True)
    if len(lines) < 2:
        return content
    opening = lines[0].rstrip(b"\r\n")
    closing = lines[-1].rstrip(b"\r\n")
    if _OPENING_FENCE.fullmatch(opening) and closing == b"```":
        return b"".join(lines[1:-1])
    return content


def write(target: str | Path, content: bytes, *, force: bool = False) -> None:
    """Write content through a same-directory temporary file and atomic rename."""
    path = Path(target)
    if not force and os.path.lexists(path):
        raise FileExistsError(f"target already exists: {path}")

    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                                     dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if not force and os.path.lexists(path):
            raise FileExistsError(f"target already exists: {path}")
        os.replace(temporary, path)
        temporary = ""
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def run(args) -> int:
    """Read generated content, strip one outer fence, and publish it safely."""
    source = Path(args.source)
    target = Path(args.target)
    try:
        content = source.read_bytes()
        write(target, strip_outer_fence(content), force=args.force)
    except OSError as exc:
        detail = exc.strerror or str(exc)
        print(f"dg safewrite: {detail}", file=sys.stderr)
        return 1
    print(target)
    return 0
