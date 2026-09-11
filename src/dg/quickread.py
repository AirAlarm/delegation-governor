"""Prepare a one-shot, XML-delimited corpus for read-only delegation."""
from __future__ import annotations

import html
import stat
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class FileInput:
    path: str
    text: str
    lines: int
    size: int


def _read_files(paths: list[str]) -> list[FileInput]:
    resolved = [(path, Path(path)) for path in paths]
    for display, path in resolved:
        try:
            info = path.stat()
        except FileNotFoundError as exc:
            raise ValueError(f"file not found: {display}")
        except OSError as exc:
            detail = exc.strerror or str(exc)
            raise ValueError(f"unreadable file: {display}: {detail}") from exc
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"not a file: {display}")

    files = []
    for display, path in resolved:
        try:
            data = path.read_bytes()
        except OSError as exc:
            detail = exc.strerror or str(exc)
            raise ValueError(f"unreadable file: {display}: {detail}") from exc
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"unreadable file: {display}: not valid UTF-8") from exc
        files.append(FileInput(display, text, len(text.splitlines()), len(data)))
    return files


def _corpus(files: list[FileInput]) -> str:
    chunks = []
    for file in files:
        path = html.escape(file.path, quote=True)
        chunks.append(f'<file path="{path}">\n{file.text}</file>\n\n')
    return "".join(chunks)


def run(args) -> int:
    """Validate and read every path before emitting an all-or-nothing corpus."""
    try:
        files = _read_files(args.paths)
    except ValueError as exc:
        print(f"dg quickread: {exc}", file=sys.stderr)
        return 1

    corpus = _corpus(files)
    approx_tokens = len(corpus) // 4
    for file in files:
        line_label = "line" if file.lines == 1 else "lines"
        byte_label = "byte" if file.size == 1 else "bytes"
        print(f"{file.path}: {file.lines} {line_label}, {file.size} {byte_label}",
              file=sys.stderr)
    file_label = "file" if len(files) == 1 else "files"
    print(f"[quickread: {len(files)} {file_label}, ~{approx_tokens} input tokens]",
          file=sys.stderr)
    sys.stdout.write(corpus)
    return 0
