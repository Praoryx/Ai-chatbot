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

graph TD
    User((User/Phone)) <-->|SIP/RTP| Asterisk[Asterisk Server]
    
    subgraph "AI Voice Pipeline"
        Asterisk -- "Raw RTP (ulaw)" --> Whisper[Whisper Listener STT]
        Whisper -- "JSON Transcript" --> ARI[ARI Orchestrator]
        ARI <-->|WebSocket/REST| Asterisk
        ARI -- "Prompt + History" --> Gemini[Gemini 2.5 Flash]
        Gemini -- "Text Stream" --> Piper[Piper Worker TTS]
        Piper -- "RTP Audio" --> Asterisk
    end

    subgraph "Post-Call Processing"
        ARI -- "Full Transcript" --> Thinker[Gemini Thinker]
        Thinker -- "Markdown" --> PDF[WeasyPrint PDF Generator]
    end











# 🤖 AI Voice Agent (CPaaS) - Barbie Builders
### Team: [Your Team Name]
### Event: UDYAM'26 - DevBits

> A contextual, low-latency AI receptionist and sales agent for Real Estate, capable of handling interruptions and generating Minutes of Meeting (MoM).

---

## 📖 Overview
This project is an **AI-Powered Customer Engagement Platform** designed to automate Inbound and Outbound calls for a real estate company ("Barbie Builders"). The system acts as a Smart Receptionist that qualifies leads, answers queries about properties (Apartments, Villas, Plots), and automatically generates a PDF summary of the conversation.

### ✨ Key Features
* [cite_start]**Contextual Intelligence:** Powered by **Gemini 2.5 Flash**, the agent understands location context (Noida/Gurgaon) and user intent[cite: 31, 43].
* [cite_start]**Ultra-Low Latency:** Uses streaming responses (Piper TTS + Gemini Streaming) to achieve conversational speeds < 2 seconds[cite: 121].
* [cite_start]**Barge-In Support:** The agent stops speaking immediately if the user interrupts, mimicking natural human conversation.
* [cite_start]**Automated MoM:** Generates a structured **Minutes of Meeting PDF** containing customer details, budget, and action items immediately after the call[cite: 50, 131].
* **Full Duplex Audio:** Utilizes Asterisk External Media (RTP) for raw audio processing.

---

## 🏗️ Architecture

**Data Flow:**
1.  **Telephony:** Asterisk handles the SIP call.
2.  **Audio Stream:** Audio is piped via RTP (External Media) to `whisper_listener.py`.
3.  **STT:** **Faster-Whisper** with **Silero VAD** transcribes speech and detects interruptions.
4.  **Brain:** The transcript is sent to **Gemini 2.5 Flash**, which streams a text response.
5.  **TTS:** **Piper** (running locally) converts the text stream to audio and sends RTP packets back to Asterisk.
6.  **Orchestrator:** `ari.py` manages the call state via WebSocket.

---

## 🛠️ Tech Stack
| Component | Technology | Description |
| :--- | :--- | :--- |
| **LLM** | Google Gemini 2.5 Flash | Logic, conversation, and MoM generation |
| **Telephony** | Asterisk (v18+) | SIP Server & ARI (Asterisk REST Interface) |
| **STT** | Faster-Whisper | Low-latency speech-to-text |
| **TTS** | Piper | Neural text-to-speech (running specifically `en_US-amy-medium`) |
| **VAD** | Silero VAD | Voice Activity Detection for Barge-in |
| **PDF Engine** | WeasyPrint | HTML to PDF conversion for MoM |
| **Language** | Python 3.10+ | Core application logic |

---

## 🚀 Setup Instructions

### 1. Prerequisites
* Python 3.10+ installed.
* **Asterisk** installed and running.
* **Piper** binary downloaded and placed in `./piper/piper`.
* Piper Model (`en_US-amy-medium.onnx`) placed in `./models/`.

### 2. Installation
Clone the repository and install dependencies:
```bash
git clone <your-repo-url>
cd <your-repo-folder>
pip install -r requirements.txt
