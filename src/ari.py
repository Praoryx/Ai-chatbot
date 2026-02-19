#!/usr/bin/env python3
import asyncio
import json
import os
import sys
import time
import uuid
import argparse
import requests
import websockets
from weasyprint import HTML

import gemini_thinker
from mom_pdf_template import render_mom_html


# =======================
# Config
# =======================
ARI_HOST = os.getenv("ARI_HOST", "localhost:8088")
ARI_USER = os.getenv("ARI_USER", "whisper")
ARI_PASS = os.getenv("ARI_PASS", "unsecurepassword")

APP_NAME = os.getenv("APP_NAME", "whisper-stt")

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

# Outbound defaults (env overridable)
TARGET = os.getenv("TARGET", "6001")
DIAL_ENDPOINT = os.getenv("DIAL_ENDPOINT", f"PJSIP/{TARGET}")
CALLERID = os.getenv("CALLERID", "Sarah <1001>")
ORIGINATE_TIMEOUT = int(os.getenv("ORIGINATE_TIMEOUT", "30"))

GREETING_TEXT = os.getenv(
    "GREETING_TEXT",
    "Hello! I am Sarah from Barbie Builders. How can I help you today in your real estate needs?"
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
    return v.get("value")


# ---------------------------------------------------------
# GLOBAL TRACKER for all channels created by this app
# ---------------------------------------------------------
ALL_CREATED_CHANNELS = set()

state = {
    "call_active": False,
    "mode": "IDLE",
    "caller_id": None,
    "call_bridge": None,
    "stt_bridge": None,
    "snoop_id": None,
    "stt_ext_chan": None,
    "tts_ext_chan": None,
    "tts_target": None,
    "whisper_proc": None,
    "piper_proc": None,
    "piper_ready": asyncio.Event(),
    "piper_waiters": {},
    "active_stream_id": None,
    "speak_task": None,
    "speak_seq": 0,
    "piper_write_lock": asyncio.Lock(),
    "transcript": [],

    # combined inbound/outbound
    "run_mode": "INBOUND",         # INBOUND | OUTBOUND
    "expected_channel_id": None,   # outbound channel id from originate
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
    except Exception as e:
        if "404" not in str(e):
            print(f"⚠️ Hangup failed for {channel_id}: {e}")
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
                print("[WHISPER]", s)
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
            continue

        print("[WHISPER]", s)
    print("❌ Whisper exited")


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
            print("[PIPER]", s)
            continue

        if ev.get("type") == "ready":
            state["piper_ready"].set()
            continue

        if ev.get("type") == "done":
            sid = ev.get("id")
            status = ev.get("status", "ok")
            fut = state["piper_waiters"].pop(sid, None)
            if fut and not fut.done():
                fut.set_result(status)
            continue

    for sid, fut in list(state["piper_waiters"].items()):
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
    stream_id = state.get("active_stream_id")
    if not stream_id:
        return
    if stream_id not in state["piper_waiters"]:
        loop = asyncio.get_running_loop()
        state["piper_waiters"][stream_id] = loop.create_future()
    await piper_send({"cmd": "cancel", "id": stream_id})
    state["active_stream_id"] = None
    fut = state["piper_waiters"].get(stream_id)
    if fut and not fut.done():
        try:
            await asyncio.wait_for(fut, timeout=0.2)
        except asyncio.TimeoutError:
            pass


# =======================
# Turn logic
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
        cancelled = False
        await set_mode("SPEAK")

        host, port = state["tts_target"]
        stream_id = str(uuid.uuid4())
        state["active_stream_id"] = stream_id

        loop = asyncio.get_running_loop()
        done_fut = loop.create_future()
        state["piper_waiters"][stream_id] = done_fut

        await piper_send({"cmd": "begin", "id": stream_id, "host": host, "port": int(port)})

        try:
            buffer = ""
            full_response = ""

            async for chunk in gemini_thinker.get_ai_response_stream(text):
                if state.get("active_stream_id") != stream_id:
                    cancelled = True
                    break

                buffer += chunk
                full_response += chunk

                while True:
                    import re
                    match = re.search(r'([.?!]+|[,;:])', buffer)
                    if match:
                        end_idx = match.end()
                        sentence = buffer[:end_idx].strip()
                        buffer = buffer[end_idx:]
                        if sentence:
                            await piper_send({"cmd": "chunk", "id": stream_id, "text": sentence})
                    else:
                        break

            if buffer.strip() and not cancelled:
                await piper_send({"cmd": "chunk", "id": stream_id, "text": buffer.strip()})

            if full_response.strip():
                print(f"GEMINI: {full_response.strip()}")

            if full_response.strip() and not cancelled:
                state["transcript"].append({"speaker": "agent", "text": full_response.strip(), "timestamp": time.time()})

            if not cancelled:
                status = await done_fut
                print("🗣️ Piper status:", status)

        except asyncio.CancelledError:
            cancelled = True
            try:
                await asyncio.shield(piper_send({"cmd": "cancel", "id": stream_id}))
            except Exception:
                pass
            return
        except Exception as e:
            print("❌ Stream error:", e)
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
    print("\nBack to LISTENING")
    t = state.get("speak_task")
    if t and not t.done():
        t.cancel()
    try:
        await piper_cancel_active()
    except Exception as e:
        print("⚠️ cancel failed:", e)
    state["speak_seq"] += 1
    await set_mode("LISTEN")


# =======================
# Call setup
# =======================
async def on_call_start(caller_id: str):
    state["call_active"] = True
    state["caller_id"] = caller_id
    state["transcript"] = []
    await set_mode("LISTEN")

    try:
        ari("POST", f"channels/{caller_id}/answer")
    except Exception:
        pass

    call_bridge = str(uuid.uuid4())
    ari("POST", "bridges", params={"type": "mixing", "bridgeId": call_bridge})
    ari("POST", f"bridges/{call_bridge}/addChannel", params={"channel": caller_id})
    state["call_bridge"] = call_bridge

    # TTS externalMedia
    tts_ext_id = f"tts-{int(time.time()*1000)}"
    tts_ext = ari("POST", "channels/externalMedia", params={
        "app": APP_NAME,
        "channelId": tts_ext_id,
        "external_host": TTS_EXTERNAL_HOST,
        "format": FORMAT,
        "encapsulation": "rtp",
        "transport": "udp",
        "direction": "both",
    })
    tts_ext_chan = tts_ext.get("id") or (tts_ext.get("channel") or {}).get("id")
    state["tts_ext_chan"] = tts_ext_chan
    register_channel(tts_ext_chan)
    ari("POST", f"bridges/{call_bridge}/addChannel", params={"channel": tts_ext_chan})

    inject_host = None
    inject_port = None
    for _ in range(40):
        try:
            inject_host = get_var(tts_ext_chan, "UNICASTRTP_LOCAL_ADDRESS")
            inject_port = get_var(tts_ext_chan, "UNICASTRTP_LOCAL_PORT")
            if inject_host and inject_port:
                break
        except Exception:
            pass
        await asyncio.sleep(0.05)
    if not inject_host or not inject_port:
        raise RuntimeError("Failed to get UNICASTRTP_LOCAL_ADDRESS/PORT")
    state["tts_target"] = (inject_host, int(inject_port))

    # STT snoop + bridge + externalMedia
    snoop = ari("POST", f"channels/{caller_id}/snoop", params={
        "app": APP_NAME,
        "spy": "in",
        "whisper": "none",
    })
    state["snoop_id"] = snoop["id"]
    register_channel(state["snoop_id"])

    stt_bridge = str(uuid.uuid4())
    ari("POST", "bridges", params={"type": "mixing", "bridgeId": stt_bridge})
    ari("POST", f"bridges/{stt_bridge}/addChannel", params={"channel": state["snoop_id"]})
    state["stt_bridge"] = stt_bridge

    stt_ext_id = f"stt-{int(time.time()*1000)}"
    stt_ext = ari("POST", "channels/externalMedia", params={
        "app": APP_NAME,
        "channelId": stt_ext_id,
        "external_host": STT_EXTERNAL_HOST,
        "format": FORMAT,
        "encapsulation": "rtp",
        "transport": "udp",
        "direction": "both",
    })
    stt_ext_chan = stt_ext.get("id") or (stt_ext.get("channel") or {}).get("id")
    state["stt_ext_chan"] = stt_ext_chan
    register_channel(stt_ext_chan)
    ari("POST", f"bridges/{stt_bridge}/addChannel", params={"channel": stt_ext_chan})

    print("Call STARTED")

    # Greeting
    await set_mode("SPEAK")
    host, port = state["tts_target"]
    greeting_text = GREETING_TEXT
    print("PIPER :", greeting_text)

    state["transcript"].append({"speaker": "agent", "text": greeting_text, "timestamp": time.time()})

    async def _greet():
        stream_id = None
        try:
            stream_id = str(uuid.uuid4())
            state["active_stream_id"] = stream_id

            loop = asyncio.get_running_loop()
            done_fut = loop.create_future()
            state["piper_waiters"][stream_id] = done_fut

            await piper_send({"cmd": "begin", "id": stream_id, "host": host, "port": int(port)})
            await piper_send({"cmd": "chunk", "id": stream_id, "text": greeting_text})
            await done_fut

        except asyncio.CancelledError:
            try:
                if stream_id:
                    await asyncio.shield(piper_send({"cmd": "cancel", "id": stream_id}))
            except Exception:
                pass
            raise

        except Exception as e:
            print("❌ Greeting failed:", e)

        finally:
            try:
                if stream_id:
                    await asyncio.shield(piper_send({"cmd": "end", "id": stream_id}))
            except Exception:
                pass

            if state.get("active_stream_id") == stream_id:
                state["active_stream_id"] = None

            if state.get("call_active"):
                await asyncio.sleep(POST_TTS_LISTEN_GUARD_SEC)
                await set_mode("LISTEN")

    state["speak_task"] = asyncio.create_task(_greet())


# =======================
# MoM PDF (now uses external template module)
# =======================
async def save_mom_as_pdf(mom_markdown: str, filename: str, caller_id: str = None):
    def _convert():
        html_str = render_mom_html(mom_markdown, caller_id=caller_id)
        HTML(string=html_str).write_pdf(filename)

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _convert)


async def on_call_end():
    transcript = state.get("transcript", [])
    caller_id = state.get("caller_id", "unknown")

    if transcript and len(transcript) > 1:
        print("\nGenerating Minutes of Meeting")
        try:
            mom_content = await gemini_thinker.generate_mom(transcript)
            timestamp = time.strftime("%d%m")
            pdf_filename = f"MoM_{caller_id}_{timestamp}.pdf"
            await save_mom_as_pdf(mom_content, pdf_filename, caller_id=caller_id)
            print(f"✅ MoM saved to: {pdf_filename}")
        except Exception as e:
            print(f"❌ MoM generation failed: {e}")
    else:
        print("⚠️ No transcript to generate MoM (call too short)")

    await set_mode("IDLE")
    state["call_active"] = False
    state["transcript"] = []

    try:
        await piper_cancel_active()
    except Exception:
        pass

    targets = [state.get("snoop_id"), state.get("stt_ext_chan"), state.get("tts_ext_chan")]
    for cid in list(ALL_CREATED_CHANNELS):
        if cid not in targets:
            targets.append(cid)

    for cid in targets:
        if cid:
            await safe_hangup(cid)

    for key in ("stt_bridge", "call_bridge"):
        bid = state.get(key)
        if bid:
            try:
                await asyncio.to_thread(ari, "DELETE", f"bridges/{bid}")
            except:
                pass

    for k in ("caller_id", "call_bridge", "stt_bridge", "snoop_id", "stt_ext_chan", "tts_ext_chan", "tts_target"):
        state[k] = None

    state["expected_channel_id"] = None
    print("CALL ENDED")


# =======================
# Outbound originate
# =======================
async def originate_outbound():
    print(f"Dialing endpoint: {DIAL_ENDPOINT}")
    resp = ari("POST", "channels", params={
        "endpoint": DIAL_ENDPOINT,
        "app": APP_NAME,
        "appArgs": "outbound",
        "callerId": CALLERID,
        "timeout": ORIGINATE_TIMEOUT,
    }, timeout=10.0)

    chan_id = (resp or {}).get("id")
    if not chan_id:
        raise RuntimeError(f"Originate returned no channel id: {resp}")

    state["expected_channel_id"] = chan_id
    register_channel(chan_id)
    print("Originate channel id:", chan_id)
    return chan_id


# =======================
# CLI
# =======================
def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=["inbound", "outbound"], default=None)
    p.add_argument("--target", default=None)
    p.add_argument("--dial-endpoint", default=None)
    p.add_argument("--callerid", default=None)
    p.add_argument("--timeout", type=int, default=None)
    return p.parse_args()


def ask(prompt: str, default: str = "") -> str:
    if default:
        s = input(f"{prompt} [{default}]: ").strip()
        return s if s else default
    return input(f"{prompt}: ").strip()


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
    args = parse_args()

    mode = (args.mode or "").strip().lower()
    if not mode:
        mode = ask("Mode? (inbound/outbound)", "inbound").lower()

    if mode == "outbound":
        state["run_mode"] = "OUTBOUND"

        if args.target:
            os.environ["TARGET"] = args.target
        if args.dial_endpoint:
            os.environ["DIAL_ENDPOINT"] = args.dial_endpoint
        if args.callerid:
            os.environ["CALLERID"] = args.callerid
        if args.timeout is not None:
            os.environ["ORIGINATE_TIMEOUT"] = str(args.timeout)

        global TARGET, DIAL_ENDPOINT, CALLERID, ORIGINATE_TIMEOUT
        TARGET = os.getenv("TARGET", TARGET)
        DIAL_ENDPOINT = os.getenv("DIAL_ENDPOINT", DIAL_ENDPOINT)
        CALLERID = os.getenv("CALLERID", CALLERID)
        ORIGINATE_TIMEOUT = int(os.getenv("ORIGINATE_TIMEOUT", str(ORIGINATE_TIMEOUT)))

        print(f"✅ Mode: OUTBOUND  DIAL_ENDPOINT={DIAL_ENDPOINT}")
    else:
        state["run_mode"] = "INBOUND"
        print("✅ Mode: INBOUND (waiting for calls)")

    await ensure_whisper_running()
    await ensure_piper_running()
    await set_mode("IDLE")

    try:
        async with websockets.connect(WS_URL, ping_interval=30) as ws:
            if state["run_mode"] == "OUTBOUND":
                await originate_outbound()

            async for msg in ws:
                ev = json.loads(msg)
                et = ev.get("type")

                ch = ev.get("channel") or {}
                cid = ch.get("id")
                expected = state.get("expected_channel_id")

                if state["run_mode"] == "OUTBOUND" and expected and cid and cid != expected and not is_internal_channel(ch):
                    continue

                if et == "StasisStart":
                    if ev.get("application") != APP_NAME:
                        continue
                    if is_internal_channel(ch):
                        continue

                    if state["run_mode"] == "INBOUND":
                        if cid and (not state["call_active"]):
                            try:
                                await on_call_start(cid)
                            except Exception as e:
                                print("❌ call setup failed:", e)
                                await on_call_end()
                    continue

                if et == "ChannelStateChange":
                    if state["run_mode"] == "OUTBOUND" and expected and cid == expected:
                        st = (ch.get("state") or "").lower()
                        if st == "up" and not state["call_active"]:
                            print("✅ Outbound channel is UP:", cid)
                            try:
                                await on_call_start(cid)
                            except Exception as e:
                                print("❌ call setup failed:", e)
                                await on_call_end()
                    continue

                if et == "ChannelDestroyed":
                    if state["call_active"] and cid == state.get("caller_id"):
                        print("📴 Call ended:", cid)
                        await on_call_end()
                    continue

                if et == "StasisEnd":
                    if cid in ALL_CREATED_CHANNELS:
                        unregister_channel(cid)
                    if cid == state.get("caller_id"):
                        await on_call_end()
                    continue

    finally:
        await shutdown_procs()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
