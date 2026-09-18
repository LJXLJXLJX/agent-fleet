"""Git Smart HTTP request-body parsing (chunked/gzip), decoupled from server.py."""

from __future__ import annotations

import re
import zlib
from typing import BinaryIO

from .services.git import GitMirrorError

_MAX_GIT_REQUEST_BYTES = 16 * 1024 * 1024
_MAX_GIT_CHUNKS = 64 * 1024
_MAX_GIT_CHUNK_LINE_BYTES = 1024
_MAX_GIT_TRAILER_BYTES = 16 * 1024


class GitRequestBodyError(ValueError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def _read_git_request_line(stream: BinaryIO, description: str) -> bytes:
    line = stream.readline(_MAX_GIT_CHUNK_LINE_BYTES + 1)
    if not line:
        raise GitRequestBodyError(400, f"Git chunked {description} ended early")
    if len(line) > _MAX_GIT_CHUNK_LINE_BYTES:
        raise GitRequestBodyError(400, f"Git chunked {description} is too long")
    if not line.endswith(b"\r\n"):
        raise GitRequestBodyError(400, f"Git chunked {description} is malformed")
    return line[:-2]


def _read_git_request_exact(stream: BinaryIO, length: int) -> bytes:
    body = bytearray()
    while len(body) < length:
        chunk = stream.read(min(1024 * 1024, length - len(body)))
        if not chunk:
            raise GitRequestBodyError(400, "Git request body ended early")
        body.extend(chunk)
    return bytes(body)


def read_chunked_git_request_body(stream: BinaryIO) -> bytes:
    body = bytearray()
    chunks = 0
    while True:
        size_line = _read_git_request_line(stream, "chunk size")
        size_token = size_line.split(b";", 1)[0].strip()
        if not size_token or re.fullmatch(rb"[0-9A-Fa-f]+", size_token) is None:
            raise GitRequestBodyError(400, "Git chunked request has invalid chunk size")
        size = int(size_token, 16)
        if size > _MAX_GIT_REQUEST_BYTES - len(body):
            raise GitRequestBodyError(413, "Git request body is too large")
        if size == 0:
            break
        chunks += 1
        if chunks > _MAX_GIT_CHUNKS:
            raise GitRequestBodyError(400, "Git chunked request has too many chunks")
        body.extend(_read_git_request_exact(stream, size))
        if _read_git_request_exact(stream, 2) != b"\r\n":
            raise GitRequestBodyError(400, "Git chunked request is malformed")

    trailer_bytes = 0
    while True:
        trailer = _read_git_request_line(stream, "trailer")
        trailer_bytes += len(trailer) + 2
        if trailer_bytes > _MAX_GIT_TRAILER_BYTES:
            raise GitRequestBodyError(400, "Git chunked trailers are too large")
        if not trailer:
            return bytes(body)
        name, separator, _value = trailer.partition(b":")
        if not separator or not name.strip() or name[:1] in b" \t":
            raise GitRequestBodyError(400, "Git chunked trailer is malformed")


def decode_git_request_body(content: bytes, encoding: str) -> bytes:
    normalized = encoding.strip().lower()
    if normalized in {"", "identity"}:
        return content
    if normalized not in {"gzip", "x-gzip"}:
        raise GitMirrorError("unsupported Git request content encoding")
    try:
        decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
        decoded = decompressor.decompress(content, _MAX_GIT_REQUEST_BYTES + 1)
        if len(decoded) > _MAX_GIT_REQUEST_BYTES or decompressor.unconsumed_tail:
            raise GitMirrorError("decompressed Git request body is too large")
        decoded += decompressor.flush(_MAX_GIT_REQUEST_BYTES + 1 - len(decoded))
    except zlib.error as exc:
        raise GitMirrorError("invalid gzip Git request body") from exc
    if (
        len(decoded) > _MAX_GIT_REQUEST_BYTES
        or decompressor.unconsumed_tail
        or not decompressor.eof
    ):
        raise GitMirrorError("decompressed Git request body is too large or incomplete")
    return decoded
