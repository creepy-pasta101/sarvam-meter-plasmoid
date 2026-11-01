# SPDX-License-Identifier: MIT
"""The recording reverse proxy (SPEC section 8).

A small ``ThreadingHTTPServer`` that sits in front of ``proxy.upstream_base``
(``https://api.sarvam.ai`` by default), relays requests and responses without
buffering them, and records one ledger row per attempt that reached upstream.

Metering rules (SPEC 8.7):

=======  ==========  =============================================
api      unit        quantity
=======  ==========  =============================================
chat     tokens      ``in + out + cached`` from the response usage
stt      seconds     ``audio.duration_seconds()`` of the upload
tts      chars       code points of the request ``text``
translate/transliterate  chars   code points of the request ``input``
lid      requests    1
=======  ==========  =============================================

Secrets (``api-subscription-key``, ``authorization``, ``x-management-key``) are
forwarded to upstream untouched but are never logged, never put into ``meta``
and never written to the ledger.
"""

from __future__ import annotations

import http.client
import json
import logging
import math
import re
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import urlsplit

from .audio import container_of, duration_seconds
from .ledger import utc_now_iso
from .rates import cost_for

__all__ = [
    "ProxyServer",
    "classify",
    "chat_usage",
    "chat_stream_usage",
    "tts_char_count",
    "record_from_exchange",
]

_LOG = logging.getLogger("sarvam_meter.proxy")

#: Request bodies are streamed upstream in chunks of this size.
CHUNK_SIZE = 64 * 1024
#: Only this many bytes of a request body are kept for metering (model, text,
#: ``input``, and the audio header region).  The rest is never held in memory.
HEAD_LIMIT = 64 * 1024
#: Bounded tail buffer for SSE usage extraction.
SSE_TAIL_LIMIT = 64 * 1024
#: Bounded capture of a non-SSE response body (a rolling tail is kept beyond).
RES_BODY_LIMIT = 1024 * 1024
#: Socket timeout for the upstream connection, in seconds.
UPSTREAM_TIMEOUT = 300.0

#: Request headers that are never forwarded upstream (SPEC 8.3).
_SKIP_REQUEST_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "transfer-encoding",
        "connection",
        "accept-encoding",
        "proxy-connection",
        "keep-alive",
        "upgrade",
        "te",
        "expect",
    }
)

#: Response headers we re-generate ourselves.
_SKIP_RESPONSE_HEADERS = frozenset(
    {"transfer-encoding", "connection", "content-length", "keep-alive", "trailer"}
)

_USAGE_RE = re.compile(r'"usage"\s*:\s*\{')


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------

_CHAT_MARKERS = ("/v1/chat/completions", "/v2/chat/completions", "/chat/completions")

_TABLE = (
    ("stt", ("/speech-to-text", "/speech-to-text-translate")),
    ("tts", ("/text-to-speech",)),
    ("translate", ("/translate",)),
    ("transliterate", ("/transliterate",)),
    ("lid", ("/text-lid",)),
)


def classify(method: str, path: str) -> dict:
    """Map a request path onto an api name.

    Returns ``{"api": ..., "metered": bool, "stream": bool}``.  Matching is a
    substring/prefix test on the *path* only - the query string is ignored.

    ``stream`` is reported as ``False`` here because streaming is a property of
    the request *body* (``"stream": true``), which ``classify`` never sees.  The
    proxy handler and :func:`record_from_exchange` refine it from the body and
    from the upstream ``Content-Type``.
    """
    del method  # routing is path-only; the signature is fixed by the spec
    path_only = _strip_query(path or "")
    for api, markers in _TABLE:
        for marker in markers:
            if marker in path_only:
                return {"api": api, "metered": True, "stream": False}
    for marker in _CHAT_MARKERS:
        if marker in path_only:
            return {"api": "chat", "metered": True, "stream": False}
    return {"api": "other", "metered": False, "stream": False}


def _strip_query(path: str) -> str:
    if not path:
        return ""
    return str(path).split("?", 1)[0].split("#", 1)[0]


def _wants_stream(body: Any) -> bool:
    obj = _loads_json(body)
    if isinstance(obj, dict):
        value = obj.get("stream")
        if value is True:
            return True
        if isinstance(value, str) and value.strip().lower() == "true":
            return True
    return False


# ---------------------------------------------------------------------------
# JSON / usage helpers
# ---------------------------------------------------------------------------

def _decode(raw: Any) -> str:
    if isinstance(raw, str):
        return raw
    if isinstance(raw, (bytes, bytearray, memoryview)):
        return bytes(raw).decode("utf-8", "replace")
    return str(raw)


def _loads_json(raw: Any) -> Optional[Any]:
    if raw is None:
        return None
    if isinstance(raw, (bytes, bytearray, memoryview)):
        if not bytes(raw).strip():
            return None
    elif isinstance(raw, str):
        if not raw.strip():
            return None
    else:
        return None
    try:
        return json.loads(_decode(raw))
    except Exception:
        return None


def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return int(value)
    return None


def _first_int(source: dict, keys) -> Optional[int]:
    for key in keys:
        if key in source:
            value = _as_int(source.get(key))
            if value is not None:
                return value
    return None


def _usage_from_dict(usage: dict) -> Optional[dict]:
    """Normalise an OpenAI-style usage object; ``None`` when it carries nothing."""
    if not isinstance(usage, dict):
        return None
    in_tokens = _first_int(usage, ("prompt_tokens", "input_tokens", "in_tokens"))
    out_tokens = _first_int(usage, ("completion_tokens", "output_tokens", "out_tokens"))
    if in_tokens is None and out_tokens is None:
        return None
    cached = _first_int(usage, ("cached_tokens", "cache_read_input_tokens"))
    if cached is None:
        for key in ("prompt_tokens_details", "input_tokens_details", "details"):
            details = usage.get(key)
            if isinstance(details, dict):
                cached = _first_int(details, ("cached_tokens", "cached"))
                if cached is not None:
                    break
    return {
        "in_tokens": in_tokens or 0,
        "out_tokens": out_tokens or 0,
        "cached_tokens": cached or 0,
    }


def chat_usage(body: bytes) -> Optional[dict]:
    """Extract ``{"in_tokens","out_tokens","cached_tokens"}`` from a JSON body.

    Returns ``None`` when the body has no ``usage`` object (SPEC edge case 9).
    If the body is not valid JSON - e.g. a truncated capture - the last
    ``"usage"`` object in the raw text is still tried, because usage is
    normally the final field of a chat completion response.
    """
    if not body:
        return None
    obj = _loads_json(body)
    if isinstance(obj, dict):
        found = _usage_from_dict(obj.get("usage"))
        if found is not None:
            return found
    text = _decode(body)
    if '"usage"' not in text:
        return None
    index = text.rfind('"usage"')
    while index >= 0:
        match = _USAGE_RE.search(text, index)
        if match is None:
            index = text.rfind('"usage"', 0, index)
            continue
        try:
            parsed, _end = json.JSONDecoder().raw_decode(text[match.end() - 1:])
        except ValueError:
            index = text.rfind('"usage"', 0, index)
            continue
        found = _usage_from_dict(parsed)
        if found is not None:
            return found
        index = text.rfind('"usage"', 0, index)
    return None


def chat_stream_usage(lines: list) -> Optional[dict]:
    """Extract usage from SSE ``data:`` chunks; the last usage wins."""
    if not lines:
        return None
    found = None
    for line in lines:
        text = _decode(line).strip()
        if not text.startswith("data:"):
            continue
        payload = text[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except Exception:
            continue
        if isinstance(obj, dict):
            candidate = _usage_from_dict(obj.get("usage"))
            if candidate is not None:
                found = candidate
    return found


def tts_char_count(body: bytes) -> Optional[int]:
    """Code-point count of the TTS request text (never a byte count)."""
    obj = _loads_json(body)
    if not isinstance(obj, dict):
        return None
    text = obj.get("text")
    if isinstance(text, str):
        return len(text)
    inputs = obj.get("inputs")
    if isinstance(inputs, str):
        return len(inputs)
    if isinstance(inputs, list):
        total = 0
        for item in inputs:
            if not isinstance(item, str):
                return None
            total += len(item)
        return total
    return None


def _input_char_count(obj: Any) -> Optional[int]:
    if not isinstance(obj, dict):
        return None
    for key in ("input", "inputs", "text"):
        value = obj.get(key)
        if isinstance(value, str):
            return len(value)
        if isinstance(value, list):
            total = 0
            ok = True
            for item in value:
                if isinstance(item, str):
                    total += len(item)
                else:
                    ok = False
                    break
            if ok:
                return total
    return None


def _resolve_model(resp_json: Any, req_json: Any) -> Optional[str]:
    for source in (resp_json, req_json):
        if isinstance(source, dict):
            value = source.get("model")
            if isinstance(value, str) and value:
                return value
    return None


# ---------------------------------------------------------------------------
# recording
# ---------------------------------------------------------------------------

def record_from_exchange(config, ledger, method, path, req_headers, req_body_head,
                         status, resp_body, *, duration_hint=None) -> None:
    """Price one proxied exchange and write a ledger row.

    ``duration_hint`` carries the STT audio duration measured by the caller
    (the proxy only ever holds the head of the upload, so it measures it while
    streaming).  Never raises - metering failures must not break the proxy.
    """
    del config, req_headers  # kept for the pinned signature; nothing to read
    try:
        info = classify(method, path)
        api = info["api"]
        if not info["metered"]:
            return

        status_code = None
        if status is not None:
            try:
                status_code = int(status)
            except (TypeError, ValueError):
                status_code = None
        succeeded = status_code is None or 200 <= status_code < 400

        req_json = _loads_json(req_body_head)
        resp_json = _loads_json(resp_body)
        model = _resolve_model(resp_json, req_json)

        unit = None
        quantity = None
        cost = 0.0
        estimated = False

        if api == "chat":
            unit = "tokens"
            usage = chat_usage(resp_body)
            if usage is None and resp_body:
                usage = chat_stream_usage(_decode(resp_body).splitlines())
            if succeeded and usage is not None:
                in_tokens = float(usage["in_tokens"])
                out_tokens = float(usage["out_tokens"])
                # prompt_tokens already include the cached ones; never count twice.
                cached_tokens = min(float(usage["cached_tokens"]), in_tokens)
                # quantity is the total tokens processed, for display in the widget
                quantity = in_tokens + out_tokens
                # output tokens are priced 4x input, so they must be passed apart
                cost = cost_for(
                    "chat", model, max(in_tokens - cached_tokens, 0.0), "tokens",
                    cached_quantity=cached_tokens,
                    out_quantity=out_tokens,
                )
            else:
                estimated = True
        elif api == "stt":
            unit = "seconds"
            if succeeded and duration_hint is not None:
                try:
                    candidate = float(duration_hint)
                except (TypeError, ValueError):
                    candidate = None
                if candidate is not None and math.isfinite(candidate) and candidate >= 0:
                    quantity = candidate
                    cost = cost_for("stt", model, quantity, "seconds")
            if quantity is None:
                estimated = True
        elif api == "tts":
            unit = "chars"
            if succeeded:
                count = tts_char_count(req_body_head)
                if count is not None:
                    quantity = float(count)
                    cost = cost_for("tts", model, quantity, "chars")
            if quantity is None:
                estimated = True
        elif api in ("translate", "transliterate"):
            unit = "chars"
            if succeeded:
                count = _input_char_count(req_json)
                if count is not None:
                    quantity = float(count)
                    # price is unpublished -> cost_for returns 0.0
                    cost = cost_for(api, model, quantity, "chars")
            if quantity is None:
                estimated = True
        elif api == "lid":
            unit = "requests"
            if succeeded:
                quantity = 1.0
                cost = cost_for("lid", model, quantity, "requests")
            else:
                estimated = True
        else:  # pragma: no cover - unreachable, "other" is not metered
            return

        if not succeeded:
            # An errored call is not billable: keep the row, drop the numbers.
            quantity = None
            cost = 0.0
            estimated = True

        try:
            cost_value = round(float(cost), 6)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            cost_value = 0.0
        if not math.isfinite(cost_value):
            cost_value = 0.0

        meta = {
            "api": api,
            "model": model,
            "path": _strip_query(path),
            "status": status_code,
        }
        ledger.record_event(
            source="proxy",
            api=api,
            model=model,
            quantity=quantity,
            unit=unit,
            cost=cost_value,
            status=status_code,
            estimated=estimated,
            meta=meta,
        )
        if status_code == 402:
            ledger.set_kv("quota_exhausted_at", utc_now_iso())
    except Exception:
        _LOG.warning("failed to record proxied exchange", exc_info=True)


# ---------------------------------------------------------------------------
# upstream helpers
# ---------------------------------------------------------------------------

class _BodyTooLarge(Exception):
    """Raised while streaming when the body exceeds ``max_body_bytes``."""


class _BadRequestBody(Exception):
    """Raised when the client's request body framing is broken."""


def _open_upstream(upstream) -> http.client.HTTPConnection:
    scheme = (upstream.scheme or "https").lower()
    host = upstream.hostname
    if not host:
        raise ValueError("upstream_base has no host")
    if scheme == "https":
        context = ssl.create_default_context()
        conn = http.client.HTTPSConnection(
            host, upstream.port or 443, timeout=UPSTREAM_TIMEOUT, context=context
        )
    elif scheme == "http":
        conn = http.client.HTTPConnection(
            host, upstream.port or 80, timeout=UPSTREAM_TIMEOUT
        )
    else:
        raise ValueError("unsupported upstream scheme: %r" % (scheme,))
    conn.connect()
    return conn


def _target_path(upstream, raw_path: str) -> str:
    base = (upstream.path or "").rstrip("/")
    return base + raw_path


def _path_is_safe(path: str) -> bool:
    if not path or not path.startswith("/"):
        return False
    if ".." in path:
        return False
    lowered = path.lower()
    for marker in ("http:", "https:", "file:", "ftp:", "//", "\\"):
        if marker in lowered:
            return False
    for char in ("\r", "\n", "\x00"):
        if char in path:
            return False
    return True


# ---------------------------------------------------------------------------
# request handler
# ---------------------------------------------------------------------------

class _ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "sarvam-meter-proxy"
    sys_version = ""

    # -- logging: one line per request, never any header value --------------
    def log_message(self, fmt, *args):  # noqa: A003 - stdlib signature
        return

    def log_request(self, code="-", size="-"):
        logger = getattr(self.server, "logger", None)
        if logger is None:
            return
        try:
            logger.info("%s %s -> %s", self.command, _strip_query(self.path or ""), code)
        except Exception:  # pragma: no cover - defensive
            pass

    def log_error(self, fmt, *args):  # noqa: A003 - stdlib signature
        return

    # -- verbs -------------------------------------------------------------
    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._method_not_allowed()

    def do_DELETE(self):
        self._method_not_allowed()

    def do_PATCH(self):
        self._method_not_allowed()

    def do_OPTIONS(self):
        self._method_not_allowed()

    def do_HEAD(self):
        self._method_not_allowed()

    # -- plumbing ----------------------------------------------------------
    def _method_not_allowed(self):
        self._discard_body()
        self._error(405, "method not allowed", "meter_method_not_allowed")

    def _error(self, status: int, message: str, code: str):
        payload = json.dumps(
            {"error": {"message": message, "code": code}}
        ).encode("utf-8")
        try:
            self.send_response_only(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            self._response_started = True
            if self.command != "HEAD":
                self.wfile.write(payload)
            self.wfile.flush()
        except Exception:  # pragma: no cover - client vanished
            self.close_connection = True
        self.log_request(status)

    def _discard_body(self):
        """Read and drop a small request body so the client sees our response."""
        try:
            if (self.headers.get("Transfer-Encoding") or "").lower().find("chunked") >= 0:
                return
            raw = self.headers.get("Content-Length")
            if not raw:
                return
            remaining = int(raw)
        except (TypeError, ValueError):
            return
        if remaining <= 0 or remaining > HEAD_LIMIT:
            return
        try:
            while remaining > 0:
                chunk = self.rfile.read(min(CHUNK_SIZE, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
        except Exception:  # pragma: no cover - defensive
            pass

    # -- main flow ---------------------------------------------------------
    def _handle(self, method: str):
        self._response_started = False
        self._captured_body = bytearray()
        self._captured_tail = bytearray()

        server = self.server
        config = server.config
        ledger = server.ledger
        proxy_config = config.proxy

        raw_path = self.path or ""
        if not _path_is_safe(raw_path):
            self._discard_body()
            self._error(400, "bad request path", "meter_bad_path")
            return
        path_only = _strip_query(raw_path) or "/"

        transfer_encoding = (self.headers.get("Transfer-Encoding") or "").lower()
        chunked_request = "chunked" in transfer_encoding
        content_length = 0
        if not chunked_request:
            raw_length = self.headers.get("Content-Length")
            if raw_length:
                try:
                    content_length = int(raw_length)
                except (TypeError, ValueError):
                    self._error(400, "invalid content-length", "meter_bad_request")
                    return
                if content_length < 0:
                    self._error(400, "invalid content-length", "meter_bad_request")
                    return
            if content_length > proxy_config.max_body_bytes:
                self._error(413, "request body too large", "meter_body_too_large")
                return

        try:
            conn = _open_upstream(server.upstream)
        except Exception:
            self._error(502, "upstream unreachable", "meter_upstream_error")
            return

        head = bytearray()
        status = None
        try:
            conn.putrequest(
                method, _target_path(server.upstream, raw_path), skip_accept_encoding=True
            )
            self._forward_request_headers(conn, chunked_request, content_length, method)
            conn.endheaders()
            self._relay_request_body(
                conn, chunked_request, content_length, head, proxy_config.max_body_bytes
            )
            response = conn.getresponse()
            status = response.status
            self._relay_response(response)
        except _BodyTooLarge:
            self._error(413, "request body too large", "meter_body_too_large")
        except _BadRequestBody:
            if not self._response_started:
                self._error(400, "bad request body", "meter_bad_request")
        except (BrokenPipeError, ConnectionResetError):
            # The upstream closed while we were still talking to it. If nothing
            # has gone to the client yet, say so instead of returning silence.
            if not self._response_started:
                self._error(502, "upstream closed the connection", "meter_upstream_error")
            self.close_connection = True
        except Exception:
            if not self._response_started:
                self._error(502, "upstream unreachable", "meter_upstream_error")
            else:  # pragma: no cover - mid-stream failure
                self.close_connection = True
        finally:
            try:
                conn.close()
            except Exception:  # pragma: no cover - defensive
                pass

        if status is not None:
            duration_hint = None
            if classify(method, path_only)["api"] == "stt":
                duration_hint = _audio_duration_from_head(bytes(head))
            captured = bytes(self._captured_body) or bytes(self._captured_tail)
            record_from_exchange(
                config,
                ledger,
                method,
                path_only,
                dict(self.headers.items()),
                bytes(head),
                status,
                captured,
                duration_hint=duration_hint,
            )

    def _forward_request_headers(self, conn, chunked_request, content_length, method):
        for name, value in self.headers.items():
            if name.lower() in _SKIP_REQUEST_HEADERS:
                continue
            conn.putheader(name, value)
        conn.putheader("Accept-Encoding", "identity")
        if chunked_request:
            conn.putheader("Transfer-Encoding", "chunked")
        elif content_length or method == "POST":
            conn.putheader("Content-Length", str(content_length))

    def _relay_request_body(self, conn, chunked_request, content_length, head, max_body):
        total = 0
        if chunked_request:
            while True:
                line = self.rfile.readline(65536)
                if not line:
                    raise _BadRequestBody()
                size_field = line.split(b";", 1)[0].strip()
                try:
                    size = int(size_field, 16)
                except ValueError:
                    raise _BadRequestBody()
                if size == 0:
                    while True:
                        trailer = self.rfile.readline(65536)
                        if not trailer or trailer in (b"\r\n", b"\n"):
                            break
                    conn.send(b"0\r\n\r\n")
                    return
                conn.send(b"%x\r\n" % size)
                remaining = size
                while remaining > 0:
                    chunk = self.rfile.read(min(CHUNK_SIZE, remaining))
                    if not chunk:
                        raise _BadRequestBody()
                    remaining -= len(chunk)
                    total += len(chunk)
                    if total > max_body:
                        raise _BodyTooLarge()
                    if len(head) < HEAD_LIMIT:
                        head.extend(chunk[: HEAD_LIMIT - len(head)])
                    conn.send(chunk)
                self.rfile.read(2)  # trailing CRLF of the chunk
                conn.send(b"\r\n")
        else:
            remaining = content_length
            while remaining > 0:
                chunk = self.rfile.read(min(CHUNK_SIZE, remaining))
                if not chunk:
                    raise _BadRequestBody()
                remaining -= len(chunk)
                total += len(chunk)
                if total > max_body:
                    raise _BodyTooLarge()
                if len(head) < HEAD_LIMIT:
                    head.extend(chunk[: HEAD_LIMIT - len(head)])
                conn.send(chunk)

    def _relay_response(self, response):
        status = response.status
        headers = response.getheaders()
        content_type = (response.getheader("Content-Type") or "").lower()
        is_sse = "text/event-stream" in content_type
        length = response.length
        chunked_out = length is None
        self._start_response(status, headers, length, chunked_out)
        self.log_request(status)

        body = self._captured_body
        tail = self._captured_tail

        if is_sse:
            while True:
                line = response.readline()
                if not line:
                    break
                self._write_out(line, chunked_out)
                if line.strip().startswith(b"data:"):
                    tail.extend(line)
                    if len(tail) > SSE_TAIL_LIMIT:
                        del tail[: len(tail) - SSE_TAIL_LIMIT]
        else:
            while True:
                chunk = response.read(CHUNK_SIZE)
                if not chunk:
                    break
                self._write_out(chunk, chunked_out)
                if len(body) + len(chunk) <= RES_BODY_LIMIT:
                    body.extend(chunk)
                else:
                    body.extend(chunk)
                    del body[: len(body) - RES_BODY_LIMIT]

        if chunked_out:
            try:
                self.wfile.write(b"0\r\n\r\n")
            except Exception:  # pragma: no cover - client vanished
                pass
        try:
            self.wfile.flush()
        except Exception:  # pragma: no cover - client vanished
            pass

    def _start_response(self, status, headers, length, chunked_out):
        self.send_response_only(status)
        for name, value in headers:
            if name.lower() in _SKIP_RESPONSE_HEADERS:
                continue
            self.send_header(name, value)
        if chunked_out:
            self.send_header("Transfer-Encoding", "chunked")
        else:
            self.send_header("Content-Length", str(length))
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self._response_started = True

    def _write_out(self, data, chunked_out):
        if not data:
            return
        if chunked_out:
            self.wfile.write(b"%x\r\n" % len(data) + data + b"\r\n")
        else:
            self.wfile.write(data)
        self.wfile.flush()


# ---------------------------------------------------------------------------
# audio head probing
# ---------------------------------------------------------------------------

_AUDIO_MARKERS = (b"RIFF", b"RF64", b"fLaC", b"OggS", b"ID3", b"ftyp")


def _audio_duration_from_head(head: bytes) -> Optional[float]:
    """Duration of the audio carried by the *head* of an upload.

    The upload may be raw audio or wrapped in ``multipart/form-data``; either
    way the container signature is located and everything before it dropped.
    WAV and FLAC are exact from their headers even though only a prefix of the
    file is available - which is exactly why the proxy never buffers an upload.

    MP3, OGG and MP4 durations, by contrast, are derived from the *whole* file
    (frame counts, last granule position, the movie header). Measuring them from
    a truncated head under-reports the duration badly, so when the head is full
    (i.e. the upload was longer than :data:`HEAD_LIMIT`) those containers return
    ``None`` and the call is recorded as an estimate rather than as a wrong
    exact number.  Pass ``duration_hint`` if you know the real length.
    """
    if not head:
        return None
    try:
        offset = None
        for marker in _AUDIO_MARKERS:
            index = head.find(marker)
            if index < 0:
                continue
            if marker == b"ftyp":
                index = max(0, index - 4)
            if offset is None or index < offset:
                offset = index
        if offset is None:
            for index in range(len(head) - 1):
                if head[index] == 0xFF and (head[index + 1] & 0xE0) == 0xE0:
                    offset = index
                    break
        data = head[offset:] if offset is not None else head
        container = container_of(data)
        if container in ("mp3", "ogg", "mp4") and len(head) >= HEAD_LIMIT:
            # The head is full, so the upload was longer than HEAD_LIMIT and the
            # duration of these containers cannot be derived from a prefix.
            return None
        return duration_seconds(data)
    except Exception:  # pragma: no cover - defensive
        return None


# ---------------------------------------------------------------------------
# server
# ---------------------------------------------------------------------------

class ProxyServer:
    """Recording reverse proxy for ``config.proxy.upstream_base``.

    The listening socket is created on first use (``.port`` or
    ``serve_forever()``) so that ``port`` can be read before the blocking call,
    which is what tests need when configured with port ``0``.  A bind failure
    raises ``OSError`` out of whichever of those two calls comes first - never a
    silent hang (SPEC 8.11).
    """

    def __init__(self, config, ledger, logger=None):
        self.config = config
        self.ledger = ledger
        self.logger = logger
        self.upstream = urlsplit(config.proxy.upstream_base)
        self._httpd = None
        self._lock = threading.Lock()

    def _ensure_bound(self):
        with self._lock:
            if self._httpd is not None:
                return self._httpd
            httpd = ThreadingHTTPServer(
                (self.config.proxy.host, self.config.proxy.port), _ProxyHandler
            )
            httpd.daemon_threads = True
            httpd.config = self.config
            httpd.ledger = self.ledger
            httpd.logger = self.logger
            httpd.upstream = self.upstream
            self._httpd = httpd
            return httpd

    @property
    def port(self) -> int:
        """The actually bound port (useful when configured with ``0``)."""
        return int(self._ensure_bound().server_address[1])

    def serve_forever(self) -> None:
        self._ensure_bound().serve_forever(poll_interval=0.2)

    def shutdown(self) -> None:
        with self._lock:
            httpd = self._httpd
            self._httpd = None
        if httpd is None:
            return
        try:
            httpd.shutdown()
        finally:
            httpd.server_close()
