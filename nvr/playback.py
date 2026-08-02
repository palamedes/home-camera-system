"""Playback of recorded footage.

The browser asks for a window of time, not a file. We find the segments
covering that window, stitch them with ffmpeg's concat demuxer, and stream the
result as fragmented MP4 — which starts playing immediately instead of waiting
for a whole file to be assembled.

Codec handling matters on this hardware. Segments are stored exactly as the
camera sent them, which is often H.265: great for disk, poorly supported by
browsers. When a window needs converting we decode and encode on the iGPU via
QuickSync, which keeps a 6 MP stream well inside real time. H.264 segments skip
all of that and are remuxed, which costs almost nothing.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger("nvr.playback")

# Segments recorded back-to-back land a fraction of a second apart; anything
# under this is treated as continuous rather than as a gap in coverage.
GAP_TOLERANCE = 2.0

BROWSER_SAFE_CODECS = {"h264", "avc1"}


@dataclass
class Range:
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start

    def to_dict(self) -> dict[str, float]:
        return {"start": self.start, "end": self.end, "duration": self.duration}


def coverage(db: Any, camera_id: str, start: float, end: float) -> list[Range]:
    """Contiguous stretches of recorded footage within a window.

    Drives the timeline: solid where there is footage, empty where the camera
    was offline.
    """
    rows = db.segments_in_range(camera_id, start, end)
    ranges: list[Range] = []
    for row in rows:
        seg_start = float(row["start_ts"])
        seg_end = seg_start + float(row["duration"])
        if ranges and seg_start - ranges[-1].end <= GAP_TOLERANCE:
            ranges[-1].end = max(ranges[-1].end, seg_end)
        else:
            ranges.append(Range(seg_start, seg_end))

    # Trim to the requested window so the UI does not draw past its own edges.
    clipped = []
    for span in ranges:
        lo, hi = max(span.start, start), min(span.end, end)
        if hi > lo:
            clipped.append(Range(lo, hi))
    return clipped


def _concat_file(rows: list[Any], directory: Path) -> Path:
    """Write an ffmpeg concat list.

    Paths are single-quoted with embedded quotes escaped, per the concat
    demuxer's format.
    """
    lines = []
    for row in rows:
        path = str(row["path"]).replace("'", r"'\''")
        lines.append(f"file '{path}'")
    listing = directory / "concat.txt"
    listing.write_text("\n".join(lines) + "\n")
    return listing


def _needs_transcode(rows: list[Any], config: Any) -> bool:
    if config.playback.always_transcode:
        return True
    codecs = {(row["codec"] or "").lower() for row in rows}
    return not codecs.issubset(BROWSER_SAFE_CODECS)


def _video_args(transcode: bool, config: Any) -> tuple[list[str], list[str]]:
    """Returns (input_args, output_args) for the video path."""
    if not transcode:
        return [], ["-c:v", "copy"]

    if config.playback.hardware_available():
        return (
            [
                "-init_hw_device", f"qsv=hw,child_device={config.playback.qsv_device}",
                "-filter_hw_device", "hw",
                "-hwaccel", "qsv",
                "-hwaccel_output_format", "qsv",
            ],
            [
                "-vf", "hwupload=extra_hw_frames=64,scale_qsv=format=nv12",
                "-c:v", "h264_qsv",
                "-preset", "veryfast",
                "-global_quality", "26",
            ],
        )

    log.warning("QuickSync unavailable; falling back to software encoding")
    return [], ["-c:v", "libx264", "-preset", "veryfast", "-crf", "26"]


def stream_window(
    db: Any, config: Any, camera_id: str, start: float, duration: float,
) -> Iterator[bytes]:
    """Yield fragmented MP4 for [start, start+duration).

    Raises FileNotFoundError when nothing is recorded for that window.
    """
    end = start + duration
    rows = db.segments_in_range(camera_id, start, end)
    if not rows:
        raise FileNotFoundError(f"no footage for {camera_id} at {start}")

    # Seek offset into the first segment, since it usually starts before the
    # requested instant.
    offset = max(0.0, start - float(rows[0]["start_ts"]))
    transcode = _needs_transcode(rows, config)
    input_args, output_args = _video_args(transcode, config)

    workdir = Path(tempfile.mkdtemp(prefix="nvr-playback-"))
    try:
        listing = _concat_file(rows, workdir)
        command = [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
            *input_args,
            "-f", "concat", "-safe", "0",
            "-protocol_whitelist", "file",
            "-ss", f"{offset:.3f}",
            "-i", str(listing),
            "-t", f"{duration:.3f}",
            *output_args,
            "-c:a", "aac", "-b:a", "64k",
            # empty_moov lets playback begin before the stream is finished;
            # without it the browser waits for a moov atom that only arrives
            # at the end.
            "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
            "-f", "mp4", "pipe:1",
        ]
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        try:
            assert process.stdout is not None
            while True:
                chunk = process.stdout.read(65536)
                if not chunk:
                    break
                yield chunk
        finally:
            # The client disconnecting mid-stream is normal (they scrubbed
            # elsewhere); make sure ffmpeg does not linger.
            if process.poll() is None:
                process.kill()
            stderr = b""
            if process.stderr:
                try:
                    stderr = process.stderr.read() or b""
                except Exception:
                    pass
            process.wait(timeout=5)
            if stderr.strip():
                log.debug("playback ffmpeg: %s", stderr.decode(errors="replace").strip())
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def export_clip(
    db: Any, config: Any, camera_id: str, start: float, duration: float,
) -> Path:
    """Render a downloadable MP4 to a temp file.

    Unlike streaming this writes a normal (non-fragmented) MP4 with the index
    moved to the front, so it plays in anything and seeks properly once saved.
    Caller owns the returned file.
    """
    end = start + duration
    rows = db.segments_in_range(camera_id, start, end)
    if not rows:
        raise FileNotFoundError(f"no footage for {camera_id} at {start}")

    offset = max(0.0, start - float(rows[0]["start_ts"]))
    transcode = _needs_transcode(rows, config)
    input_args, output_args = _video_args(transcode, config)

    workdir = Path(tempfile.mkdtemp(prefix="nvr-export-"))
    listing = _concat_file(rows, workdir)
    output = workdir / f"{camera_id}-{int(start)}.mp4"

    command = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        *input_args,
        "-f", "concat", "-safe", "0",
        "-protocol_whitelist", "file",
        "-ss", f"{offset:.3f}",
        "-i", str(listing),
        "-t", f"{duration:.3f}",
        *output_args,
        "-c:a", "aac", "-b:a", "64k",
        "-movflags", "+faststart",
        str(output),
    ]
    result = subprocess.run(command, capture_output=True, timeout=600)
    if result.returncode != 0 or not output.exists():
        shutil.rmtree(workdir, ignore_errors=True)
        detail = result.stderr.decode(errors="replace").strip().splitlines()
        raise RuntimeError(detail[-1] if detail else "export failed")
    return output
