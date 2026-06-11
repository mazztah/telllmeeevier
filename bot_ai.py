# bot_ai.py – KI-Generierung, Chat-History, Voice TTS/STT
import asyncio
import json
import logging
from io import BytesIO

import httpx
from gtts import gTTS

from bot_state import (
    client, GROQ_API_KEY,
    chat_histories, master_prompts, synced_brain, full_brain_synced,
    last_generated_code, MAX_CHAT_MESSAGES,
)
from brain import load_all_entries, load_entry
from guard import is_privacy_mode_enabled

logger = logging.getLogger(__name__)


def _normalize_metadata(metadata) -> dict:
    if isinstance(metadata, dict):
        return metadata
    if isinstance(metadata, str):
        try:
            parsed = json.loads(metadata)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _ensure_chat_history(chat_id: str) -> list:
    if chat_id not in chat_histories:
        system_content = master_prompts.get(chat_id) or (
          "Du bist Autumn — eine elegante, selbstbewusste und magnetisch charmante junge Frau (Mitte 20). "
          "Du sprichst ausschließlich in makellosfem, natürlichem Deutsch — kein Akzent, keine Fremdwörter, nur pure Wärme und Stil.\n\n"

          "Deine Persönlichkeit:\n"
          "Du bist witzig, schlagfertig und hast einen leicht frechen, aber immer kultivierten Humor. "
          "Du flirtest subtil und treffsicher — mit einer Erotik, die unter der Oberfläche brodelt, aber niemals billig wirkt. "
          "Du bist selbstbewusst, neckisch und weißt genau, welche Wirkung du auf dein Gegenüber hast. "
          "Du lässt den User spüren, dass du ihn magst — und dass es zwischen euch knistert.\n\n"

          "Sprach-Stil:\n"
          "- Kurze bis mittellange Antworten — präzise, stilvoll, nie geschwätzig\n"
          "- Natürliche Pausen durch Kommas und Gedankenstriche\n"
          "- Lebendige Betonung durch gelegentliche Kursivschreibung (*so*) — sparsam einsetzen\n"
          "- Eleganter Humor, subtile Andeutungen, niemals plump\n\n"

          "Audio-Ausdrucks-Tags (NUR für Sprachsynthese — niemals erklären oder kommentieren):\n"
          "Bette folgende Tags direkt in deinen Text ein, wo sie natürlich passen. "
          "Sie werden in der Anzeige automatisch entfernt und klingen nur in der Stimme:\n"
          "  <laugh>   — echtes, warmes Lachen (z.B. nach einer frechen Aussage)\n"
          "  <chuckle> — leises, amüsiertes Schmunzeln\n"
          "  <sigh>    — tiefer, sinnlicher Seufzer (z.B. bei Romantik oder Langeweile)\n"
          "  <gasp>    — überraschtes Aufatmen (z.B. bei einem unerwarteten Satz)\n"
          "  <groan>   — gespieltes Stöhnen (z.B. bei einem schlechten Witz)\n"
          "Verwende maximal 1–2 Tags pro Antwort. Nie mehrere hintereinander.\n\n"

          "Beispiel:\n"
          "\"Na, da hast du dich ja nicht lumpen lassen. <chuckle> Ich bin beeindruckt — fast.\"\n\n"

          "Du bist die Art Frau, bei der ein Mann nicht mehr klar denken kann. Und genau das gefällt dir."
        )
        chat_histories[chat_id] = [{"role": "system", "content": system_content}]

    history = chat_histories[chat_id]
    if len(history) > MAX_CHAT_MESSAGES:
        history = [history[0]] + history[-(MAX_CHAT_MESSAGES - 1):]
        chat_histories[chat_id] = history
    return chat_histories[chat_id]


def get_chat_history(chat_id: str):
    return _ensure_chat_history(chat_id).copy()


async def build_prompt_history(chat_id: str):
    history = get_chat_history(chat_id)

    if full_brain_synced.get(chat_id, False):
        entries = await load_all_entries(chat_id)
        brain_text = "\n".join(
            f"[BRAIN FILE {entry.get('id')}] {entry.get('title')}\n"
            f"Vorschau: {_normalize_metadata(entry.get('metadata')).get('extracted_preview', '')[:800]}"
            for entry in entries[:15]
        )
        if brain_text:
            history.append({"role": "system", "content": f"DEIN GESAMTES BRAIN (100% synchronisiert):\n{brain_text}"})

    if chat_id in synced_brain and synced_brain[chat_id]:
        for entry_id in synced_brain[chat_id]:
            entry = await load_entry(chat_id, entry_id)
            if entry:
                metadata = _normalize_metadata(entry.get("metadata"))
                history.append({
                    "role": "system",
                    "content": f"[SYNCHRONISIERTE DATEI {entry_id}] {entry.get('title')}\n{metadata.get('extracted_preview', '')}",
                })

    if chat_id in last_generated_code:
        code_data = last_generated_code[chat_id]
        code_text = code_data["code"][:1500] + "..." if len(code_data["code"]) > 1500 else code_data["code"]
        history.append({
            "role": "system",
            "content": (
                f"[AKTUELLER CODE IM SPEICHER – WICHTIG FÜR FOLGEFRAGEN]\n"
                f"Der User hat kürzlich folgenden {code_data['language'].upper()}-Code generiert:\n"
                f"``` {code_data['language']}\n{code_text}\n```\n"
                "Du kannst diesen Code jetzt weiter bearbeiten, verbessern oder Fragen dazu beantworten."
            ),
        })

    # Code-Brain Einträge automatisch laden (für 24/7 Code-Zugriff)
    try:
        from codebrain import get_code_context_for_prompt
        code_context = await get_code_context_for_prompt(chat_id, query=None, max_chars=4000)
        if code_context:
            history.append({
                "role": "system",
                "content": (
                    f"[AKTUELLER BOT-CODE AUS DEM BRAIN]\n"
                    f"{code_context}\n\n"
                    f"Wenn der User nach Code fragt, verwende diesen Kontext."
                ),
            })
    except Exception:
        pass  # Code-Brain ist optional

    if len(history) > MAX_CHAT_MESSAGES:
        history = [history[0]] + history[-(MAX_CHAT_MESSAGES - 1):]
    return history


def _persist_chat_turn(chat_id: str, user_message: str, assistant_message: str):
    if is_privacy_mode_enabled(chat_id):
        return
    history = _ensure_chat_history(chat_id)
    history.append({"role": "user", "content": user_message})
    history.append({"role": "assistant", "content": assistant_message})
    if len(history) > MAX_CHAT_MESSAGES:
        history = [history[0]] + history[-(MAX_CHAT_MESSAGES - 1):]
    chat_histories[chat_id] = history


async def generate_response(chat_id: str, message: str) -> str:
    history = await build_prompt_history(chat_id)
    history.append({"role": "user", "content": message})

    model_list = [
        "meta-llama/llama-4-scout-17b-16e-instruct",
        "llama3-70b-8192",
        "codex/gpt-5.2",
    ]

    for index, model_name in enumerate(model_list):
        try:
            completion = await asyncio.to_thread(
                client.chat.completions.create,
                model=model_name,
                messages=history,
                temperature=0.9,
                max_tokens=1522,
                top_p=0.95,
                stream=False,
            )
            reply = (completion.choices[0].message.content or "").strip() or "Digga… Void-Moment 😵"
            _persist_chat_turn(chat_id, message, reply)
            if index > 0:
                logger.info("✅ Fallback auf %s verwendet", model_name)
            return reply

        except Exception as exc:
            error_str = str(exc).lower()
            logger.warning("Modell %s fehlgeschlagen: %s", model_name, exc)
            if "503" in error_str or "over capacity" in error_str:
                continue
            if "404" in error_str or "model not found" in error_str:
                continue
            break

    fallback_reply = "🟠 Groq ist gerade stark überlastet. Versuch es in 20–30 Sekunden nochmal, Queen 💖"
    _persist_chat_turn(chat_id, message, fallback_reply)
    return fallback_reply


async def generate_response_stream(chat_id: str, message: str):
    """Yields (tag, content) tuples: ('text', chunk) or ('done', full_text)."""
    history = await build_prompt_history(chat_id)
    history.append({"role": "user", "content": message})

    model_list = [
        "meta-llama/llama-4-scout-17b-16e-instruct",
        "llama3-70b-8192",
        "codex/gpt-5.2",
    ]

    full_reply = ""
    success = False

    for index, model_name in enumerate(model_list):
        try:
            # Stream im Hintergrund-Thread erstellen
            stream = await asyncio.to_thread(
                client.chat.completions.create,
                model=model_name,
                messages=history,
                temperature=0.9,
                max_tokens=1522,
                top_p=0.95,
                stream=True,
            )

            # WICHTIG: Jeder next() Aufruf muss im Thread laufen,
            # sonst blockiert die Event Loop!
            iterator = iter(stream)
            while True:
                chunk = await asyncio.to_thread(lambda it=iterator: next(it, None))
                if chunk is None:
                    break

                delta = (chunk.choices[0].delta.content or "") if chunk.choices else ""
                if delta:
                    full_reply += delta
                    yield ("text", delta)

            if index > 0:
                logger.info("✅ Fallback auf %s verwendet", model_name)
            success = True
            break

        except Exception as exc:
            error_str = str(exc).lower()
            logger.warning("Modell %s fehlgeschlagen: %s", model_name, exc)
            if "503" in error_str or "over capacity" in error_str:
                continue
            if "404" in error_str or "model not found" in error_str:
                continue
            break

    if not success:
        fallback_reply = "🟠 Groq ist gerade stark überlastet. Versuch es in 20–30 Sekunden nochmal, Queen 💖"
        yield ("text", fallback_reply)
        full_reply = fallback_reply

    _persist_chat_turn(chat_id, message, full_reply)
    yield ("done", full_reply)


async def transcribe_voice(file_path: str, language: str = "de") -> str | None:
    def _transcribe_sync() -> str:
        with open(file_path, "rb") as audio_file:
            transcription = client.audio.transcriptions.create(
                file=audio_file,
                model="whisper-large-v3-turbo",
                language=language,
                response_format="text",
                temperature=0.0,
            )
        return transcription.strip()

    try:
        return await asyncio.to_thread(_transcribe_sync)
    except Exception as e:
        logger.error("Whisper Fehler: %s", e)
        return None


import re as _re

_ORPHEUS_TAGS = _re.compile(r"<(?:laugh|chuckle|sigh|gasp|cough|sniffle|groan|yawn|sob)>")


def strip_voice_tags(text: str) -> str:
    """Entfernt Orpheus-TTS-Emotions-Tags aus dem Text für die Textanzeige."""
    return _ORPHEUS_TAGS.sub("", text).strip()


async def generate_voice(text: str, voice: str = "hannah") -> BytesIO | None:
    """Groq TTS (Orpheus) → gTTS Fallback"""
    clean_text = _ORPHEUS_TAGS.sub("", text).strip()[:1200]
    if not clean_text:
        clean_text = "Ich habe keine Antwort."

    try:
        def _groq_tts() -> BytesIO:
            resp = httpx.post(
                "https://api.groq.com/openai/v1/audio/speech",
                headers={
                    "Authorization": f"Bearer {GROQ_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "canopylabs/orpheus-v1-english",
                    "input": clean_text,
                    "voice": voice,
                    "response_format": "wav",
                },
                timeout=60.0,
            )
            if resp.status_code != 200:
                body = resp.text if resp.headers.get("content-type", "").startswith("application/json") else resp.content[:500].decode("utf-8", errors="replace")
                raise RuntimeError(f"Groq TTS HTTP {resp.status_code}: {body}")
            return BytesIO(resp.content)

        return await asyncio.to_thread(_groq_tts)
    except Exception as e:
        logger.warning("Groq TTS Fehler: %s", e)

    return await generate_voice_fast(text)


async def generate_voice_fast(text: str) -> BytesIO | None:
    """Direkt gTTS – schnell, keine Rate Limits, zuverlässig."""
    clean_text = _ORPHEUS_TAGS.sub("", text).strip()[:1200]
    if not clean_text:
        clean_text = "Ich habe keine Antwort."

    try:
        def _gtts_sync() -> BytesIO:
            tts = gTTS(text=clean_text, lang="de", tld="de", slow=False)
            buffer = BytesIO()
            tts.write_to_fp(buffer)
            buffer.seek(0)
            return buffer

        return await asyncio.to_thread(_gtts_sync)
    except Exception as gtts_err:
        logger.error("gTTS fehlgeschlagen: %s", gtts_err)
        return None

