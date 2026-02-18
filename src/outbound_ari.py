#!/usr/bin/env python3
import asyncio
import json
import os
import sys
import time
import uuid
import requests
import websockets
import markdown
from weasyprint import HTML

import gemini_thinker

# =======================
# Config (match your ari.py)
# =======================
ARI_HOST = os.getenv("ARI_HOST", "localhost:8088")
ARI_USER = os.getenv("ARI_USER", "whisper")
ARI_PASS = os.getenv("ARI_PASS", "unsecurepassword")

APP_NAME = os.getenv("APP_NAME", "whisper-stt")  # keep same as ari.py

WS_URL = f"ws://{ARI_HOST}/ari/events?api_key={ARI_USER}:{ARI_PASS}&app={APP_NAME}"
HTTP_BASE = f"http://{ARI_HOST}/ari/"

# Must match whisper_listener.py + piper_worker.py
STT_LISTEN_PORT = int(os.getenv("STT_LISTEN_PORT", "9999"))
TTS_SOURCE_PORT = int(os.getenv("TTS_SOURCE_PORT", "17032"))

STT_EXTERNAL_HOST = f"127.0.0.1:{STT_LISTEN_PORT}"
TTS_EXTERNAL_HOST = f"127.0.0.1:{TTS_SOURCE_PORT}"
FORMAT = "ulaw"

WHISPER_SCRIPT = os.path.abspath(os.getenv("WHISPER_SCRIPT", "./whisper_listener.py"))
PIPER_WORKER = os.path.abspath(os.getenv("PIPER_WORKER", "./piper_worker.py"))

POST_TTS_LISTEN_GUARD_SEC = float(os.getenv("POST_TTS_LISTEN_GUARD_SEC", "0.0"))

# Outbound target (endpoint number)
TARGET = os.getenv("TARGET", "6001")
# IMPORTANT: PJSIP endpoint dial string
DIAL_ENDPOINT = os.getenv("DIAL_ENDPOINT", f"PJSIP/{TARGET}")

CALLERID = os.getenv("CALLERID", "Sarah <1001>")
ORIGINATE_TIMEOUT = int(os.getenv("ORIGINATE_TIMEOUT", "30"))

GREETING_TEXT = os.getenv(
    "GREETING_TEXT",
    "Hello! I am Sarah from Barbie Builders. How can I help you today?"
)

# =======================
# HTTP session
# =======================
session = requests.Session()
session.auth = (ARI_USER, ARI_PASS)

def ari(method, path, params=None, json_body=None, timeout=5.0):
    r = session.request(method, f"{HTTP_BASE}{path}", params=params, json=json_body, timeout=timeout)
    if not r.ok:
        raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text}")
    return r.json() if r.content else None

def is_internal_channel(ch: dict) -> bool:
    name = (ch.get("name") or "")
    return name.startswith("UnicastRTP/") or name.startswith("Snoop/")

def get_var(channel_id, varname):
    v = ari("GET", f"channels/{channel_id}/variable", params={"variable": varname})
    return v.get("value") if v else None

# =======================
# Global trackers/state
# =======================
ALL_CREATED_CHANNELS = set()

state = {
    "call_active": False,
    "mode": "IDLE",

    "expected_channel_id": None,   # outbound call channel id (from originate)
    "caller_id": None,             # same as expected_channel_id once we get it

    "call_bridge": None,
    "stt_bridge": None,

    "snoop_id": None,
    "stt_ext_chan": None,
    "tts_ext_chan": None,
    "tts_target": None,            # (host, port) where piper_worker must send RTP

    "whisper_proc": None,
    "piper_proc": None,
    "piper_ready": asyncio.Event(),
    "piper_waiters": {},           # stream_id -> Future
    "active_stream_id": None,

    "speak_task": None,
    "speak_seq": 0,
    "piper_write_lock": asyncio.Lock(),

    "transcript": [],
    "connected_once": False,       # guard: setup only once
}

def register_channel(channel_id):
    if channel_id:
        ALL_CREATED_CHANNELS.add(channel_id)

def unregister_channel(channel_id):
    if channel_id in ALL_CREATED_CHANNELS:
        ALL_CREATED_CHANNELS.remove(channel_id)

async def safe_hangup(channel_id):
    if not channel_id:
        return
    try:
        await asyncio.to_thread(ari, "DELETE", f"channels/{channel_id}")
    except Exception:
        pass
    finally:
        unregister_channel(channel_id)

async def set_mode(mode: str):
    mode = (mode or "IDLE").strip().upper()
    state["mode"] = mode
    proc = state.get("whisper_proc")
    if proc and proc.returncode is None and proc.stdin:
        try:
            proc.stdin.write(f"MODE {mode}\n".encode("utf-8"))
            await proc.stdin.drain()
        except Exception:
            pass

# =======================
# Whisper
# =======================
async def ensure_whisper_running():
    if state["whisper_proc"] and state["whisper_proc"].returncode is None:
        return
    proc = await asyncio.create_subprocess_exec(
        sys.executable, WHISPER_SCRIPT,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    state["whisper_proc"] = proc
    asyncio.create_task(whisper_reader(proc))

async def whisper_reader(proc):
    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        s = line.decode(errors="ignore").strip()
        if not s:
            continue

        if s.startswith("{") and s.endswith("}"):
            try:
                ev = json.loads(s)
            except Exception:
                continue

            t = (ev.get("type") or "").lower()
            if t == "barge_in":
                if state.get("mode") == "SPEAK":
                    asyncio.create_task(on_barge_in())
                continue
            if t == "final":
                text = (ev.get("text") or "").strip()
                if text:
                    print("WHISPER FINAL:", text)
                    asyncio.create_task(on_user_text(text))
                continue

# =======================
# Piper worker protocol
# =======================
async def ensure_piper_running():
    if state["piper_proc"] and state["piper_proc"].returncode is None:
        return
    state["piper_ready"].clear()
    proc = await asyncio.create_subprocess_exec(
        sys.executable, PIPER_WORKER,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    state["piper_proc"] = proc
    asyncio.create_task(piper_reader(proc))
    await asyncio.wait_for(state["piper_ready"].wait(), timeout=10.0)

async def piper_reader(proc):
    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        s = line.decode(errors="ignore").strip()
        if not s:
            continue
        try:
            ev = json.loads(s)
        except Exception:
            continue

        if ev.get("type") == "ready":
            state["piper_ready"].set()
            continue

        if ev.get("type") == "done":
            sid = ev.get("id")
            if not sid:
                continue
            fut = state["piper_waiters"].pop(sid, None)
            if fut and not fut.done():
                fut.set_result(ev.get("status", "ok"))
            continue

    for _, fut in list(state["piper_waiters"].items()):
        if not fut.done():
            fut.set_result("error")
    state["piper_waiters"].clear()
    print("❌ Piper exited")

async def piper_send(msg: dict):
    proc = state.get("piper_proc")
    if not proc or proc.returncode is not None or not proc.stdin:
        raise RuntimeError("Piper not running")
    async with state["piper_write_lock"]:
        proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
        await proc.stdin.drain()

async def piper_cancel_active():
    sid = state.get("active_stream_id")
    if not sid:
        return
    if sid not in state["piper_waiters"]:
        state["piper_waiters"][sid] = asyncio.get_running_loop().create_future()
    try:
        await piper_send({"cmd": "cancel", "id": sid})
    finally:
        state["active_stream_id"] = None

# =======================
# Conversation turn logic
# =======================
async def on_user_text(text: str):
    if not state["call_active"] or not state.get("tts_target"):
        return

    state["transcript"].append({"speaker": "user", "text": text, "timestamp": time.time()})

    state["speak_seq"] += 1
    my_seq = state["speak_seq"]

    t = state.get("speak_task")
    if t and not t.done():
        t.cancel()

    async def _speak(seq: int):
        host, port = state["tts_target"]
        stream_id = str(uuid.uuid4())
        state["active_stream_id"] = stream_id

        fut = asyncio.get_running_loop().create_future()
        state["piper_waiters"][stream_id] = fut

        cancelled = False
        await set_mode("SPEAK")
        await piper_send({"cmd": "begin", "id": stream_id, "host": host, "port": int(port)})

        full = ""
        buf = ""
        try:
            async for chunk in gemini_thinker.get_ai_response_stream(text):
                if state.get("active_stream_id") != stream_id:
                    cancelled = True
                    break
                full += chunk
                buf += chunk

                # emit sentences quickly
                import re
                while True:
                    m = re.search(r'([.?!]+|[,;:])', buf)
                    if not m:
                        break
                    s = buf[:m.end()].strip()
                    buf = buf[m.end():]
                    if s:
                        await piper_send({"cmd": "chunk", "id": stream_id, "text": s})

            if buf.strip() and not cancelled:
                await piper_send({"cmd": "chunk", "id": stream_id, "text": buf.strip()})

            if full.strip():
                print("GEMINI:", full.strip())

            if full.strip() and not cancelled:
                state["transcript"].append({"speaker": "agent", "text": full.strip(), "timestamp": time.time()})

        except asyncio.CancelledError:
            cancelled = True
            try:
                await asyncio.shield(piper_send({"cmd": "cancel", "id": stream_id}))
            except Exception:
                pass
            return
        except Exception as e:
            print("❌ Gemini/Piper stream error:", e)
        finally:
            try:
                await asyncio.shield(piper_send({"cmd": "end", "id": stream_id}))
            except Exception:
                pass

            if state.get("active_stream_id") == stream_id:
                state["active_stream_id"] = None

            if (not cancelled) and state.get("call_active") and state.get("speak_seq") == seq:
                await asyncio.sleep(POST_TTS_LISTEN_GUARD_SEC)
                await set_mode("LISTEN")
                print("___LISTENING___")

    state["speak_task"] = asyncio.create_task(_speak(my_seq))

async def on_barge_in():
    print("BARGE-IN -> LISTEN")
    t = state.get("speak_task")
    if t and not t.done():
        t.cancel()
    try:
        await piper_cancel_active()
    except Exception:
        pass
    state["speak_seq"] += 1
    await set_mode("LISTEN")

async def greet_now():
    if not state.get("tts_target") or not state["call_active"]:
        return
    state["transcript"].append({"speaker": "agent", "text": GREETING_TEXT, "timestamp": time.time()})

    host, port = state["tts_target"]
    sid = str(uuid.uuid4())
    state["active_stream_id"] = sid
    state["piper_waiters"][sid] = asyncio.get_running_loop().create_future()

    await set_mode("SPEAK")
    print("PIPER :", GREETING_TEXT)

    try:
        await piper_send({"cmd": "begin", "id": sid, "host": host, "port": int(port)})
        await piper_send({"cmd": "chunk", "id": sid, "text": GREETING_TEXT})
    finally:
        try:
            await piper_send({"cmd": "end", "id": sid})
        except Exception:
            pass
        state["active_stream_id"] = None

    await asyncio.sleep(POST_TTS_LISTEN_GUARD_SEC)
    await set_mode("LISTEN")
    print("___LISTENING___")

# =======================
# MoM PDF
# =======================
async def save_mom_as_pdf(mom_markdown: str, filename: str, callerid: str = None):
    def convert():
        html_content = markdown.markdown(mom_markdown, extensions=["tables", "fenced_code", "nl2br"])
        from datetime import datetime
        current_date = datetime.now().strftime("%B %d, %Y at %I:%M %p IST")
        styled = f"""
        <!DOCTYPE html><html><head><meta charset="UTF-8">
        <style>
        body {{ font-family: Segoe UI, Arial, sans-serif; line-height: 1.6; color: #333; max-width: 900px; margin: 30px auto; padding: 20px; }}
        .meta {{ background: #f8f9fa; border-left: 4px solid #3498db; padding: 12px; margin-bottom: 18px; }}
        table {{ border-collapse: collapse; width: 100%; }}
        th, td {{ border: 1px solid #ddd; padding: 10px; }}
        th {{ background: #3498db; color: #fff; }}
        </style></head><body>
        <div class="meta">
          <div><strong>Channel:</strong> {callerid or "Unknown"}</div>
          <div><strong>Date & Time:</strong> {current_date}</div>
          <div><strong>Agent:</strong> Sarah (Barbie Builders)</div>
        </div>
        {html_content}
        </body></html>
        """
        HTML(string=styled).write_pdf(filename)

    await asyncio.get_running_loop().run_in_executor(None, convert)

# =======================
# Call setup / teardown
# =======================
async def on_call_up(channel_id: str):
    if state["connected_once"]:
        return
    state["connected_once"] = True

    state["call_active"] = True
    state["caller_id"] = channel_id
    state["transcript"] = []

    await set_mode("LISTEN")

    # Main mixing bridge
    call_bridge = str(uuid.uuid4())
    ari("POST", "bridges", params={"type": "mixing", "bridgeId": call_bridge})
    ari("POST", f"bridges/{call_bridge}/addChannel", params={"channel": channel_id})
    state["call_bridge"] = call_bridge

    # TTS externalMedia (UnicastRTP)
    tts_ext_id = f"tts-{int(time.time() * 1000)}"
    tts_ext = ari("POST", "channels/externalMedia", params={
        "app": APP_NAME,
        "channelId": tts_ext_id,
        "external_host": TTS_EXTERNAL_HOST,
        "format": FORMAT,
        "encapsulation": "rtp",
        "transport": "udp",
        "direction": "both",
    })
    tts_chan = tts_ext.get("id") or (tts_ext.get("channel") or {}).get("id")
    state["tts_ext_chan"] = tts_chan
    register_channel(tts_chan)
    ari("POST", f"bridges/{call_bridge}/addChannel", params={"channel": tts_chan})

    # Where Piper must send RTP (UNICASTRTP local)
    inject_host = None
    inject_port = None
    for _ in range(40):
        try:
            inject_host = get_var(tts_chan, "UNICASTRTP_LOCAL_ADDRESS")
            inject_port = get_var(tts_chan, "UNICASTRTP_LOCAL_PORT")
            if inject_host and inject_port:
                break
        except Exception:
            pass
        await asyncio.sleep(0.05)
    if not inject_host or not inject_port:
        raise RuntimeError("Failed to get UNICASTRTP_LOCAL_ADDRESS/PORT")
    state["tts_target"] = (inject_host, int(inject_port))

    # Snoop inbound audio for STT
    snoop = ari("POST", f"channels/{channel_id}/snoop", params={
        "app": APP_NAME,
        "spy": "in",
        "whisper": "none",
    })
    state["snoop_id"] = snoop.get("id")
    register_channel(state["snoop_id"])

    stt_bridge = str(uuid.uuid4())
    ari("POST", "bridges", params={"type": "mixing", "bridgeId": stt_bridge})
    ari("POST", f"bridges/{stt_bridge}/addChannel", params={"channel": state["snoop_id"]})
    state["stt_bridge"] = stt_bridge

    stt_ext_id = f"stt-{int(time.time() * 1000)}"
    stt_ext = ari("POST", "channels/externalMedia", params={
        "app": APP_NAME,
        "channelId": stt_ext_id,
        "external_host": STT_EXTERNAL_HOST,
        "format": FORMAT,
        "encapsulation": "rtp",
        "transport": "udp",
        "direction": "both",
    })
    stt_chan = stt_ext.get("id") or (stt_ext.get("channel") or {}).get("id")
    state["stt_ext_chan"] = stt_chan
    register_channel(stt_chan)
    ari("POST", f"bridges/{stt_bridge}/addChannel", params={"channel": stt_chan})

    print("✅ Call is UP. Bridges + externalMedia ready.")
    await greet_now()

async def on_call_end():
    print("Call ended -> cleanup")
    await set_mode("IDLE")
    state["call_active"] = False

    try:
        await piper_cancel_active()
    except Exception:
        pass

    # MoM
    tr = state.get("transcript") or []
    if len(tr) >= 2:
        try:
            mom_md = await gemini_thinker.generate_mom(tr)
            ts = time.strftime("%Y%m%d_%H%M%S")
            pdf = f"MoM_outbound_{TARGET}_{ts}.pdf"
            await save_mom_as_pdf(mom_md, pdf, callerid=state.get("caller_id"))
            print("✅ MoM saved:", pdf)
        except Exception as e:
            print("⚠️ MoM failed:", e)

    # hangup created channels
    for cid in list(ALL_CREATED_CHANNELS):
        await safe_hangup(cid)

    # delete bridges
    for bid in (state.get("stt_bridge"), state.get("call_bridge")):
        if bid:
            try:
                await asyncio.to_thread(ari, "DELETE", f"bridges/{bid}")
            except Exception:
                pass

    # reset
    for k in (
        "call_bridge", "stt_bridge", "snoop_id", "stt_ext_chan", "tts_ext_chan",
        "tts_target", "active_stream_id", "caller_id"
    ):
        state[k] = None
    state["connected_once"] = False
    ALL_CREATED_CHANNELS.clear()

# =======================
# Originate
# =======================
async def originate_outbound():
    print(f"Dialing endpoint: {DIAL_ENDPOINT}")
    try:
        resp = ari("POST", "channels", params={
            "endpoint": DIAL_ENDPOINT,
            "app": APP_NAME,
            "appArgs": "outbound",
            "callerId": CALLERID,
            "timeout": ORIGINATE_TIMEOUT,
        }, timeout=10.0)
    except RuntimeError as e:
        msg = str(e)
        if "Allocation failed" in msg:
            raise RuntimeError(
                f"{e}\n\nFix: set DIAL_ENDPOINT='PJSIP/6001' (not SIP/6001)."
            ) from None
        raise

    chan_id = resp.get("id")
    if not chan_id:
        raise RuntimeError(f"Originate returned no channel id: {resp}")

    state["expected_channel_id"] = chan_id
    register_channel(chan_id)
    print("Originate channel id:", chan_id)
    return chan_id

# =======================
# Main
# =======================
async def shutdown_procs():
    for key in ("whisper_proc", "piper_proc"):
        p = state.get(key)
        if not p:
            continue
        try:
            if p.returncode is None:
                p.terminate()
        except Exception:
            pass

async def main():
    await ensure_whisper_running()
    await ensure_piper_running()
    await set_mode("IDLE")

    print(f"Outbound ARI ready. TARGET={TARGET}  DIAL_ENDPOINT={DIAL_ENDPOINT}")

    try:
        async with websockets.connect(WS_URL, ping_interval=30) as ws:
            # originate after websocket is up, so we don't miss events
            await originate_outbound()

            while True:
                ev = json.loads(await ws.recv())
                et = ev.get("type")
                ch = ev.get("channel") or {}
                cid = ch.get("id")

                expected = state.get("expected_channel_id")

                # Only track our outbound channel; ignore other non-internal channels.
                if expected and cid and cid != expected and not is_internal_channel(ch):
                    continue

                if et == "StasisStart":
                    if ev.get("application") != APP_NAME:
                        continue
                    if is_internal_channel(ch):
                        continue
                    if cid == expected:
                        print("StasisStart (outbound channel):", cid, "state=", ch.get("state"))
                    continue

                if et == "ChannelStateChange":
                    if cid != expected:
                        continue
                    st = (ch.get("state") or "").lower()
                    if st == "up" and not state["call_active"]:
                        print("✅ Channel is UP:", cid)
                        await on_call_up(cid)
                    continue

                if et in ("ChannelDestroyed", "StasisEnd"):
                    if cid == expected:
                        await on_call_end()
                        return

    finally:
        await shutdown_procs()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
