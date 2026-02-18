#!/usr/bin/env python3
import sys
import json
import time
import socket
import struct
import subprocess
import audioop
import threading
import select
from pathlib import Path
from queue import Queue

# Make stdout line-buffered
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

# --- CONFIGURATION ---
PIPER_BIN = "./piper/piper"
MODEL = "models/en_US-amy-medium.onnx"
MODEL_JSON = Path(MODEL + ".json")

SOURCE_PORT = 17032

# RTP pacing
RTP_FRAME_MS = 20
SAMPLES_PER_FRAME_8K = 160
PCM_BYTES_PER_FRAME_8K = SAMPLES_PER_FRAME_8K * 2

# Polling: smaller => slightly lower jitter/TTFB, more CPU
SELECT_TIMEOUT = 0.001

# Soft-cancel: discard outgoing audio for this long after cancel
# (prevents most "tail audio" without restarting Piper)
DRAIN_SECONDS = 0.8

# If you keep cancelling repeatedly, let drain extend, but never forever
MAX_DRAIN_SECONDS = 2.0

# --- GLOBAL STATE ---
cmd_q: "Queue[dict]" = Queue()
stop_event = threading.Event()

state_lock = threading.Lock()

active = {"id": None, "host": None, "port": None}
drain_until = 0.0

piper_proc = None

rtp_state = {
    "seq": 1,
    "ts": 0,
    "ssrc": 0x12345678,
    "sock": None,
}

def load_voice_sample_rate(default_sr=22050) -> int:
    try:
        cfg = json.loads(MODEL_JSON.read_text(encoding="utf-8"))
        return int(cfg.get("audio", {}).get("sample_rate", default_sr))
    except Exception:
        return default_sr

VOICE_SR = load_voice_sample_rate()

def get_rtp_socket():
    if rtp_state["sock"] is None:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("0.0.0.0", SOURCE_PORT))
        rtp_state["sock"] = s
    return rtp_state["sock"]

def start_piper_process():
    return subprocess.Popen(
        [PIPER_BIN, "-m", MODEL, "--output-raw", "--json-input"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=0,
    )

def restart_piper():
    # Still keep this as a safety fallback (broken pipe, etc.)
    global piper_proc
    if piper_proc:
        try:
            piper_proc.kill()
            piper_proc.wait()
        except Exception:
            pass
    piper_proc = start_piper_process()

def stdin_reader():
    for line in sys.stdin:
        line = (line or "").strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
            cmd_q.put(msg)
        except Exception:
            continue

def send_rtp_packet(host, port, audio_bytes):
    if not host or not port:
        return

    sock = get_rtp_socket()

    hdr = struct.pack(
        "!BBHII",
        0x80, 0x00,
        rtp_state["seq"] & 0xFFFF,
        rtp_state["ts"],
        rtp_state["ssrc"],
    )

    ulaw_payload = audioop.lin2ulaw(audio_bytes, 2)

    try:
        sock.sendto(hdr + ulaw_payload, (host, port))
    except Exception:
        pass

    rtp_state["seq"] += 1
    rtp_state["ts"] += SAMPLES_PER_FRAME_8K

def audio_consumer_loop():
    global piper_proc, drain_until

    resample_state = None
    buffer_8k = bytearray()
    next_send_time = time.perf_counter()

    while not stop_event.is_set():
        if not piper_proc or piper_proc.poll() is not None:
            time.sleep(0.01)
            continue

        try:
            r, _, _ = select.select([piper_proc.stdout], [], [], SELECT_TIMEOUT)
            if not r:
                continue

            raw_chunk = piper_proc.stdout.read(2048)
            if not raw_chunk:
                continue

            now = time.perf_counter()

            with state_lock:
                host = active["host"]
                port = active["port"]
                local_drain_until = drain_until

            # During drain, keep consuming Piper stdout but do not send RTP.
            # Also clear any accumulated resampler/buffer so we don't "release" old audio later.
            if now < local_drain_until or not host or not port:
                resample_state = None
                buffer_8k.clear()
                next_send_time = now
                continue

            frag_8k, resample_state = audioop.ratecv(raw_chunk, 2, 1, VOICE_SR, 8000, resample_state)
            buffer_8k.extend(frag_8k)

            while len(buffer_8k) >= PCM_BYTES_PER_FRAME_8K:
                frame = buffer_8k[:PCM_BYTES_PER_FRAME_8K]
                del buffer_8k[:PCM_BYTES_PER_FRAME_8K]

                now2 = time.perf_counter()
                if now2 < next_send_time:
                    time.sleep(next_send_time - now2)

                send_rtp_packet(host, port, frame)

                next_send_time += (RTP_FRAME_MS / 1000.0)
                if now2 > next_send_time + 0.5:
                    next_send_time = now2

        except Exception as e:
            print(f"[PiperWorker] Audio Loop Error: {e}", file=sys.stderr)
            time.sleep(0.05)

def main():
    global piper_proc, drain_until

    threading.Thread(target=stdin_reader, daemon=True).start()
    restart_piper()
    threading.Thread(target=audio_consumer_loop, daemon=True).start()

    print(json.dumps({"type": "ready"}), flush=True)

    while True:
        try:
            msg = cmd_q.get()
            cmd = (msg.get("cmd") or "").lower()

            if cmd == "begin":
                stream_id = msg.get("id")
                host = msg.get("host")
                port = int(msg.get("port") or 0)

                with state_lock:
                    active["id"] = stream_id
                    active["host"] = host
                    active["port"] = port

                print(json.dumps({"type": "begun", "id": stream_id}), flush=True)

            elif cmd == "chunk":
                stream_id = msg.get("id")
                text = msg.get("text")

                with state_lock:
                    is_current = (stream_id is not None and stream_id == active["id"])

                # Ignore chunks for cancelled/old streams (prevents wasted synthesis)
                if (not is_current) or (not text) or (not piper_proc):
                    continue

                try:
                    payload = json.dumps({"text": text}) + "\n"
                    piper_proc.stdin.write(payload.encode("utf-8"))
                    piper_proc.stdin.flush()
                except BrokenPipeError:
                    restart_piper()

            elif cmd == "cancel":
                # SOFT cancel: stop sending audio immediately, keep Piper alive.
                stream_id = msg.get("id")
                now = time.perf_counter()

                with state_lock:
                    # Mute (no RTP) and start drain window
                    active["id"] = None
                    active["host"] = None
                    active["port"] = None

                    # extend drain if already draining
                    new_until = now + DRAIN_SECONDS
                    drain_until = max(drain_until, new_until)
                    drain_until = min(drain_until, now + MAX_DRAIN_SECONDS)

                # IMPORTANT: include id so ari.py waiter resolves by id
                print(json.dumps({"type": "done", "id": stream_id, "status": "cancelled"}), flush=True)

            elif cmd == "end":
                stream_id = msg.get("id")
                print(json.dumps({"type": "done", "id": stream_id, "status": "ok"}), flush=True)

        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"[PiperWorker] Error: {e}", file=sys.stderr)

    stop_event.set()
    if piper_proc:
        try:
            piper_proc.kill()
        except Exception:
            pass

if __name__ == "__main__":
    main()
