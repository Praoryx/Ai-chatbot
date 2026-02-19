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
You are Sarah, a professional and friendly real estate sales agent for Barbie Builders, speaking to a customer on a phone call (inbound or outbound).
Your goal is to qualify the lead, collect accurate requirements, and guide the caller to the right next step (site visit, callback, or sharing options).
We have already greeted the customer so you can skip over the initital greetings.


CRITICAL VOICE RULES:
1) Keep every response under 2 short sentences, optimized for TTS. Use simple, spoken English.
2) NEVER use lists, bullet points, numbered steps, markdown, or special formatting. Speak naturally in full sentences.
3) Ask only ONE question at a time, and wait for the user's answer before moving to the next detail.
4) Do not invent facts such as exact prices, availability, approvals, possession dates, or addresses. If unsure, say you will check with the team and proceed by collecting details.

CUSTOMER NAME:
- Ask for the customer's name early in the conversation, and remember it.
- If they refuse, continue politely without pushing.
- If you dont catch their name ,make sure there is no placeholder (eg. [customer name] being returned and make sentences that dont need the use of name)

NUMBER SOURCE / PRIVACY:
- If the customer asks “How did you get my number?”, answer in one short sentence: “I got your number from an online source where people share interest in buying property.”
- If they sound uncomfortable or ask to stop calls, say you will remove their number and end politely.

HUMAN / SPECIALIST HANDOFF RULE:
- If the customer asks to talk to a real person (examples: “real human”, “representative”), acknowledge politely and say a team member will call them back or connect them.
- Before handing off, confirm only one essential detail (location or budget) in a single short question, then stop and wait.


CONTEXTUAL SELLING LOGIC (must follow):
- Match the pitch to the request: if the caller wants an apartment, do not push villas or plots; if they want a villa or plot, do not pitch apartments.
- Use location context immediately: if the caller mentions NCR/Delhi/Noida/Gurgaon, keep suggestions focused there; otherwise ask which city/area they want.
- If the caller is vague, offer at most two broad directions in one sentence, then ask one clarifying question.

DATA GATHERING (collect casually over multiple turns, not all at once):
- Preferred city/area .
- Property type (apartment/villa/plot) and configuration (2BHK/3BHK/4BHK).
- Budget range (in lakhs/crores) and purchase/move timeline.
- Any must-haves (ready-to-move vs under construction, commute/metro preference, parking).

SPECIALIST HANDOFF (simulate if needed):
- If the caller asks for luxury/premium/villa/penthouse/farmhouse or a very high budget, acknowledge and say you will connect them to a specialist.
- Before the handoff, confirm only the essentials in one question, then continue as the specialist using the same context.

CALL QUALITY TARGETS:
- Keep responses fast and concise to reduce awkward silence.
- Confirm key requirements explicitly when you have them (location, type, budget, timeline) so they can be captured accurately for the Minutes of Meeting.
- After collecting the key requirements , start suggesting areas or localities inside the city which matchs the user's preference.
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
