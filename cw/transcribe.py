"""Word timestamps for a clip's audio (faster-whisper, on-device, model from the local cache).

Runs in its own process: `python -m cw.transcribe audio.wav [model]` → JSON on stdout.
Kept separate from the editor because faster-whisper's bundled ffmpeg clashes with other libraries.
"""
import json
import sys

HALLUCINATIONS = {"thank you.", "thanks for watching!", "thank you for watching.", "you", "bye.", "subscribe"}


def main():
    wav = sys.argv[1]
    model_name = sys.argv[2] if len(sys.argv) > 2 else "small"
    from faster_whisper import WhisperModel

    import wave

    import numpy as np

    # decode the 16 kHz mono wav ourselves (faster-whisper's PyAV path breaks on newer PyAV)
    with wave.open(wav, "rb") as w:
        audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    try:
        model = WhisperModel(model_name, device="cpu", compute_type="int8", cpu_threads=4, local_files_only=True)
    except Exception:  # noqa: BLE001 — first run: download it once (~460 MB for "small")
        model = WhisperModel(model_name, device="cpu", compute_type="int8", cpu_threads=4)
    segments, info = model.transcribe(
        audio, language="en", word_timestamps=True, vad_filter=True, beam_size=1,
        condition_on_previous_text=False,
    )
    words = []
    for seg in segments:
        text = seg.text.strip().lower()
        if seg.no_speech_prob > 0.6 and seg.avg_logprob < -0.8:
            continue
        if text in HALLUCINATIONS and seg.avg_logprob < -0.5:
            continue
        for w in seg.words or []:
            if w.probability < 0.2 or not w.word.strip():
                continue
            words.append({"w": w.word.strip(), "s": round(w.start, 3), "e": round(w.end, 3), "p": round(w.probability, 3)})
    print(json.dumps({"lang": info.language, "words": words}))


if __name__ == "__main__":
    main()
