from pathlib import Path
from faster_whisper import WhisperModel
from weasyprint import HTML

import asyncio
import time

import gemini_thinker
from mom_pdf_template import render_mom_html


def save_mom_as_pdf(mom_markdown: str, filename: str, caller_id: str | None = None):
    html_str = render_mom_html(mom_markdown, caller_id=caller_id)
    HTML(string=html_str).write_pdf(filename)


async def transcribe_and_generate_mom():
    # 1) Locate audio
    BASE_DIR = Path(__file__).parent.parent
    audio_path = BASE_DIR / "Recording.mp3"
    
    if not audio_path.is_file():
        print("❌ Audio file not found, aborting.")
        return
    if audio_path.is_file():
        print("Audio file Path:", audio_path)

    # 2) Transcribe
    print("Loading model...")
    model = WhisperModel(
        "base",
        device="cpu",
        compute_type="int8",
    )

    segments, info = model.transcribe(
        str(audio_path),
        beam_size=5,
    )

    full_text = "".join(segment.text for segment in segments)

    print(f"Detected language: {info.language} (prob={info.language_probability:.3f})")
    # print("\n--- TRANSCRIPT ---")
    # print(full_text)

    # 3) Build transcript structure expected by generate_mom()
    # gemini_thinker.generate_mom expects: List[Dict] with keys speaker, text, timestamp.[file:31]
    transcript_for_mom = [
        {
            "speaker": "user",           # will be treated as "Customer"
            "text": full_text,
            "timestamp": time.time(),
        }
    ]

    # 4) Call async MoM generator correctly
    # print("GENERATING MOM I GUESSS HAHA")
    mom_content = await gemini_thinker.generate_mom(transcript_for_mom)

    # 5) Save PDF
    pdf_filename = str(BASE_DIR / "MoM.pdf")
    save_mom_as_pdf(mom_content, pdf_filename, caller_id="-1")
    print(f"✅ MoM saved to: {pdf_filename}")


if __name__ == "__main__":
    asyncio.run(transcribe_and_generate_mom())

