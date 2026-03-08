#!/usr/bin/env python3
"""Analyze AAC frame timing to debug playback pacing issues.

Converts an audio file the same way the HA integration does and reports
frame count, duration, and calculated pacing interval.

Usage: python3 analyze_aac_timing.py <audio_file>
Example: python3 analyze_aac_timing.py /tmp/Hallelujah-sound-effect.mp3
"""

import re
import subprocess
import sys


def parse_adts_frames(data: bytes) -> list[bytes]:
    """Parse ADTS frames from raw AAC data."""
    frames = []
    i = 0
    while i < len(data) - 7:
        if data[i] != 0xFF or (data[i + 1] & 0xF0) != 0xF0:
            i += 1
            continue
        frame_len = (
            ((data[i + 3] & 0x03) << 11) | (data[i + 4] << 3) | (data[i + 5] >> 5)
        )
        if frame_len < 7 or i + frame_len > len(data):
            break
        frames.append(data[i : i + frame_len])
        i += frame_len
    return frames


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    input_file = sys.argv[1]
    with open(input_file, "rb") as f:
        audio_data = f.read()

    print(f"Input: {input_file} ({len(audio_data)} bytes)")

    # Convert exactly like the HA integration does (pipe:0 -> pipe:1)
    result = subprocess.run(
        [
            "ffmpeg",
            "-i",
            "pipe:0",
            "-c:a",
            "aac",
            "-b:a",
            "64k",
            "-ar",
            "8000",
            "-ac",
            "1",
            "-f",
            "adts",
            "pipe:1",
        ],
        input=audio_data,
        capture_output=True,
    )

    aac_data = result.stdout
    print(f"AAC output: {len(aac_data)} bytes")

    # Parse duration like the integration does
    duration = 0.0
    for line in result.stderr.decode(errors="replace").splitlines():
        if "Duration:" in line:
            match = re.search(r"Duration:\s*(\d+):(\d+):(\d+)\.(\d+)", line)
            if match:
                h, m, s, cs = (int(g) for g in match.groups())
                duration = h * 3600 + m * 60 + s + cs / 100.0
                break
    print(f"Duration from ffmpeg stderr: {duration}s")

    frames = parse_adts_frames(aac_data)
    print(f"ADTS frames parsed: {len(frames)}")

    if not frames:
        print("ERROR: No ADTS frames found!")
        sys.exit(1)

    # What the integration calculates
    frame_interval = duration / len(frames) if len(frames) > 1 else duration
    print(f"Integration's frame_interval: {frame_interval * 1000:.1f}ms")

    # What it SHOULD be (AAC at 8kHz = 1024 samples/frame = 128ms)
    correct_interval = 1024.0 / 8000.0
    correct_duration = len(frames) * correct_interval
    print()
    print(f"Correct interval (1024/8000): {correct_interval * 1000:.1f}ms")
    print(f"Correct duration from frames: {correct_duration:.2f}s")

    # Fallback duration (bytes/8000) - also wrong for compressed AAC
    fallback_duration = len(aac_data) / 8000.0
    fallback_interval = fallback_duration / len(frames)
    print(f"Fallback duration (bytes/8000): {fallback_duration:.2f}s")
    print(f"Fallback interval: {fallback_interval * 1000:.1f}ms")

    if frame_interval < 0.001:
        print()
        print("BUG: frame_interval is ~0! All frames sent instantly (no pacing).")
        print("Fix: use 1024/sample_rate (128ms for 8kHz AAC) as frame interval.")


if __name__ == "__main__":
    main()
