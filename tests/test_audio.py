# SPDX-License-Identifier: MIT
"""Tests for sarvam_meter.audio (SPEC section 7)."""

import math
import unittest

from sarvam_meter import audio


# ---------------------------------------------------------------------------
# synthetic file builders
# ---------------------------------------------------------------------------

def _wav(data_size=1000000, byte_rate=32000, sample_rate=16000, channels=1,
         bits=16, data_payload=b"\x00" * 64, extra_chunk=None):
    """A minimal RIFF/WAVE file whose declared data size may exceed the bytes."""
    fmt_payload = (
        (1).to_bytes(2, "little")                    # PCM
        + channels.to_bytes(2, "little")
        + sample_rate.to_bytes(4, "little")
        + byte_rate.to_bytes(4, "little")
        + (channels * bits // 8).to_bytes(2, "little")   # block align
        + bits.to_bytes(2, "little")
    )
    chunks = b"fmt " + len(fmt_payload).to_bytes(4, "little") + fmt_payload
    if extra_chunk:
        chunks += extra_chunk
    chunks += b"data" + data_size.to_bytes(4, "little") + data_payload
    return b"RIFF" + (len(chunks) + 4).to_bytes(4, "little") + b"WAVE" + chunks


def _flac(total_samples=441000, sample_rate=44100, channels=2, bits=16):
    packed = ((sample_rate << 44) | ((channels - 1) << 41) | ((bits - 1) << 36)
              | total_samples)
    streaminfo = (
        (4096).to_bytes(2, "big") + (4096).to_bytes(2, "big")
        + b"\x00" * 3 + b"\x00" * 3
        + packed.to_bytes(8, "big")
        + b"\x00" * 16
    )
    assert len(streaminfo) == 34
    return b"fLaC" + bytes([0x80 | 0]) + (34).to_bytes(3, "big") + streaminfo


def _ogg_page(granule, payload, serial=1, sequence=0, header_type=0):
    header = (
        b"OggS"
        + bytes([0, header_type])
        + (granule & 0xFFFFFFFFFFFFFFFF).to_bytes(8, "little")
        + serial.to_bytes(4, "little")
        + sequence.to_bytes(4, "little")
        + b"\x00\x00\x00\x00"                       # CRC (not validated)
        + bytes([1])
        + bytes([len(payload) & 0xFF])
    )
    return header + payload


def _ogg_opus(duration_seconds=20.0, sample_rate=48000):
    ident = b"OpusHead" + bytes([1, 2]) + b"\x00" * 9
    granules = int(duration_seconds * sample_rate)
    return (
        _ogg_page(0, ident, sequence=0, header_type=0x02)
        + _ogg_page(0, b"OpusTags" + b"\x00" * 8, sequence=1)
        + _ogg_page(granules // 2, b"\x00" * 32, sequence=2)
        + _ogg_page(granules, b"\x00" * 32, sequence=3, header_type=0x04)
    )


def _ogg_vorbis(duration_seconds=10.0, sample_rate=44100):
    ident = (
        b"\x01vorbis"
        + (0).to_bytes(4, "little")
        + bytes([2])
        + sample_rate.to_bytes(4, "little")
        + b"\x00" * 16
    )
    granules = int(duration_seconds * sample_rate)
    return (
        _ogg_page(0, ident, sequence=0, header_type=0x02)
        + _ogg_page(granules, b"\x00" * 16, sequence=1, header_type=0x04)
    )


def _mp3_frame_header(bitrate_index=9, sample_rate_index=0, channel_mode=0):
    """MPEG-1 Layer III frame header (128 kbit/s @ 44.1 kHz by default)."""
    b1 = 0xFB                                          # MPEG1, Layer III, no CRC
    b2 = ((bitrate_index & 0x0F) << 4) | ((sample_rate_index & 0x03) << 2)
    b3 = (channel_mode & 0x03) << 6
    return bytes([0xFF, b1, b2, b3])


def _mp3_xing(frames=100, sample_rate=44100):
    header = _mp3_frame_header()
    side_info = b"\x00" * 32                           # MPEG1 stereo
    xing = b"Xing" + (0x00000001).to_bytes(4, "big") + frames.to_bytes(4, "big")
    return header + side_info + xing + b"\x00" * 256


def _mp3_cbr(payload_bytes=40000):
    header = _mp3_frame_header()
    side_info = b"\x00" * 32
    return header + side_info + b"\x00" * payload_bytes


def _box(box_type, payload):
    return (len(payload) + 8).to_bytes(4, "big") + box_type + payload


def _mp4(timescale=1000, duration=5000, version=0):
    if version == 1:
        mvhd = (
            b"\x01\x00\x00\x00"
            + (0).to_bytes(8, "big") + (0).to_bytes(8, "big")
            + timescale.to_bytes(4, "big")
            + duration.to_bytes(8, "big")
            + b"\x00" * 80
        )
    else:
        mvhd = (
            b"\x00\x00\x00\x00"
            + (0).to_bytes(4, "big") + (0).to_bytes(4, "big")
            + timescale.to_bytes(4, "big")
            + duration.to_bytes(4, "big")
            + b"\x00" * 80
        )
    moov = _box(b"moov", _box(b"mvhd", mvhd) + _box(b"trak", b"\x00" * 32))
    ftyp = _box(b"ftyp", b"M4A " + (0).to_bytes(4, "big") + b"M4A mp42isom")
    return ftyp + _box(b"free", b"\x00" * 8) + moov


# ---------------------------------------------------------------------------
# container_of
# ---------------------------------------------------------------------------

class ContainerOfTests(unittest.TestCase):
    def test_wav(self):
        self.assertEqual(audio.container_of(_wav()), "wav")

    def test_flac(self):
        self.assertEqual(audio.container_of(_flac()), "flac")

    def test_ogg(self):
        self.assertEqual(audio.container_of(_ogg_opus()), "ogg")

    def test_mp3_by_sync(self):
        self.assertEqual(audio.container_of(_mp3_xing()), "mp3")

    def test_mp3_by_id3(self):
        self.assertEqual(audio.container_of(b"ID3\x03\x00\x00\x00\x00\x00\x00"), "mp3")

    def test_mp4(self):
        self.assertEqual(audio.container_of(_mp4()), "mp4")

    def test_garbage_is_none(self):
        self.assertIsNone(audio.container_of(b"hello world, not audio at all"))
        self.assertIsNone(audio.container_of(b""))
        self.assertIsNone(audio.container_of(b"\x00" * 64))


# ---------------------------------------------------------------------------
# WAV
# ---------------------------------------------------------------------------

class WavDurationTests(unittest.TestCase):
    def test_exact_from_byte_rate(self):
        value = audio.duration_seconds(_wav(data_size=1000000, byte_rate=32000))
        self.assertIsNotNone(value)
        self.assertAlmostEqual(value, 31.25, places=6)

    def test_huge_declared_data_size_is_not_read_in_full(self):
        # The buffer carries only 64 bytes of audio but the header declares
        # 1 000 000 bytes.  The declared size must be trusted, so we get the
        # declared duration and not None (nothing is scanned).
        data = _wav(data_size=1000000, byte_rate=32000, data_payload=b"\x00" * 64)
        self.assertLess(len(data), 4096)
        value = audio.duration_seconds(data)
        self.assertIsNotNone(value)
        self.assertAlmostEqual(value, 31.25, places=6)

    def test_header_only_buffer_still_works(self):
        # Exactly the header, zero bytes of audio payload.
        data = _wav(data_size=2000000, byte_rate=32000, data_payload=b"")
        self.assertAlmostEqual(audio.duration_seconds(data), 62.5, places=6)

    def test_chunk_walk_skips_other_chunks(self):
        # A LIST chunk before `data` must be skipped, not misread.
        extra = b"LIST" + (4).to_bytes(4, "little") + b"INFO"
        value = audio.duration_seconds(_wav(data_size=32000, byte_rate=16000,
                                            extra_chunk=extra))
        self.assertAlmostEqual(value, 2.0, places=6)

    def test_odd_sized_chunk_padding(self):
        extra = b"junk" + (3).to_bytes(4, "little") + b"abc\x00"
        value = audio.duration_seconds(_wav(data_size=32000, byte_rate=16000,
                                            extra_chunk=extra))
        self.assertAlmostEqual(value, 2.0, places=6)

    def test_fallback_to_block_align_when_byte_rate_missing(self):
        value = audio.duration_seconds(_wav(data_size=32000, byte_rate=0,
                                            sample_rate=16000, channels=1, bits=16))
        self.assertAlmostEqual(value, 1.0, places=6)

    def test_truncated_returns_none(self):
        full = _wav()
        self.assertIsNone(audio.duration_seconds(full[:8]))
        self.assertIsNone(audio.duration_seconds(full[:20]))
        self.assertIsNone(audio.duration_seconds(b"RIFF" + (0).to_bytes(4, "little")
                                                 + b"WAVE"))

    def test_no_data_chunk_returns_none(self):
        fmt = b"fmt " + (16).to_bytes(4, "little") + b"\x00" * 16
        body = b"WAVE" + fmt
        self.assertIsNone(audio.duration_seconds(b"RIFF" + (len(body)).to_bytes(4, "little")
                                                 + body))


# ---------------------------------------------------------------------------
# FLAC
# ---------------------------------------------------------------------------

class FlacDurationTests(unittest.TestCase):
    def test_exact_from_streaminfo(self):
        value = audio.duration_seconds(_flac(total_samples=441000, sample_rate=44100))
        self.assertAlmostEqual(value, 10.0, places=6)

    def test_other_sample_rate(self):
        value = audio.duration_seconds(_flac(total_samples=96000, sample_rate=48000))
        self.assertAlmostEqual(value, 2.0, places=6)

    def test_unknown_total_samples_returns_none(self):
        self.assertIsNone(audio.duration_seconds(_flac(total_samples=0)))

    def test_truncated_streaminfo_returns_none(self):
        self.assertIsNone(audio.duration_seconds(_flac()[:20]))
        self.assertIsNone(audio.duration_seconds(b"fLaC"))

    def test_tiny_buffer_is_not_scanned(self):
        data = _flac(total_samples=44100 * 3600, sample_rate=44100)
        self.assertAlmostEqual(audio.duration_seconds(data), 3600.0, places=6)


# ---------------------------------------------------------------------------
# OGG
# ---------------------------------------------------------------------------

class OggDurationTests(unittest.TestCase):
    def test_opus_uses_48k_and_last_granule(self):
        value = audio.duration_seconds(_ogg_opus(duration_seconds=20.0))
        self.assertAlmostEqual(value, 20.0, places=6)

    def test_vorbis_reads_header_sample_rate(self):
        value = audio.duration_seconds(_ogg_vorbis(duration_seconds=10.0,
                                                   sample_rate=44100))
        self.assertAlmostEqual(value, 10.0, places=6)

    def test_last_granule_wins(self):
        pages = _ogg_opus(duration_seconds=20.0)
        self.assertAlmostEqual(audio.duration_seconds(pages), 20.0, places=6)

    def test_truncated_header_returns_none(self):
        self.assertIsNone(audio.duration_seconds(b"OggS" + b"\x00" * 10))

    def test_no_codec_returns_none(self):
        pages = _ogg_page(1000, b"unknown-codec") + _ogg_page(5000, b"x")
        self.assertIsNone(audio.duration_seconds(pages))


# ---------------------------------------------------------------------------
# MP3
# ---------------------------------------------------------------------------

class Mp3DurationTests(unittest.TestCase):
    def test_xing_frame_count(self):
        data = _mp3_xing(frames=100, sample_rate=44100)
        expected = 100 * 1152 / 44100.0
        self.assertAlmostEqual(audio.duration_seconds(data), expected, places=6)

    def test_info_header_is_accepted(self):
        header = _mp3_frame_header()
        side_info = b"\x00" * 32
        info = b"Info" + (0x00000001).to_bytes(4, "big") + (50).to_bytes(4, "big")
        data = header + side_info + info + b"\x00" * 64
        self.assertAlmostEqual(audio.duration_seconds(data),
                               50 * 1152 / 44100.0, places=6)

    def test_cbr_fallback(self):
        data = _mp3_cbr(payload_bytes=40000)
        expected = len(data) * 8 / 128000.0
        self.assertAlmostEqual(audio.duration_seconds(data), expected, places=6)

    def test_id3v2_tag_is_skipped(self):
        tag_body = b"\x00" * 100
        size = len(tag_body)
        synchsafe = bytes([(size >> 21) & 0x7F, (size >> 14) & 0x7F,
                           (size >> 7) & 0x7F, size & 0x7F])
        data = b"ID3\x03\x00\x00" + synchsafe + tag_body + _mp3_xing(frames=10)
        self.assertAlmostEqual(audio.duration_seconds(data), 10 * 1152 / 44100.0,
                               places=6)

    def test_id3v1_trailer_is_excluded(self):
        body = _mp3_cbr(payload_bytes=40000)
        data = body + b"TAG" + b"\x00" * 125
        expected = len(body) * 8 / 128000.0
        self.assertAlmostEqual(audio.duration_seconds(data), expected, places=6)

    def test_no_frame_returns_none(self):
        self.assertIsNone(audio.duration_seconds(b"ID3\x03\x00\x00\x00\x00\x00\x00"))


# ---------------------------------------------------------------------------
# MP4
# ---------------------------------------------------------------------------

class Mp4DurationTests(unittest.TestCase):
    def test_mvhd_version0(self):
        self.assertAlmostEqual(audio.duration_seconds(_mp4(1000, 5000)), 5.0, places=6)

    def test_mvhd_version1(self):
        self.assertAlmostEqual(audio.duration_seconds(_mp4(1000, 12345, version=1)),
                               12.345, places=6)

    def test_unknown_duration_returns_none(self):
        data = _mp4(1000, 0xFFFFFFFF)
        self.assertIsNone(audio.duration_seconds(data))

    def test_no_moov_returns_none(self):
        self.assertIsNone(audio.duration_seconds(_box(b"ftyp", b"isom" + b"\x00" * 8)))

    def test_truncated_returns_none(self):
        self.assertIsNone(audio.duration_seconds(_mp4()[:12]))


# ---------------------------------------------------------------------------
# robustness / contract
# ---------------------------------------------------------------------------

class ContractTests(unittest.TestCase):
    def test_never_raises_on_garbage(self):
        payloads = [
            b"", b"\x00", b"RIFF", b"fLaC\x80\x00\x00\x22",
            b"OggS\x00" + b"\xff" * 40,
            b"ID3" + b"\xff" * 20,
            _wav()[:13], _flac()[:9], _mp4()[:11],
            bytes(range(256)) * 4,
        ]
        for payload in payloads:
            value = audio.duration_seconds(payload)
            if value is not None:
                self.assertIsInstance(value, float)
                self.assertTrue(math.isfinite(value))
                self.assertGreaterEqual(value, 0.0)

    def test_all_containers_return_finite_floats(self):
        for payload in (_wav(), _flac(), _ogg_opus(), _ogg_vorbis(), _mp3_xing(),
                        _mp3_cbr(), _mp4()):
            value = audio.duration_seconds(payload)
            self.assertIsNotNone(value)
            self.assertTrue(math.isfinite(value))
            self.assertGreater(value, 0.0)

    def test_content_type_hint_selects_parser(self):
        data = _flac(total_samples=44100, sample_rate=44100)
        self.assertAlmostEqual(audio.duration_seconds(data, "audio/flac"), 1.0,
                               places=6)
        self.assertAlmostEqual(audio.duration_seconds(data, "audio/flac; charset=binary"),
                               1.0, places=6)

    def test_content_type_hint_cannot_invent_duration(self):
        self.assertIsNone(audio.duration_seconds(b"not audio", "audio/wav"))
        self.assertIsNone(audio.duration_seconds(b"not audio", None))

    def test_returns_none_for_none_like_input(self):
        self.assertIsNone(audio.duration_seconds(b""))
        self.assertIsNone(audio.duration_seconds(bytearray()))


if __name__ == "__main__":
    unittest.main()
