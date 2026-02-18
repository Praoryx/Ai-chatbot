#!/usr/bin/env python3
import os
import asyncio
from typing import AsyncGenerator, List, Dict, Optional
from dotenv import load_dotenv
import google.generativeai as genai

load_dotenv("API_Key.env")

API_KEY = os.getenv("GEMINI_API_KEY")
MODEL_NAME = "gemini-2.5-flash-lite"

if not API_KEY:
    raise ValueError("❌ GEMINI_API_KEY not found in API_Key.env")

genai.configure(api_key=API_KEY)

SYSTEM_INSTRUCTION = """
You are Sarah, a professional and friendly real estate agent for Barbie Builders.
Your goal is to qualify leads by having a natural conversation.

CRITICAL VOICE RULES:
1. Keep every response under 2 sentences. Use simple, spoken English.
2. NEVER use lists, bullet points, bold text, or markdown. Speak in full, flowing sentences.
3. Ask only ONE question at a time. Wait for the user's answer before moving on.
4. Do not make up facts. If unsure, say you will check with the team.

CONVERSATION FLOW:
1. Contextual Awareness: If the user mentions a location, immediately tailor your suggestions to that area.
2. Data Gathering: Casually collect these details over multiple turns:
   - Preferred Location
   - Property Type (3BHK, Villa, Plot)
   - Budget Range

TONE: Warm, professional, and concise. Treat this like a phone call, not an email.
"""

GEN_CONFIG = genai.GenerationConfig(
    temperature=0.7
)

model = genai.GenerativeModel(
    model_name=MODEL_NAME,
    system_instruction=SYSTEM_INSTRUCTION
)

# ---- Manual history (kept small for latency) ----
_HISTORY: List[Dict] = []
_HISTORY_LOCK = asyncio.Lock()
MAX_TURNS = 10  # last N user+assistant turns

def _trim_history(history: List[Dict]) -> List[Dict]:
    # Each "turn" here is one message dict; we keep last 2*MAX_TURNS messages.
    keep = 2 * MAX_TURNS
    return history[-keep:] if len(history) > keep else history

def _mk_user_msg(text: str) -> Dict:
    return {"role": "user", "parts": [text]}

def _mk_model_msg(text: str) -> Dict:
    return {"role": "model", "parts": [text]}

async def get_ai_response_stream(user_text: str) -> AsyncGenerator[str, None]:
    """
    Streaming text generator that is interruption-safe:
    - Does NOT use chat_session.
    - Appends to history only if the stream completes normally.
    """
    # Snapshot current history quickly (don’t hold lock during streaming)
    async with _HISTORY_LOCK:
        hist_snapshot = list(_HISTORY)

    contents = hist_snapshot + [_mk_user_msg(user_text)]

    response_iter = None
    iterator = None
    full = []

    def _next_or_none(it):
        try:
            return next(it)
        except StopIteration:
            return None

    try:
        # Start streaming (sync SDK call, so offload to a thread)
        response_iter = await asyncio.to_thread(
            model.generate_content,
            contents,
            stream=True,
            generation_config=GEN_CONFIG,
        )
        iterator = iter(response_iter)

        while True:
            chunk = await asyncio.to_thread(_next_or_none, iterator)
            if chunk is None:
                break

            # IMPORTANT: only touch chunk.text, never response_iter.text
            text = None
            try:
                text = chunk.text
            except Exception:
                text = None

            if text:
                full.append(text)
                yield text

    except asyncio.CancelledError:
        # Interrupted turn: do not update history
        raise

    except Exception as e:
        print(f"❌ Gemini Error: {e}")
        yield " I'm sorry, I'm having trouble connecting right now."
        return

    # Stream completed normally: commit to history
    final_text = "".join(full).strip()
    if final_text:
        async with _HISTORY_LOCK:
            _HISTORY.append(_mk_user_msg(user_text))
            _HISTORY.append(_mk_model_msg(final_text))
            _HISTORY[:] = _trim_history(_HISTORY)

# --- MoM model (your code can remain) ---
mom_model = genai.GenerativeModel(
    model_name=MODEL_NAME,
    system_instruction="""You are an expert executive assistant.
Your task is to summarize sales calls into structured Minutes of Meeting (MoM) documents."""
)

async def generate_mom(transcript: List[Dict]) -> str:
    if not transcript:
        return "No transcript available."

    conversation_text = ""
    for entry in transcript:
        role = "Agent (Sarah)" if entry["speaker"] == "agent" else "Customer"
        conversation_text += f"{role}: {entry['text']}\n"

    prompt = f"""
Based on the following conversation transcript, create a structured Minutes of Meeting (MoM) document.

TRANSCRIPT:
{conversation_text}

OUTPUT FORMAT (Markdown):
# Minutes of Meeting - Barbie Builders

## 1. Call Summary
(A brief 2-3 sentence summary of the call)

## 2. Customer Details
- **Intent:** (Buying/Selling/Inquiry)
- **Key Interests:** (Location, Budget, Type)

## 3. Key Discussion Points
- (Bulleted list of main topics discussed)

## 4. Action Items / Next Steps
- (What needs to be done next)

## 5. Sentiment Analysis
(Positive/Neutral/Negative)
"""
    try:
        response = await asyncio.to_thread(
            mom_model.generate_content,
            prompt,
            generation_config=genai.GenerationConfig(temperature=0.3)
        )
        return response.text
    except Exception as e:
        return f"❌ Failed to generate MoM: {e}"
