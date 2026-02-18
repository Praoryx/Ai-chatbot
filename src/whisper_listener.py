#!/usr/bin/env python3
import sys, socket, struct, time, json, audioop, traceback, threading
from collections import deque
import numpy as np
import torch
from scipy.signal import resample_poly
from faster_whisper import WhisperModel
from silero_vad import load_silero_vad

# Make stdout line-buffered
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

# Reduce jitter
try:
    torch.set_num_threads(1)
except Exception:
    pass

LISTEN_IP = "0.0.0.0"
LISTEN_PORT = 9999

IN_SR = 8000
OUT_SR = 16000

WHISPER_CONTEXT = """
I am interested in buying a flat,apartment,villa,plot or a property. 
My budget is in Lakhs or Crores.Is it a 2BHK ,3BHK or 4BHK?
"""

# 256 samples @ 8k = 32ms
FRAME_SECONDS = 0.032
IN_SAMPLES = int(IN_SR * FRAME_SECONDS)

VAD_THRESHOLD = 0.95
SILENCE_FRAMES_TO_END = 10      # ~320ms silence

BARGE_CONSEC_SPEECH_FRAMES = 3  # ~100ms speech to trigger interrupt
MIN_UTTER_SEC = (BARGE_CONSEC_SPEECH_FRAMES * FRAME_SECONDS) - 0.01

PRE_ROLL_SEC = 2 * MIN_UTTER_SEC
PRE_ROLL_SAMPLES = int(IN_SR * PRE_ROLL_SEC)

_mode_lock = threading.Lock()
_mode = "IDLE"
_transition_handoff = False  # Flag to preserve buffer across mode switch

def set_mode(m: str):
    global _mode, _transition_handoff
    with _mode_lock:
        new_mode = (m or "IDLE").strip().upper()
        # If we are handing off a barge-in, DON'T reset; otherwise normal reset
        if _transition_handoff and new_mode == "LISTEN":
            pass # Keep buffer alive
        else:
            _transition_handoff = False # Reset flag for normal switch
        _mode = new_mode

def get_mode() -> str:
    with _mode_lock:
        return _mode

def stdin_control_thread():
    try:
        for line in sys.stdin:
            line = (line or "").strip()
            if not line: continue
            if line.upper() == "QUIT":
                print(json.dumps({"type": "log", "level": "info", "msg": "QUIT received", "ts": time.time()}))
                sys.exit(0)
            if line.upper().startswith("MODE "):
                _, m = line.split(" ", 1)
                set_mode(m)
                print(json.dumps({"type": "mode", "mode": get_mode(), "ts": time.time()}))
    except Exception:
        pass

# print("🔄 Loading Whisper + Silero VAD...", file=sys.stderr)
model = WhisperModel("base.en", device="cpu", compute_type="int8")
vad_model = load_silero_vad()
# print("✅ Whisper streaming listener ready", file=sys.stderr)

def parse_rtp(packet: bytes):
    if len(packet) < 12: return None
    v_p_x_cc = packet[0]
    cc = v_p_x_cc & 0x0F
    x = (v_p_x_cc >> 4) & 1
    header_len = 12 + cc * 4
    if x:
        if len(packet) < header_len + 4: return None
        ext_len = struct.unpack("!H", packet[header_len + 2:header_len + 4])[0]
        header_len += 4 + ext_len * 4
    return packet[header_len:] if len(packet) > header_len else None

@torch.no_grad()
def vad_prob_8k(frame8k_float: np.ndarray) -> float:
    x = torch.from_numpy(frame8k_float.astype(np.float32, copy=False))
    p = vad_model(x.unsqueeze(0), IN_SR).item()
    return float(p)

def _whisper_transcribe(audio16k: np.ndarray) -> str:
    segments, _ = model.transcribe(
        audio16k,
        language="en",
        vad_filter=False,
        beam_size=1,
        temperature=0.0,
        condition_on_previous_text=False,
        compression_ratio_threshold=1.8,
        no_speech_threshold=0.5,
        initial_prompt=WHISPER_CONTEXT 
    )
    return "".join(seg.text for seg in segments).strip()

def run_listener_forever():
    global _transition_handoff
    
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((LISTEN_IP, LISTEN_PORT))

    # print(f"🎧 Listening RTP ulaw on udp://{LISTEN_IP}:{LISTEN_PORT}", file=sys.stderr)

    pcm_buf = deque()
    pre_roll = deque(maxlen=PRE_ROLL_SAMPLES)
    speech_acc_8k = []
    silence_frames = 0
    last_mode = "IDLE"
    
    # Barge-in state (detection only)
    barge_speech_frames = 0

    def reset_utterance_state(reset_vad_state: bool = True):
        nonlocal silence_frames, barge_speech_frames
        silence_frames = 0
        speech_acc_8k.clear()
        pcm_buf.clear()
        barge_speech_frames = 0
        if reset_vad_state:
            try: vad_model.reset_states()
            except Exception: pass

    while True:
        pkt, _ = sock.recvfrom(2048)
        m = get_mode()

        # MODE SWITCH LOGIC
        if m != last_mode:
            # If handing off barge-in -> LISTEN, PRESERVE buffer!
            if _transition_handoff and m == "LISTEN":
                # Do NOT reset buffers
                _transition_handoff = False # Consumed
            else:
                # Normal mode switch (e.g. IDLE->LISTEN or LISTEN->SPEAK)
                reset_utterance_state(reset_vad_state=True)
            
            last_mode = m

        payload = parse_rtp(pkt)
        if not payload: continue

        pcm_bytes = audioop.ulaw2lin(payload, 2)
        pcm16 = np.frombuffer(pcm_bytes, dtype=np.int16)
        for s in pcm16: pcm_buf.append(int(s))

        if len(pcm_buf) < IN_SAMPLES: continue

        frame8k_i16 = np.array([pcm_buf.popleft() for _ in range(IN_SAMPLES)], dtype=np.int16)
        frame8k_float = (frame8k_i16.astype(np.float32) / 32768.0)
        
        pre_roll.extend(frame8k_float.tolist())
        p = vad_prob_8k(frame8k_float)
        is_speech = (p >= VAD_THRESHOLD)

        # ---------- SPEAK MODE (Detection Only) ----------
        if m == "SPEAK":
            # If we already triggered handoff, keep buffering until mode switch happens!
            if _transition_handoff:
                speech_acc_8k.extend(frame8k_float.tolist())
                continue

            if is_speech:
                barge_speech_frames += 1
                if barge_speech_frames >= BARGE_CONSEC_SPEECH_FRAMES:
                    print(json.dumps({"type": "barge_in", "ts": time.time()}))
                    
                    # 1. Start buffering immediately
                    _transition_handoff = True
                    speech_acc_8k.clear()
                    speech_acc_8k.extend(list(pre_roll)) # Dump history
                    # Current frame will be added in next loop via `if _transition_handoff`
            else:
                barge_speech_frames = 0
            continue

        # ---------- LISTEN MODE (Universal Capture) ----------
        if m != "LISTEN": continue

        # Normal recording logic handles EVERYTHING (barge-in handoff OR normal speech)
        if is_speech:
            speech_acc_8k.extend(frame8k_float.tolist())
            silence_frames = 0
            continue

        # Silence logic
        if len(speech_acc_8k) > 0:
            silence_frames += 1
            
            if (len(speech_acc_8k) >= int(IN_SR * MIN_UTTER_SEC)) and (silence_frames >= SILENCE_FRAMES_TO_END):
                audio8k = np.array(speech_acc_8k, dtype=np.float32)
                
                # Transcribe
                audio16k = resample_poly(audio8k, 2, 1).astype(np.float32, copy=False)
                try:
                    final_text = _whisper_transcribe(audio16k)
                    if final_text:
                        print(json.dumps({"type": "final", "text": final_text}))
                except Exception as e:
                    print(json.dumps({"type": "error", "msg": str(e)}))
                
                # Reset for next utterance
                reset_utterance_state(reset_vad_state=True)
            
            elif silence_frames >= SILENCE_FRAMES_TO_END:
                # Discard noise
                speech_acc_8k.clear()
                silence_frames = 0

def main():
    threading.Thread(target=stdin_control_thread, daemon=True).start()
    while True:
        try:
            run_listener_forever()
        except Exception as e:
            print("❌ Whisper listener crashed:", e, file=sys.stderr)
            traceback.print_exc()
            time.sleep(1.0)

if __name__ == "__main__":
    main()