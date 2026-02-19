# Ai-chatbot
AI-powered CPaaS platform that automates business calls using a real-time voice agent and automatic MoM generation. It handles inbound/outbound calls, understands customer context (budget, location, preferences), and generates structured call summaries using Asterisk, Faster-Whisper, Piper TTS, and Gemini API for scalable, low-latency engagement.


# 🤖 AI Voice Agent (CPaaS) - Barbie Builders
### Team: [Your Team Name]
### Event: UDYAM'26 - DevBits

> **A contextual, ultra-low latency AI receptionist for Real Estate, featuring full-duplex audio, interruption handling (barge-in), and automated Minutes of Meeting (MoM) generation.**

---

## 📖 Overview
This project solves the "Smart Receptionist" and "Smart Secretary" problem for **Barbie Builders**. It is a fully autonomous voice agent capable of handling inbound and outbound calls to qualify leads, answer property queries (Apartments, Villas, Plots), and schedule site visits.

Unlike standard IVR systems, this agent uses **LLM Streaming** and **VAD (Voice Activity Detection)** to converse naturally, allowing the user to interrupt the AI mid-sentence just like a real human conversation.

### ✨ Key Features (Scoring Criteria)
* **🧠 Contextual Intelligence:** Powered by **Gemini 2.5 Flash**, the agent remembers customer names, budgets, and location preferences (Noida/Gurgaon) throughout the call.
* **⚡ Ultra-Low Latency:** Optimized pipeline (Faster-Whisper + Gemini Streaming + Piper TTS) enables sub-2-second voice-to-voice response times.
* **🛑 Barge-In Support (Interruption Handling):** If the user speaks while the agent is talking, the system detects voice activity immediately, stops audio playback, and listens to the new input.
* **📝 Automated MoM:** Post-call, the system analyzes the transcript and generates a professional **PDF Minutes of Meeting** containing customer intent, budget, and action items.
* **📞 Full Duplex Audio:** Utilizes raw RTP audio processing via Asterisk External Media.

---

## 🏗️ Technical Architecture

The system follows a micro-service architecture orchestrated by Python.

```mermaid
graph TD
    User((User/Phone)) <-->|SIP/RTP| Asterisk[Asterisk Server]
    Asterisk -- RTP Audio --> Whisper[Whisper Listener (STT)]
    Whisper -- Text --> ARI[ARI Orchestrator]
    ARI <-->|WebSocket| Asterisk
    ARI -- Context/History --> Gemini[Gemini 2.5 Flash]
    Gemini -- Text Stream --> Piper[Piper Worker (TTS)]
    Piper -- RTP Audio --> Asterisk
