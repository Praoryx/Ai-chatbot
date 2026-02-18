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

ARI_HOST = "localhost:8088"
ARI_USER = "whisper"
ARI_PASS = "unsecurepassword"
APP_NAME = "whisper-stt"

WS_URL = f"ws://{ARI_HOST}/ari/events?api_key={ARI_USER}:{ARI_PASS}&app={APP_NAME}"
HTTP_BASE = f"http://{ARI_HOST}/ari/"

session = requests.Session()
session.auth = (ARI_USER, ARI_PASS)

STT_LISTEN_PORT = 9999
TTS_SOURCE_PORT = 17032
STT_EXTERNAL_HOST = f"127.0.0.1:{STT_LISTEN_PORT}"
TTS_EXTERNAL_HOST = f"127.0.0.1:{TTS_SOURCE_PORT}"
FORMAT = "ulaw"

WHISPER_SCRIPT = os.path.abspath("./whisper_listener.py")
PIPER_WORKER = os.path.abspath("./piper_worker.py")

POST_TTS_LISTEN_GUARD_SEC = 0.0

# ---------------------------------------------------------
# GLOBAL TRACKER for all channels created by this app
# (Protects against leaks if state gets overwritten)
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
    "transcript": [],  # Store conversation for MoM
}

def register_channel(channel_id):
    if channel_id:
        ALL_CREATED_CHANNELS.add(channel_id)

def unregister_channel(channel_id):
    if channel_id in ALL_CREATED_CHANNELS:
        ALL_CREATED_CHANNELS.remove(channel_id)

def ari(method, path, params=None, json_body=None):
    r = session.request(method, f"{HTTP_BASE}{path}", params=params, json=json_body, timeout=2.0)
    if not r.ok:
        raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text}")
    return r.json() if r.content else None

def is_internal_channel(ch: dict) -> bool:
    name = (ch.get("name") or "")
    return name.startswith("UnicastRTP/") or name.startswith("Snoop/")

def get_var(channel_id, varname):
    v = ari("GET", f"channels/{channel_id}/variable", params={"variable": varname})
    return v.get("value")

async def safe_hangup(channel_id):
    if not channel_id: return
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

# ----------------- Whisper & Piper Setup -----------------

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
    print("✅ Whisper listener started")

async def whisper_reader(proc):
    while True:
        line = await proc.stdout.readline()
        if not line: break
        s = line.decode(errors="ignore").strip()
        if not s: continue

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
                    print("🎤 STT FINAL:", text)
                    asyncio.create_task(on_user_text(text))
                continue
            continue
        print("[WHISPER]", s)
    print("❌ Whisper exited")

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
    print("✅ Piper worker started")

async def piper_reader(proc):
    while True:
        line = await proc.stdout.readline()
        if not line: break
        s = line.decode(errors="ignore").strip()
        if not s: continue
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
        if not fut.done(): fut.set_result("error")
    state["piper_waiters"].clear()
    print("❌ Piper exited")

async def piper_send(msg: dict):
    proc = state.get("piper_proc")
    if not proc or proc.returncode is not None or not proc.stdin:
        raise RuntimeError("Piper not running")
    async with state["piper_write_lock"]:
        proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
        await proc.stdin.drain()

async def piper_stream_say(text: str, host: str, port: int) -> str:
    stream_id = str(uuid.uuid4())
    state["active_stream_id"] = stream_id
    loop = asyncio.get_running_loop()
    done_fut = loop.create_future()
    state["piper_waiters"][stream_id] = done_fut

    await piper_send({"cmd": "begin", "id": stream_id, "host": host, "port": int(port)})
    try:
        await piper_send({"cmd": "chunk", "id": stream_id, "text": text})
        status = await done_fut
        return status
    except asyncio.CancelledError:
        try:
            await asyncio.shield(piper_send({"cmd": "cancel", "id": stream_id}))
        except Exception:
            pass
        raise
    finally:
        try:
            await asyncio.shield(piper_send({"cmd": "end", "id": stream_id}))
        except Exception:
            pass
        if state.get("active_stream_id") == stream_id:
            state["active_stream_id"] = None

async def piper_cancel_active():
    stream_id = state.get("active_stream_id")
    if not stream_id: return
    if stream_id not in state["piper_waiters"]:
        loop = asyncio.get_running_loop()
        state["piper_waiters"][stream_id] = loop.create_future()
    await piper_send({"cmd": "cancel", "id": stream_id})
    state["active_stream_id"] = None
    fut = state["piper_waiters"].get(stream_id)
    if fut and not fut.done():
        try: await asyncio.wait_for(fut, timeout=2.0)
        except asyncio.TimeoutError: pass

# ----------------- Turn logic -----------------

async def on_user_text(text: str):
    if not state["call_active"] or not state.get("tts_target"):
        return

    # 1. Record User Input for MoM
    state["transcript"].append({
        "speaker": "user",
        "text": text,
        "timestamp": time.time()
    })

    state["speak_seq"] += 1
    my_seq = state["speak_seq"]

    t = state.get("speak_task")
    if t and not t.done():
        t.cancel()

    async def _speak(seq: int):
        cancelled = False
        await set_mode("SPEAK")
        print("🗣️ TURN: THINKING (Streaming)...")

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
                    match = re.search(r'([.?!]+)', buffer)
                    if match:
                        end_idx = match.end()
                        sentence = buffer[:end_idx].strip()
                        buffer = buffer[end_idx:]
                        
                        if sentence:
                            print(f"  -> Sending chunk: '{sentence}'")
                            await piper_send({"cmd": "chunk", "id": stream_id, "text": sentence})
                    else:
                        break
            
            if buffer.strip() and not cancelled:
                 print(f"  -> Sending final chunk: '{buffer.strip()}'")
                 await piper_send({"cmd": "chunk", "id": stream_id, "text": buffer.strip()})
                 full_response += buffer

            # 2. Record Agent Response for MoM
            if full_response.strip() and not cancelled:
                state["transcript"].append({
                    "speaker": "agent",
                    "text": full_response.strip(),
                    "timestamp": time.time()
                })

            if not cancelled:
                status = await done_fut
                print("🗣️ Piper status:", status)

        except asyncio.CancelledError:
            cancelled = True
            try: await asyncio.shield(piper_send({"cmd": "cancel", "id": stream_id}))
            except: pass
            return
        except Exception as e:
            print("❌ Stream error:", e)
        finally:
            try: await asyncio.shield(piper_send({"cmd": "end", "id": stream_id}))
            except: pass
            
            if state.get("active_stream_id") == stream_id:
                state["active_stream_id"] = None

            if (not cancelled) and state.get("call_active") and state.get("speak_seq") == seq:
                await asyncio.sleep(POST_TTS_LISTEN_GUARD_SEC)
                await set_mode("LISTEN")
                print("🎧 TURN: LISTEN")

    state["speak_task"] = asyncio.create_task(_speak(my_seq))

async def on_barge_in():
    print("⚡ BARGE-IN: cancel TTS, back to LISTEN")
    t = state.get("speak_task")
    if t and not t.done(): t.cancel()
    try: await piper_cancel_active()
    except Exception as e: print("⚠️ cancel failed:", e)
    state["speak_seq"] += 1
    await set_mode("LISTEN")

# ----------------- Call setup -----------------
async def on_call_start(caller_id: str):
    state["call_active"] = True
    state["caller_id"] = caller_id
    state["transcript"] = [] # Reset transcript
    await set_mode("LISTEN")

    try: ari("POST", f"channels/{caller_id}/answer")
    except Exception: pass

    call_bridge = str(uuid.uuid4())
    ari("POST", "bridges", params={"type": "mixing", "bridgeId": call_bridge})
    ari("POST", f"bridges/{call_bridge}/addChannel", params={"channel": caller_id})
    state["call_bridge"] = call_bridge

    # TTS Setup
    tts_ext_id = f"tts-{int(time.time()*1000)}"
    tts_ext = ari("POST", "channels/externalMedia", params={
        "app": APP_NAME, "channelId": tts_ext_id, "external_host": TTS_EXTERNAL_HOST,
        "format": FORMAT, "encapsulation": "rtp", "transport": "udp", "direction": "both",
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
            if inject_host and inject_port: break
        except Exception: pass
        await asyncio.sleep(0.05)
    if not inject_host or not inject_port: raise RuntimeError("Failed to get UNICASTRTP_LOCAL_ADDRESS/PORT")
    state["tts_target"] = (inject_host, int(inject_port))
    print(f"🎯 Piper inject target: {inject_host}:{inject_port}")

    # STT Setup
    snoop = ari("POST", f"channels/{caller_id}/snoop", params={"app": APP_NAME, "spy": "in", "whisper": "none"})
    state["snoop_id"] = snoop["id"]
    register_channel(state["snoop_id"])

    stt_bridge = str(uuid.uuid4())
    ari("POST", "bridges", params={"type": "mixing", "bridgeId": stt_bridge})
    ari("POST", f"bridges/{stt_bridge}/addChannel", params={"channel": state["snoop_id"]})
    state["stt_bridge"] = stt_bridge

    stt_ext_id = f"stt-{int(time.time()*1000)}"
    stt_ext = ari("POST", "channels/externalMedia", params={
        "app": APP_NAME, "channelId": stt_ext_id, "external_host": STT_EXTERNAL_HOST,
        "format": FORMAT, "encapsulation": "rtp", "transport": "udp", "direction": "both",
    })
    stt_ext_chan = stt_ext.get("id") or (stt_ext.get("channel") or {}).get("id")
    state["stt_ext_chan"] = stt_ext_chan
    register_channel(stt_ext_chan)
    
    ari("POST", f"bridges/{stt_bridge}/addChannel", params={"channel": stt_ext_chan})
    print(f"🎤 STT RTP destination: udp://{STT_EXTERNAL_HOST}")
    print("✅ Call setup complete")

    # --- INITIAL GREETING ---
    print("👋 Triggering initial greeting (No Gemini)...")
    await set_mode("SPEAK")
    print("🗣️ TURN: GREETING (Hardcoded)")

    host, port = state["tts_target"]
    greeting_text = "Hello! I am Sarah from Barbie Builders. How can I help you today?"
    
    # Record greeting in transcript
    state["transcript"].append({
        "speaker": "agent",
        "text": greeting_text,
        "timestamp": time.time()
    })

    async def _greet():
        try:
            status = await piper_stream_say(greeting_text, host, port)
            print("🗣️ Greeting status:", status)
        except Exception as e:
            print("❌ Greeting failed:", e)
        finally:
            if state.get("call_active"):
                await asyncio.sleep(POST_TTS_LISTEN_GUARD_SEC)
                await set_mode("LISTEN")
                print("🎧 TURN: LISTEN")

    state["speak_task"] = asyncio.create_task(_greet())

async def save_mom_as_pdf(mom_markdown: str, filename: str, caller_id: str = None):
    """
    Convert Markdown MoM to styled PDF with Caller ID and Date.
    """
    def _convert():
        # Convert Markdown to HTML
        html_content = markdown.markdown(
            mom_markdown,
            extensions=['tables', 'fenced_code', 'nl2br']
        )
        
        # Get current date and time
        from datetime import datetime
        current_date = datetime.now().strftime("%B %d, %Y at %I:%M %p IST")
        
        # Add CSS styling for professional look
        styled_html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <style>
                body {{
                    font-family: 'Segoe UI', Arial, sans-serif;
                    line-height: 1.6;
                    color: #333;
                    max-width: 800px;
                    margin: 40px auto;
                    padding: 20px;
                }}
                .header-info {{
                    background-color: #f8f9fa;
                    border-left: 4px solid #3498db;
                    padding: 15px;
                    margin-bottom: 30px;
                    border-radius: 4px;
                }}
                .header-info p {{
                    margin: 5px 0;
                    font-size: 14px;
                    color: #555;
                }}
                .header-info strong {{
                    color: #2c3e50;
                    font-weight: 600;
                }}
                h1 {{
                    color: #2c3e50;
                    border-bottom: 3px solid #3498db;
                    padding-bottom: 10px;
                    margin-top: 0;
                }}
                h2 {{
                    color: #34495e;
                    margin-top: 30px;
                    border-bottom: 1px solid #bdc3c7;
                    padding-bottom: 5px;
                }}
                ul {{
                    line-height: 1.8;
                }}
                li {{
                    margin-bottom: 8px;
                }}
                strong {{
                    color: #2980b9;
                }}
                code {{
                    background-color: #ecf0f1;
                    padding: 2px 6px;
                    border-radius: 3px;
                }}
                table {{
                    border-collapse: collapse;
                    width: 100%;
                    margin: 20px 0;
                }}
                th, td {{
                    border: 1px solid #ddd;
                    padding: 12px;
                    text-align: left;
                }}
                th {{
                    background-color: #3498db;
                    color: white;
                }}
                .footer {{
                    margin-top: 40px;
                    padding-top: 20px;
                    border-top: 1px solid #bdc3c7;
                    text-align: center;
                    color: #7f8c8d;
                    font-size: 12px;
                }}
            </style>
        </head>
        <body>
            <div class="header-info">
                <p><strong>Caller ID:</strong> {caller_id or 'Unknown'}</p>
                <p><strong>Date & Time:</strong> {current_date}</p>
                <p><strong>Agent:</strong> Sarah (Barbie Builders)</p>
            </div>
            
            {html_content}
            
            <div class="footer">
                <p>Generated automatically by Barbie Builders AI Sales Assistant</p>
                <p>Document ID: MoM-{caller_id or 'UNKNOWN'}-{datetime.now().strftime("%Y%m%d%H%M%S")}</p>
            </div>
        </body>
        </html>
        """
        
        # Convert HTML to PDF
        HTML(string=styled_html).write_pdf(filename)
    
    # Run in thread pool to avoid blocking
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _convert)



async def on_call_end():
    print("🧹 Starting cleanup...")
    
     # --- MOM GENERATION ---
    transcript = state.get("transcript", [])
    caller_id = state.get("caller_id", "unknown")
    
    if transcript and len(transcript) > 1:
        print("📝 Generating Minutes of Meeting...")
        try:
            mom_content = await gemini_thinker.generate_mom(transcript)
            
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            pdf_filename = f"MoM_{caller_id}_{timestamp}.pdf"
            
            # Pass caller_id to the PDF generator
            await save_mom_as_pdf(mom_content, pdf_filename, caller_id=caller_id)
            
            print(f"✅ MoM saved to: {pdf_filename}")
        except Exception as e:
            print(f"❌ MoM generation failed: {e}")
    else:
        print("⚠️ No transcript to generate MoM (call too short)")

    # --- CLEANUP ---
    await set_mode("IDLE")
    state["call_active"] = False
    state["transcript"] = []
    
    try: await piper_cancel_active()
    except Exception: pass

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
            try: await asyncio.to_thread(ari, "DELETE", f"bridges/{bid}")
            except: pass

    for k in ("caller_id", "call_bridge", "stt_bridge", "snoop_id", "stt_ext_chan", "tts_ext_chan"):
        state[k] = None
    
    print(f"✨ Cleanup complete. Remaining tracked channels: {len(ALL_CREATED_CHANNELS)}")

async def main():
    await ensure_whisper_running()
    await ensure_piper_running()
    await set_mode("IDLE")
    print("🚀 ARI orchestrator running (Gemini + Piper + MoM)")
    async with websockets.connect(WS_URL, ping_interval=30) as ws:
        async for msg in ws:
            ev = json.loads(msg)
            et = ev.get("type")
            
            if et == "StasisStart":
                if ev.get("application") != APP_NAME: continue
                ch = ev.get("channel") or {}
                if is_internal_channel(ch): continue
                caller_id = ch.get("id")
                if not caller_id or state["call_active"]: continue
                print("📞 Incoming call:", caller_id)
                try: await on_call_start(caller_id)
                except Exception as e:
                    print("❌ call setup failed:", e)
                    await on_call_end()
            
            elif et == "ChannelDestroyed":
                ch = ev.get("channel") or {}
                cid = ch.get("id")
                if state["call_active"] and cid == state.get("caller_id"):
                    print("📴 Call ended:", cid)
                    await on_call_end()
            
            elif et == "StasisEnd":
                ch = ev.get("channel") or {}
                cid = ch.get("id")
                if cid in ALL_CREATED_CHANNELS:
                    unregister_channel(cid)
                if cid == state.get("caller_id"):
                    print("🛑 StasisEnd (Caller left):", cid)
                    await on_call_end()

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt:
        pass