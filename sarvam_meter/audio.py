# SPDX-License-Identifier: MIT
"""Audio duration probing (SPEC section 7).

Sarvam's speech-to-text response carries no duration, so the recording proxy
derives it from the uploaded bytes.  Every public function here is total: it
never raises and never returns NaN/inf, only ``None`` when the input cannot be
understood.

Design notes
------------
* WAV and FLAC are read from headers only.  The declared ``data`` chunk size
  (WAV) or ``total_samples`` field (FLAC) is trusted, so a 200 MB file is never
  scanned - the caller may even pass a truncated buffer that holds nothing but
  the first few hundred bytes and still get the exact duration.
* OGG uses the granule position of the last valid page.
* MP3 uses a Xing/Info (or VBRI) frame count when present, else a CBR estimate
  from the audio bitrate.
* MP4/M4A uses ``mvhd`` timescale and duration.
"""

from __future__ import annotations

import math
from typing import Optional

__all__ = ["duration_seconds", "container_of"]


# ---------------------------------------------------------------------------
# container sniffing
# ---------------------------------------------------------------------------

def container_of(data: bytes) -> Optional[str]:
    """Return ``'wav'|'flac'|'ogg'|'mp3'|'mp4'`` or ``None``.

    Detection is by magic bytes only; no header parsing happens here.
    """
    try:
        if not data:
            return None
        head = bytes(data[:16])
        if len(head) >= 12 and head[:4] in (b"RIFF", b"RF64") and head[8:12] == b"WAVE":
            return "wav"
        if head[:4] == b"fLaC":
            return "flac"
        if head[:4] == b"OggS":
            return "ogg"
        if len(head) >= 8 and head[4:8] == b"ftyp":
            return "mp4"
        if head[:3] == b"ID3":
            return "mp3"
        if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
            return "mp3"
        return None
    except Exception:  # pragma: no cover - defensive
        return None


def _container_from_content_type(content_type: Optional[str]) -> Optional[str]:
    if not content_type:
        return None
    try:
        base = str(content_type).split(";", 1)[0].strip().lower()
    except Exception:  # pragma: no cover - defensive
        return None
    mapping = {
        "audio/wav": "wav",
        "audio/wave": "wav",
        "audio/x-wav": "wav",
        "audio/vnd.wave": "wav",
        "audio/flac": "flac",
        "audio/x-flac": "flac",
        "audio/ogg": "ogg",
        "application/ogg": "ogg",
        "audio/opus": "ogg",
        "audio/mpeg": "mp3",
        "audio/mp3": "mp3",
        "audio/mpeg3": "mp3",
        "audio/x-mpeg-3": "mp3",
        "audio/mp4": "mp4",
        "audio/m4a": "mp4",
        "audio/x-m4a": "mp4",
        "video/mp4": "mp4",
        "application/mp4": "mp4",
    }
    return mapping.get(base)


# ---------------------------------------------------------------------------
# WAV (RIFF/WAVE)
# ---------------------------------------------------------------------------

def _wav_duration(data: bytes) -> Optional[float]:
    n = len(data)
    if n < 12:
        return None
    if data[:4] not in (b"RIFF", b"RF64") or data[8:12] != b"WAVE":
        return None

    pos = 12
    byte_rate = 0
    block_align = 0
    sample_rate = 0
    channels = 0
    bits = 0
    data_size = None

    # Walk chunk *headers* only.  Payloads are skipped by their declared size,
    # so the scan never touches the audio itself.
    while pos + 8 <= n:
        chunk_id = data[pos:pos + 4]
        chunk_size = int.from_bytes(data[pos + 4:pos + 8], "little")
        body = pos + 8
        if chunk_id == b"fmt ":
            if body + 16 > n:
                return None
            channels = int.from_bytes(data[body + 2:body + 4], "little")
            sample_rate = int.from_bytes(data[body + 4:body + 8], "little")
            byte_rate = int.from_bytes(data[body + 8:body + 12], "little")
            block_align = int.from_bytes(data[body + 12:body + 14], "little")
            bits = int.from_bytes(data[body + 14:body + 16], "little")
        elif chunk_id == b"data":
            # The declared size is authoritative - do not read the payload.
            data_size = chunk_size
            break
        # chunks are word aligned
        pos = body + chunk_size + (chunk_size & 1)

    if data_size is None:
        return None

    rate = byte_rate
    if rate <= 0 and block_align > 0 and sample_rate > 0:
        rate = block_align * sample_rate
    if rate <= 0 and channels > 0 and bits > 0 and sample_rate > 0:
        rate = channels * (bits // 8) * sample_rate
    if rate <= 0:
        return None
    return float(data_size) / float(rate)


# ---------------------------------------------------------------------------
# FLAC
# ---------------------------------------------------------------------------

def _flac_duration(data: bytes) -> Optional[float]:
    n = len(data)
    if n < 8 or data[:4] != b"fLaC":
        return None
    pos = 4
    while pos + 4 <= n:
        header = data[pos]
        is_last = bool(header & 0x80)
        block_type = header & 0x7F
        length = int.from_bytes(data[pos + 1:pos + 4], "big")
        body = pos + 4
        if block_type == 0:  # STREAMINFO
            if body + 34 > n:
                return None
            packed = data[body + 10:body + 18]
            sample_rate = (packed[0] << 12) | (packed[1] << 4) | (packed[2] >> 4)
            total_samples = (
                ((packed[3] & 0x0F) << 32)
                | (packed[4] << 24)
                | (packed[5] << 16)
                | (packed[6] << 8)
                | packed[7]
            )
            if sample_rate <= 0 or total_samples <= 0:
                return None
            return float(total_samples) / float(sample_rate)
        if is_last:
            break
        pos = body + length
    return None


# ---------------------------------------------------------------------------
# OGG (Opus / Vorbis)
# ---------------------------------------------------------------------------

def _ogg_sample_rate(data: bytes) -> Optional[int]:
    try:
        if data.find(b"OpusHead") >= 0:
            return 48000
        idx = data.find(b"\x01vorbis")
        if idx >= 0 and idx + 16 <= len(data):
            rate = int.from_bytes(data[idx + 12:idx + 16], "little")
            if rate > 0:
                return rate
        return None
    except Exception:  # pragma: no cover - defensive
        return None


def _ogg_duration(data: bytes) -> Optional[float]:
    n = len(data)
    if n < 27 or data[:4] != b"OggS":
        return None
    sample_rate = _ogg_sample_rate(data)
    if not sample_rate:
        return None
    idx = data.rfind(b"OggS")
    while idx >= 0:
        # A page header is 27 bytes plus a segment table; validate minimally.
        if idx + 27 <= n and data[idx + 4] == 0:
            granule = int.from_bytes(data[idx + 6:idx + 14], "little")
            if granule != 0xFFFFFFFFFFFFFFFF:
                return float(granule) / float(sample_rate)
        idx = data.rfind(b"OggS", 0, idx)
    return None


# ---------------------------------------------------------------------------
# MP3
# ---------------------------------------------------------------------------

_MP3_BITRATES = {
    # (version_group, layer) -> kbit/s table indexed by bitrate_index
    ("mpeg1", 1): (0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448),
    ("mpeg1", 2): (0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384),
    ("mpeg1", 3): (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
    ("mpeg2", 1): (0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256),
    ("mpeg2", 2): (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
    ("mpeg2", 3): (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
}

_MP3_SAMPLE_RATES = {
    3: (44100, 48000, 32000),   # MPEG 1
    2: (22050, 24000, 16000),   # MPEG 2
    0: (11025, 12000, 8000),    # MPEG 2.5
}


def _synchsafe(raw: bytes) -> int:
    value = 0
    for byte in raw[:4]:
        value = (value << 7) | (byte & 0x7F)
    return value


def _mp3_frame_header(data: bytes, pos: int) -> Optional[dict]:
    if pos + 4 > len(data):
        return None
    if data[pos] != 0xFF or (data[pos + 1] & 0xE0) != 0xE0:
        return None
    b1 = data[pos + 1]
    b2 = data[pos + 2]
    b3 = data[pos + 3]
    version = (b1 >> 3) & 0x03
    layer_bits = (b1 >> 1) & 0x03
    if version == 1 or layer_bits == 0:  # reserved values
        return None
    # layer bits: 01 = Layer III, 10 = Layer II, 11 = Layer I
    layer = 4 - layer_bits
    bitrate_index = (b2 >> 4) & 0x0F
    sample_rate_index = (b2 >> 2) & 0x03
    if bitrate_index in (0, 15) or sample_rate_index == 3:
        return None
    group = "mpeg1" if version == 3 else "mpeg2"
    table = _MP3_BITRATES.get((group, layer))
    if table is None:
        return None
    bitrate = table[bitrate_index]
    if bitrate <= 0:
        return None
    sample_rate = _MP3_SAMPLE_RATES[version][sample_rate_index]
    channel_mode = (b3 >> 6) & 0x03
    if layer == 3:
        samples_per_frame = 1152 if version == 3 else 576
    elif layer == 2:
        samples_per_frame = 1152
    else:
        samples_per_frame = 384
    if version == 3:
        side_info = 17 if channel_mode == 3 else 32
    else:
        side_info = 9 if channel_mode == 3 else 17
    return {
        "version": version,
        "layer": layer,
        "bitrate": bitrate,
        "sample_rate": sample_rate,
        "samples_per_frame": samples_per_frame,
        "side_info": side_info,
        "channel_mode": channel_mode,
    }


def _mp3_duration(data: bytes) -> Optional[float]:
    n = len(data)
    if n < 4:
        return None

    start = 0
    if data[:3] == b"ID3" and n >= 10:
        size = _synchsafe(data[6:10])
        start = 10 + size
        if data[5] & 0x10:  # footer present
            start += 10
    end = n
    if n >= 128 and data[-128:-125] == b"TAG":  # ID3v1 trailer
        end -= 128
    if start >= end:
        return None

    # Find the first frame header, searching a bounded window only.
    limit = min(end, start + 65536)
    pos = start
    header = None
    while pos + 4 <= limit:
        header = _mp3_frame_header(data, pos)
        if header is not None:
            break
        pos += 1
    if header is None:
        return None

    sample_rate = header["sample_rate"]
    samples_per_frame = header["samples_per_frame"]

    # Xing / Info (in the first frame, after the side info block).
    xing = pos + 4 + header["side_info"]
    if data[xing:xing + 4] in (b"Xing", b"Info") and xing + 8 <= n:
        flags = int.from_bytes(data[xing + 4:xing + 8], "big")
        cursor = xing + 8
        if flags & 0x0001 and cursor + 4 <= n:
            frames = int.from_bytes(data[cursor:cursor + 4], "big")
            if frames > 0:
                return float(frames) * float(samples_per_frame) / float(sample_rate)

    # VBRI (Fraunhofer) header at a fixed offset after the frame header.
    vbri = pos + 36
    if data[vbri:vbri + 4] == b"VBRI" and vbri + 18 <= n:
        frames = int.from_bytes(data[vbri + 14:vbri + 18], "big")
        if frames > 0:
            return float(frames) * float(samples_per_frame) / float(sample_rate)

    # CBR fallback: bytes * 8 / bitrate.
    audio_bytes = end - pos
    if audio_bytes <= 0:
        return None
    return float(audio_bytes) * 8.0 / (float(header["bitrate"]) * 1000.0)


# ---------------------------------------------------------------------------
# MP4 / M4A
# ---------------------------------------------------------------------------

def _mp4_box(buf: bytes, pos: int):
    """Return (type, header_len, size) for the box at ``pos`` or ``None``."""
    n = len(buf)
    if pos + 8 > n:
        return None
    size = int.from_bytes(buf[pos:pos + 4], "big")
    box_type = buf[pos + 4:pos + 8]
    header_len = 8
    if size == 1:
        if pos + 16 > n:
            return None
        size = int.from_bytes(buf[pos + 8:pos + 16], "big")
        header_len = 16
    elif size == 0:
        size = n - pos
    if size < header_len:
        return None
    return box_type, header_len, size


def _mp4_mvhd(buf: bytes) -> Optional[float]:
    n = len(buf)
    pos = 0
    while pos + 8 <= n:
        box = _mp4_box(buf, pos)
        if box is None:
            return None
        box_type, header_len, size = box
        if box_type == b"mvhd":
            body = buf[pos + header_len:pos + size]
            if len(body) < 20:
                return None
            version = body[0]
            if version == 1:
                if len(body) < 32:
                    return None
                timescale = int.from_bytes(body[20:24], "big")
                duration = int.from_bytes(body[24:32], "big")
                unknown = 0xFFFFFFFFFFFFFFFF
            else:
                timescale = int.from_bytes(body[12:16], "big")
                duration = int.from_bytes(body[16:20], "big")
                unknown = 0xFFFFFFFF
            if timescale <= 0 or duration == unknown:
                return None
            return float(duration) / float(timescale)
        pos += size
    return None


def _mp4_duration(data: bytes) -> Optional[float]:
    n = len(data)
    pos = 0
    while pos + 8 <= n:
        box = _mp4_box(data, pos)
        if box is None:
            return None
        box_type, header_len, size = box
        if box_type == b"moov":
            value = _mp4_mvhd(data[pos + header_len:pos + size])
            if value is not None:
                return value
        pos += size
    return None


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------

_PARSERS = {
    "wav": _wav_duration,
    "flac": _flac_duration,
    "ogg": _ogg_duration,
    "mp3": _mp3_duration,
    "mp4": _mp4_duration,
}


def duration_seconds(data: bytes, content_type: Optional[str] = None) -> Optional[float]:
    """Best-effort duration of ``data`` in seconds, or ``None``.

    ``content_type`` is only used as a *hint* for which parser to try when the
    magic bytes are inconclusive; it never changes the result of a successful
    signature match.  Never raises; never returns NaN or infinity.
    """
    try:
        if not data:
            return None
        candidates = []
        detected = container_of(data)
        if detected:
            candidates.append(detected)
        hinted = _container_from_content_type(content_type)
        if hinted and hinted not in candidates:
            candidates.append(hinted)

        for name in candidates:
            parser = _PARSERS.get(name)
            if parser is None:
                continue
            try:
                value = parser(data)
            except Exception:
                value = None
            if value is None:
                continue
            try:
                result = float(value)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(result) or result < 0.0:
                continue
            return result
        return None
    except Exception:  # pragma: no cover - defensive
        return None
