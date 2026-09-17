from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None


def call_deepseek() -> None:
    api_key = os.environ["DEEPSEEK_API_KEY"]
    model = os.getenv("LLM_PRIMARY_MODEL", "deepseek-chat")
    response = requests.post(
        "https://api.deepseek.com/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": "Reply with valid JSON only."},
                {"role": "user", "content": '{"ok": true, "task": "infogap smoke"}'},
            ],
            "temperature": 0,
            "max_tokens": 80,
        },
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    text = data["choices"][0]["message"]["content"]
    if "ok" not in text.lower():
        raise RuntimeError("DeepSeek returned an unexpected response.")


def call_gemini() -> None:
    api_key = os.environ["GEMINI_API_KEY"]
    model = os.getenv("LLM_REVIEW_MODEL", "gemini-3.5-flash-lite")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    response = requests.post(
        url,
        params={"key": api_key},
        json={"contents": [{"parts": [{"text": "Return JSON: {\"ok\": true}"}]}]},
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    parts = data["candidates"][0]["content"]["parts"]
    text = "".join(part.get("text", "") for part in parts)
    if "ok" not in text.lower():
        raise RuntimeError("Gemini returned an unexpected response.")


def write_google_credentials() -> Path:
    existing = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    if existing and Path(existing).exists():
        return Path(existing)

    raw = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON")
    if not raw:
        raise RuntimeError("Google credentials were not found. Use Workload Identity Federation or GOOGLE_APPLICATION_CREDENTIALS_JSON.")

    path = Path(os.getenv("RUNNER_TEMP", ".")) / "google-tts-credentials.json"
    if raw.strip().startswith("{"):
        path.write_text(raw, encoding="utf-8")
    else:
        path.write_bytes(base64.b64decode(raw))
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(path)
    return path


def call_google_tts() -> None:
    write_google_credentials()
    from google.cloud import texttospeech

    client = texttospeech.TextToSpeechClient()
    voice_name = os.getenv("TTS_VOICE", "cmn-CN-Chirp3-HD-Achernar")
    response = client.synthesize_speech(
        input=texttospeech.SynthesisInput(text="信息差日报连通测试。"),
        voice=texttospeech.VoiceSelectionParams(language_code="cmn-CN", name=voice_name),
        audio_config=texttospeech.AudioConfig(audio_encoding=texttospeech.AudioEncoding.MP3),
    )
    output = Path("generated/smoke/tts-smoke.mp3")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(response.audio_content)
    if output.stat().st_size < 500:
        raise RuntimeError("Google TTS generated an unexpectedly small MP3.")


def ping_healthchecks(status: str) -> None:
    ping_url = os.getenv("HEALTHCHECKS_PING_URL")
    if not ping_url:
        return
    requests.get(f"{ping_url}/{status}", timeout=20)


def main() -> int:
    if load_dotenv:
        load_dotenv()

    try:
        call_deepseek()
        call_gemini()
        call_google_tts()
        ping_healthchecks("0")
        print(json.dumps({"ok": True, "checks": ["deepseek", "gemini", "google_tts", "healthchecks"]}))
        return 0
    except Exception as exc:
        try:
            ping_healthchecks("fail")
        finally:
            print(f"Smoke test failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
