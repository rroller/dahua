#!/usr/bin/env python3
"""Test RTSP backchannel audio playback to a Dahua camera.

Usage: python3 rtsp_backchannel_test.py <camera_host> <aac_file>
Example: python3 rtsp_backchannel_test.py 192.168.253.11 /tmp/hallelujah-aac.raw

Env vars: CAMERA_USER (default: admin), CAMERA_PASS (required)
"""

import asyncio
import os
import random
import re
import struct
import sys
import time
from hashlib import md5


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


async def test_backchannel(host: str, aac_file: str):
    user = os.environ.get("CAMERA_USER", "admin")
    password = os.environ.get("CAMERA_PASS")
    if not password:
        print("ERROR: CAMERA_PASS env var required")
        sys.exit(1)

    with open(aac_file, "rb") as f:
        aac_data = f.read()

    frames = parse_adts_frames(aac_data)
    print(f"Parsed {len(frames)} ADTS frames from {len(aac_data)} bytes")
    if not frames:
        print("ERROR: No ADTS frames found")
        sys.exit(1)

    rtsp_port = 554
    channel = 1
    rtsp_url = f"rtsp://{host}:{rtsp_port}/cam/realmonitor?channel={channel}&subtype=0"
    uri_path = f"/cam/realmonitor?channel={channel}&subtype=0"

    print(f"Connecting to {host}:{rtsp_port}...")
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, rtsp_port), timeout=10
    )
    print("Connected.")

    cseq = 0
    session_id = ""

    async def rtsp_send(method: str, url: str, extra_headers: str = "") -> str:
        nonlocal cseq
        cseq += 1
        msg = f"{method} {url} RTSP/1.0\r\nCSeq: {cseq}\r\n{extra_headers}"
        if not msg.endswith("\r\n\r\n"):
            msg += "\r\n"
        print(f"\n>>> {method} (CSeq {cseq})")
        writer.write(msg.encode())
        await writer.drain()

        resp_bytes = b""
        while b"\r\n\r\n" not in resp_bytes:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=10)
            if not chunk:
                break
            resp_bytes += chunk
        resp = resp_bytes.decode(errors="replace")

        cl = re.search(r"Content-Length:\s*(\d+)", resp, re.IGNORECASE)
        if cl:
            body_start = resp_bytes.find(b"\r\n\r\n") + 4
            body_needed = int(cl.group(1)) - (len(resp_bytes) - body_start)
            if body_needed > 0:
                body_extra = await asyncio.wait_for(
                    reader.readexactly(body_needed), timeout=10
                )
                resp += body_extra.decode(errors="replace")

        status_line = resp.split("\r\n")[0]
        print(f"<<< {status_line}")
        return resp

    try:
        # DESCRIBE (unauthenticated to get digest challenge)
        resp = await rtsp_send(
            "DESCRIBE",
            rtsp_url,
            "Accept: application/sdp\r\nRequire: www.onvif.org/ver20/backchannel\r\n",
        )

        realm_m = re.search(r'realm="([^"]+)"', resp)
        nonce_m = re.search(r'nonce="([^"]+)"', resp)
        if not (realm_m and nonce_m):
            print(f"ERROR: No digest challenge found.\nResponse:\n{resp[:500]}")
            return
        realm = realm_m.group(1)
        nonce = nonce_m.group(1)
        print(f"    Got digest challenge (realm={realm})")

        def digest_auth(method: str, uri: str) -> str:
            ha1 = md5(f"{user}:{realm}:{password}".encode()).hexdigest()
            ha2 = md5(f"{method}:{uri}".encode()).hexdigest()
            resp_hash = md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
            return (
                f'Digest username="{user}", realm="{realm}", nonce="{nonce}", '
                f'uri="{uri}", response="{resp_hash}"'
            )

        # DESCRIBE (authenticated)
        resp = await rtsp_send(
            "DESCRIBE",
            rtsp_url,
            f"Accept: application/sdp\r\n"
            f"Require: www.onvif.org/ver20/backchannel\r\n"
            f"Authorization: {digest_auth('DESCRIBE', uri_path)}\r\n",
        )
        if "200" not in resp.split("\r\n")[0]:
            print(f"ERROR: DESCRIBE failed.\nResponse:\n{resp[:500]}")
            return

        # Parse SDP for backchannel track
        bc_track = None
        current_track = None
        print("\n--- SDP ---")
        for line in resp.split("\n"):
            line = line.strip()
            if line.startswith("m=") or line.startswith("a="):
                print(f"  {line}")
            if line.startswith("a=control:trackID="):
                current_track = line.split("=", 1)[1]
            if line == "a=sendonly":
                bc_track = current_track

        if not bc_track:
            print("ERROR: No backchannel (sendonly) track found in SDP")
            return
        print(f"\nBackchannel track: {bc_track}")

        # SETUP backchannel track
        setup_url = f"{rtsp_url}/trackID={bc_track}"
        setup_uri = f"{uri_path}/trackID={bc_track}"
        resp = await rtsp_send(
            "SETUP",
            setup_url,
            f"Transport: RTP/AVP/TCP;unicast;interleaved=0-1\r\n"
            f"Authorization: {digest_auth('SETUP', setup_uri)}\r\n",
        )
        if "200" not in resp.split("\r\n")[0]:
            print(f"ERROR: SETUP failed.\nResponse:\n{resp[:500]}")
            return

        sess_m = re.search(r"Session:\s*([^;\r\n]+)", resp)
        if sess_m:
            session_id = sess_m.group(1).strip()
            print(f"    Session: {session_id}")

        il_m = re.search(r"interleaved=(\d+)-(\d+)", resp)
        rtp_channel = int(il_m.group(1)) if il_m else 0
        print(f"    RTP channel: {rtp_channel}")

        # PLAY
        play_headers = f"Authorization: {digest_auth('PLAY', uri_path)}\r\n"
        if session_id:
            play_headers += f"Session: {session_id}\r\n"
        resp = await rtsp_send("PLAY", rtsp_url, play_headers)
        if "200" not in resp.split("\r\n")[0]:
            print(f"ERROR: PLAY failed.\nResponse:\n{resp[:500]}")
            return

        # Stream audio as RTP
        seq = random.randint(0, 0xFFFF)
        ts = random.randint(0, 0xFFFFFFFF)
        ssrc = random.randint(0, 0xFFFFFFFF)
        payload_type = 97

        duration = 9.45  # known duration of the file
        frame_interval = duration / len(frames) if len(frames) > 1 else duration

        print(
            f"\nStreaming {len(frames)} frames over {duration:.1f}s (interval {frame_interval * 1000:.1f}ms)..."
        )
        start = time.monotonic()
        for i, adts_frame in enumerate(frames):
            header_len = 7
            if len(adts_frame) > 1 and not (adts_frame[1] & 0x01):
                header_len = 9
            raw_aac = adts_frame[header_len:]
            if not raw_aac:
                continue

            au_size = len(raw_aac)
            au_headers_length = 16
            au_header = (au_size << 3) & 0xFFF8
            au_section = struct.pack(">HH", au_headers_length, au_header)

            seq = (seq + 1) & 0xFFFF
            rtp_header = struct.pack(
                ">BBHII",
                0x80,
                0x80 | payload_type if i == len(frames) - 1 else payload_type,
                seq,
                ts & 0xFFFFFFFF,
                ssrc,
            )
            rtp_packet = rtp_header + au_section + raw_aac

            interleaved = (
                struct.pack(">cBH", b"$", rtp_channel, len(rtp_packet)) + rtp_packet
            )

            writer.write(interleaved)
            await writer.drain()

            ts += 1024

            # Pace at frame rate
            target = start + (i + 1) * frame_interval
            delay = target - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)

            if i % 10 == 0:
                elapsed = time.monotonic() - start
                print(f"  Frame {i}/{len(frames)} ({elapsed:.1f}s)")

        elapsed = time.monotonic() - start
        print(f"\nDone! Streamed {len(frames)} frames in {elapsed:.1f}s")

        # TEARDOWN
        td_headers = f"Authorization: {digest_auth('TEARDOWN', uri_path)}\r\n"
        if session_id:
            td_headers += f"Session: {session_id}\r\n"
        await rtsp_send("TEARDOWN", rtsp_url, td_headers)

    finally:
        writer.close()
        await writer.wait_closed()


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    asyncio.run(test_backchannel(sys.argv[1], sys.argv[2]))
