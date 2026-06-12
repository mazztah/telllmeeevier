#!/usr/bin/env python3
# main.py – Telllmeeedrei_BOT | KORRIGIERT: Stabile Version v3.0.2
# ═══════════════════════════════════════════════════════════════════════════════

import os
import sys
import json
import logging
import asyncio
import tempfile
import base64
import httpx
from pathlib import Path

# ── HTTPX Global Connection Limits (verhindert socket exhaustion auf HF Free Tier) ──
HTTPX_LIMITS = httpx.Limits(
    max_keepalive_connections=20,
    max_connections=100,
)
HTTPX_TIMEOUT = httpx.Timeout(
    connect=5.0,
    read=15.0,
    write=10.0,
    pool=10.0,
)
from datetime import datetime
from contextlib import asynccontextmanager
from typing import Optional, Dict, Any

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import NetworkError as TgNetworkError, TimedOut as TgTimedOut
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

import groq

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_ANON_KEY")
USE_WEBHOOK = os.getenv("USE_WEBHOOK", "false").lower() in ("true", "1", "yes")
WEBHOOK_URL = (os.getenv("WEBHOOK_URL") or os.getenv("RENDER_EXTERNAL_URL", "")).rstrip("/")
PORT = int(os.getenv("PORT", "7860"))
HOST = os.getenv("HOST", "0.0.0.0")
OWNER_CHAT_ID = os.getenv("OWNER_CHAT_ID")

OFFSET_FILE = Path("last_update_offset.txt")

def _load_offset() -> int:
    try:
        if OFFSET_FILE.exists():
            return int(OFFSET_FILE.read_text().strip())
    except Exception as e:
        logger.warning("Offset-Laden fehlgeschlagen: %s", e)
    return 0

def _save_offset(offset: int) -> None:
    try:
        OFFSET_FILE.write_text(str(offset))
    except Exception as e:
        logger.warning("Offset-Speicherung fehlgeschlagen: %s", e)

_groq_client: Optional[groq.AsyncGroq] = None

def get_groq_client() -> groq.AsyncGroq:
    global _groq_client
    if _groq_client is None:
        if not GROQ_API_KEY:
            raise RuntimeError("GROQ_API_KEY fehlt!")
        _groq_client = groq.AsyncGroq(api_key=GROQ_API_KEY)
        logger.info("Groq-Client initialisiert")
    return _groq_client

_telegram_app: Optional[Application] = None
_init_lock = asyncio.Lock()
_polling_task = None
_watchdog_task = None

# ═══════════════════════════════════════════════════════════════════════════════
# MINI-APP IMPORTS
# ═══════════════════════════════════════════════════════════════════════════════

try:
    from voice_mini_app import app as voice_mini_app
except ImportError as e:
    logger.warning(f"voice_mini_app nicht verfuegbar: {e}")
    voice_mini_app = None

try:
    from scanner_mini_app import app as scanner_mini_app
except ImportError as e:
    logger.warning(f"scanner_mini_app nicht verfuegbar: {e}")
    scanner_mini_app = None

try:
    from lightmeter_mini_app import app as lightmeter_mini_app
except ImportError as e:
    logger.warning(f"lightmeter_mini_app nicht verfuegbar: {e}")
    lightmeter_mini_app = None

try:
    from archive_mini_app import app as archive_mini_app
except ImportError as e:
    logger.warning(f"archive_mini_app nicht verfuegbar: {e}")
    archive_mini_app = None

try:
    from papersearch_mini_app import app as papersearch_mini_app
except ImportError as e:
    logger.warning(f"papersearch_mini_app nicht verfuegbar: {e}")
    papersearch_mini_app = None

try:
    from dragon_mini_app import app as dragon_mini_app
except ImportError as e:
    logger.warning(f"dragon_mini_app nicht verfuegbar: {e}")
    dragon_mini_app = None

try:
    from space_war_mini_app import app as spacewar_mini_app
except ImportError as e:
    logger.warning(f"space_war_mini_app nicht verfuegbar: {e}")
    spacewar_mini_app = None

try:
    from chess_mini_app import app as chess_app
except ImportError as e:
    logger.warning(f"chess_mini_app nicht verfuegbar: {e}")
    chess_app = None

try:
    from sandbox_mini_app import app as sandbox_mini_app
except ImportError as e:
    logger.warning(f"sandbox_mini_app nicht verfuegbar: {e}")
    sandbox_mini_app = None

try:
    from trichome_mini_app import app as trichome_mini_app
except ImportError as e:
    logger.warning(f"trichome_mini_app nicht verfuegbar: {e}")
    trichome_mini_app = None

try:
    from plantid_mini_app import app as plantid_mini_app
except ImportError as e:
    logger.warning(f"plantid_mini_app nicht verfuegbar: {e}")
    plantid_mini_app = None

try:
    from shellgame_mini_app import app as shellgame_mini_app
except ImportError as e:
    logger.warning(f"shellgame_mini_app nicht verfuegbar: {e}")
    shellgame_mini_app = None

try:
    from diagnose_app import app as diagnose_app
except ImportError as e:
    logger.warning(f"diagnose_app nicht verfuegbar: {e}")
    diagnose_app = None

# ═══════════════════════════════════════════════════════════════════════════════
# HANDLER IMPORTS
# ═══════════════════════════════════════════════════════════════════════════════

try:
    from bot_state import (
        application as telegram_application,
        OWNER_CHAT_ID as BOT_OWNER_ID,
        safe_send_message,
    )
except ImportError as e:
    logger.warning(f"bot_state nicht verfuegbar: {e}")
    telegram_application = None
    BOT_OWNER_ID = OWNER_CHAT_ID
    async def safe_send_message(*args, **kwargs):
        return None

try:
    from handlers_cmd import (
        start, handle_upload, handle_edit_command, handle_vision_command,
        handle_vision_stop, toggle_voice_response, handle_imagine, handle_textvideo,
        handle_stop_video, handle_code, handle_yt_pdf_callback, handle_email_callback,
        handle_audit_callback, cmd_synchromaster, cmd_synchroall, cmd_synchdata,
        cmd_convert, cmd_textconvert, cmd_yt, cmd_testbrain, cmd_chat, cmd_listbrain,
        cmd_agent, cmd_workflow, cmd_social, cmd_brainindex, cmd_semantic,
        cmd_privacy, cmd_guard_status, cmd_audit, cmd_gmail_auth, cmd_gmail_code,
        cmd_mailbatch, cmd_voiceclone, cmd_myvoices, cmd_deletevoice, cmd_speak,
        cmd_robot, cmd_deepvoice, cmd_chipmunk, cmd_demon, cmd_telephone, cmd_echo,
        cmd_stopdistort, cmd_alien, cmd_underwater, cmd_radio, cmd_megaphone,
        cmd_whisper, cmd_monster, cmd_cyberpunk, cmd_cave, cmd_helium, cmd_reverse,
        cmd_startstream, cmd_endstream, cmd_livevoice, cmd_scanner, cmd_shellgame,
        handle_ttv26, cmd_lyria, cmd_suno, cmd_freebeat, cmd_convert3d, cmd_text_to_3d,
        cmd_readme, cmd_diagnose, cmd_savecode,
    )
except ImportError as e:
    logger.warning(f"handlers_cmd nicht verfuegbar: {e}")
    def start(*args, **kwargs): pass
    def handle_upload(*args, **kwargs): pass
    def handle_edit_command(*args, **kwargs): pass
    def handle_vision_command(*args, **kwargs): pass
    def handle_vision_stop(*args, **kwargs): pass
    def toggle_voice_response(*args, **kwargs): pass
    def handle_imagine(*args, **kwargs): pass
    def handle_textvideo(*args, **kwargs): pass
    def handle_stop_video(*args, **kwargs): pass
    def handle_code(*args, **kwargs): pass
    def handle_yt_pdf_callback(*args, **kwargs): pass
    def handle_email_callback(*args, **kwargs): pass
    def handle_audit_callback(*args, **kwargs): pass
    def cmd_synchromaster(*args, **kwargs): pass
    def cmd_synchroall(*args, **kwargs): pass
    def cmd_synchdata(*args, **kwargs): pass
    def cmd_convert(*args, **kwargs): pass
    def cmd_textconvert(*args, **kwargs): pass
    def cmd_yt(*args, **kwargs): pass
    def cmd_testbrain(*args, **kwargs): pass
    def cmd_chat(*args, **kwargs): pass
    def cmd_listbrain(*args, **kwargs): pass
    def cmd_agent(*args, **kwargs): pass
    def cmd_workflow(*args, **kwargs): pass
    def cmd_social(*args, **kwargs): pass
    def cmd_brainindex(*args, **kwargs): pass
    def cmd_semantic(*args, **kwargs): pass
    def cmd_privacy(*args, **kwargs): pass
    def cmd_guard_status(*args, **kwargs): pass
    def cmd_audit(*args, **kwargs): pass
    def cmd_gmail_auth(*args, **kwargs): pass
    def cmd_gmail_code(*args, **kwargs): pass
    def cmd_mailbatch(*args, **kwargs): pass
    def cmd_voiceclone(*args, **kwargs): pass
    def cmd_myvoices(*args, **kwargs): pass
    def cmd_deletevoice(*args, **kwargs): pass
    def cmd_speak(*args, **kwargs): pass
    def cmd_robot(*args, **kwargs): pass
    def cmd_deepvoice(*args, **kwargs): pass
    def cmd_chipmunk(*args, **kwargs): pass
    def cmd_demon(*args, **kwargs): pass
    def cmd_telephone(*args, **kwargs): pass
    def cmd_echo(*args, **kwargs): pass
    def cmd_stopdistort(*args, **kwargs): pass
    def cmd_alien(*args, **kwargs): pass
    def cmd_underwater(*args, **kwargs): pass
    def cmd_radio(*args, **kwargs): pass
    def cmd_megaphone(*args, **kwargs): pass
    def cmd_whisper(*args, **kwargs): pass
    def cmd_monster(*args, **kwargs): pass
    def cmd_cyberpunk(*args, **kwargs): pass
    def cmd_cave(*args, **kwargs): pass
    def cmd_helium(*args, **kwargs): pass
    def cmd_reverse(*args, **kwargs): pass
    def cmd_startstream(*args, **kwargs): pass
    def cmd_endstream(*args, **kwargs): pass
    def cmd_livevoice(*args, **kwargs): pass
    def cmd_scanner(*args, **kwargs): pass
    def cmd_shellgame(*args, **kwargs): pass
    def handle_ttv26(*args, **kwargs): pass
    def cmd_lyria(*args, **kwargs): pass
    def cmd_suno(*args, **kwargs): pass
    def cmd_freebeat(*args, **kwargs): pass
    def cmd_convert3d(*args, **kwargs): pass
    def cmd_text_to_3d(*args, **kwargs): pass
    def cmd_readme(*args, **kwargs): pass
    def cmd_diagnose(*args, **kwargs): pass
    def cmd_savecode(*args, **kwargs): pass

try:
    from trichome_handler import cmd_trichome
    from trichome_analyzer import trichome_callback
except ImportError as e:
    logger.warning(f"trichome_handler nicht verfuegbar: {e}")
    def cmd_trichome(*args, **kwargs): pass
    def trichome_callback(*args, **kwargs): pass

try:
    from plantid_handler import cmd_plantid, plantid_callback
except ImportError as e:
    logger.warning(f"plantid_handler nicht verfuegbar: {e}")
    def cmd_plantid(*args, **kwargs): pass
    def plantid_callback(*args, **kwargs): pass

try:
    from archive_handler import cmd_archive, cmd_archivesearch, cmd_archivedetails, cmd_archivedownload
except ImportError as e:
    logger.warning(f"archive_handler nicht verfuegbar: {e}")
    def cmd_archive(*args, **kwargs): pass
    def cmd_archivesearch(*args, **kwargs): pass
    def cmd_archivedetails(*args, **kwargs): pass
    def cmd_archivedownload(*args, **kwargs): pass

try:
    from papersearch_handler import cmd_papersearch, cmd_psworkspace, cmd_pschat, papersearch_callback
except ImportError as e:
    logger.warning(f"papersearch_handler nicht verfuegbar: {e}")
    def cmd_papersearch(*args, **kwargs): pass
    def cmd_psworkspace(*args, **kwargs): pass
    def cmd_pschat(*args, **kwargs): pass
    def papersearch_callback(*args, **kwargs): pass

try:
    from brain_web_handler import cmd_brainweb, brainweb_callback
except ImportError as e:
    logger.warning(f"brain_web_handler nicht verfuegbar: {e}")
    def cmd_brainweb(*args, **kwargs): pass
    def brainweb_callback(*args, **kwargs): pass

try:
    from dragon_handler import cmd_dragon, dragon_callback
except ImportError as e:
    logger.warning(f"dragon_handler nicht verfuegbar: {e}")
    def cmd_dragon(*args, **kwargs): pass
    def dragon_callback(*args, **kwargs): pass

try:
    from space_war_handler import cmd_spacewar, spacewar_callback
except ImportError as e:
    logger.warning(f"space_war_handler nicht verfuegbar: {e}")
    def cmd_spacewar(*args, **kwargs): pass
    def spacewar_callback(*args, **kwargs): pass

try:
    from chess_handler import cmd_chess, chess_callback
except ImportError as e:
    logger.warning(f"chess_handler nicht verfuegbar: {e}")
    def cmd_chess(*args, **kwargs): pass
    def chess_callback(*args, **kwargs): pass

try:
    from sandbox_handler import cmd_sandbox, cmd_runcode, cmd_codefile, cmd_py, cmd_htmlapp, sandbox_callback
except ImportError as e:
    logger.warning(f"sandbox_handler nicht verfuegbar: {e}")
    def cmd_sandbox(*args, **kwargs): pass
    def cmd_runcode(*args, **kwargs): pass
    def cmd_codefile(*args, **kwargs): pass
    def cmd_py(*args, **kwargs): pass
    def cmd_htmlapp(*args, **kwargs): pass
    def sandbox_callback(*args, **kwargs): pass

try:
    from lightmeter_handler import cmd_lightmeter, lightmeter_callback
except ImportError as e:
    logger.warning(f"lightmeter_handler nicht verfuegbar: {e}")
    def cmd_lightmeter(*args, **kwargs): pass
    def lightmeter_callback(*args, **kwargs): pass

try:
    from superagent import superagent_handler, superagent_callback
except ImportError as e:
    logger.warning(f"superagent nicht verfuegbar: {e}")
    def superagent_handler(*args, **kwargs): pass
    def superagent_callback(*args, **kwargs): pass

try:
    from openclaw import openclaw_handler, openclaw_callback
except ImportError as e:
    logger.warning(f"openclaw nicht verfuegbar: {e}")
    def openclaw_handler(*args, **kwargs): pass
    def openclaw_callback(*args, **kwargs): pass

try:
    from openclaw_cloud import openclaw_cloud_handler, openclaw_cloud_callback
except ImportError as e:
    logger.warning(f"openclaw_cloud nicht verfuegbar: {e}")
    def openclaw_cloud_handler(*args, **kwargs): pass
    def openclaw_cloud_callback(*args, **kwargs): pass

try:
    from claude_code import handle_claude_code
except ImportError as e:
    logger.warning(f"claude_code nicht verfuegbar: {e}")
    def handle_claude_code(*args, **kwargs): pass

try:
    from brainlist_handler import cmd_brainlist as brainlist_cmd, brain_callback as brain_cb
except ImportError as e:
    logger.warning(f"brainlist_handler nicht verfuegbar: {e}")
    def brainlist_cmd(*args, **kwargs): pass
    def brain_cb(*args, **kwargs): pass

try:
    from handlers_media import handle_photo, handle_voice, handle_audio_upload, handle_document, handle_musik, handle_humming
except ImportError as e:
    logger.warning(f"handlers_media nicht verfuegbar: {e}")
    def handle_photo(*args, **kwargs): pass
    def handle_voice(*args, **kwargs): pass
    def handle_audio_upload(*args, **kwargs): pass
    def handle_document(*args, **kwargs): pass
    def handle_musik(*args, **kwargs): pass
    def handle_humming(*args, **kwargs): pass

try:
    from handlers_chat import handle_message
except ImportError as e:
    logger.warning(f"handlers_chat nicht verfuegbar: {e}")
    def handle_message(*args, **kwargs): pass

try:
    from stickerpack import cmd_stickerpack, collect_sticker_for_pack, finish_stickerpack, handle_bg_callback, has_active_sticker_session
except ImportError as e:
    logger.warning(f"stickerpack nicht verfuegbar: {e}")
    def cmd_stickerpack(*args, **kwargs): pass
    def collect_sticker_for_pack(*args, **kwargs): pass
    def finish_stickerpack(*args, **kwargs): pass
    def handle_bg_callback(*args, **kwargs): pass
    def has_active_sticker_session(*args, **kwargs): return False

try:
    from gif_handler import handle_gif_command, collect_gif_for_session, finish_gif_session, cancel_gif_session, has_active_gif_session
except ImportError as e:
    logger.warning(f"gif_handler nicht verfuegbar: {e}")
    def handle_gif_command(*args, **kwargs): pass
    def collect_gif_for_session(*args, **kwargs): pass
    def finish_gif_session(*args, **kwargs): pass
    def cancel_gif_session(*args, **kwargs): pass
    def has_active_gif_session(*args, **kwargs): return False

try:
    from instantmesh import mesh_handler
except ImportError as e:
    logger.warning(f"instantmesh nicht verfuegbar: {e}")
    mesh_handler = None

try:
    from brain import (
        is_enabled as brain_enabled,
        save_chat, save_text, save_file, load_all_entries, load_entry,
        list_entries, delete_entry, set_master_prompt, test_connection as test_brain_connection,
        get_brain_status,
    )
except ImportError as e:
    logger.warning(f"brain nicht verfuegbar: {e}")
    brain_enabled = lambda: False
    async def save_chat(*args, **kwargs): return "Brain nicht verfuegbar"
    async def save_text(*args, **kwargs): return "Brain nicht verfuegbar"
    async def save_file(*args, **kwargs): return "Brain nicht verfuegbar"
    async def load_all_entries(*args, **kwargs): return []
    async def load_entry(*args, **kwargs): return None
    async def list_entries(*args, **kwargs): return "Brain nicht verfuegbar"
    async def delete_entry(*args, **kwargs): return "Brain nicht verfuegbar"
    async def set_master_prompt(*args, **kwargs): return None
    async def test_brain_connection(*args, **kwargs): return "Brain nicht konfiguriert"
    async def get_brain_status(*args, **kwargs): return {}

try:
    from brain_agent import brain_query_agent
except ImportError as e:
    logger.warning(f"brain_agent nicht verfuegbar: {e}")
    async def brain_query_agent(*args, **kwargs): return {"success": False, "answer": "Agent nicht verfuegbar"}

try:
    from super_skill_app import router as superskill_router, cmd_superskill
except Exception as e:
    logger.warning(f"super_skill_app nicht verfuegbar: {e}")
    from fastapi import APIRouter
    superskill_router = APIRouter(prefix="/superskill")
    def cmd_superskill(*args, **kwargs): pass

class _GifSessionFilter(filters.BaseFilter):
    def check_update(self, update):
        if not getattr(update, "message", None):
            return False
        chat_id = str(update.effective_chat.id) if update.effective_chat else None
        return has_active_gif_session(chat_id) if chat_id else False

class _StickerSessionFilter(filters.BaseFilter):
    def check_update(self, update):
        if not getattr(update, "message", None):
            return False
        chat_id = str(update.effective_chat.id) if update.effective_chat else None
        return has_active_sticker_session(chat_id) if chat_id else False

gif_active = _GifSessionFilter()
sticker_active = _StickerSessionFilter()

# ═══════════════════════════════════════════════════════════════════════════════
# TELEGRAM HANDLER REGISTRIERUNG
# ═══════════════════════════════════════════════════════════════════════════════

if telegram_application:
    application = telegram_application
else:
    application = Application.builder().token(TELEGRAM_TOKEN).build() if TELEGRAM_TOKEN else None

if application:
    async def _mesh_unavailable(update, context):
        if getattr(update, "message", None):
            await update.message.reply_text("Mesh nicht verfuegbar")

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("upload", handle_upload))
    application.add_handler(CommandHandler("synchromaster", cmd_synchromaster))
    application.add_handler(CommandHandler("synchroall", cmd_synchroall))
    application.add_handler(CommandHandler("synchdata", cmd_synchdata))
    application.add_handler(CommandHandler("convert", cmd_convert))
    application.add_handler(CommandHandler("textconvert", cmd_textconvert))
    application.add_handler(CommandHandler("yt", cmd_yt))
    application.add_handler(CommandHandler("voicetoggle", toggle_voice_response))
    application.add_handler(CommandHandler("imagine", handle_imagine))
    application.add_handler(CommandHandler("img", handle_imagine))
    application.add_handler(CommandHandler("edit", handle_edit_command))
    application.add_handler(CommandHandler("bearbeiten", handle_edit_command))
    application.add_handler(CommandHandler("vision", handle_vision_command))
    application.add_handler(CommandHandler("analyze", handle_vision_command))
    application.add_handler(CommandHandler("beschreib", handle_vision_command))
    application.add_handler(CommandHandler("visionstop", handle_vision_stop))
    application.add_handler(CommandHandler("musik", handle_musik))
    application.add_handler(CommandHandler("humming", handle_humming))
    application.add_handler(CommandHandler("summen", handle_humming))
    application.add_handler(CommandHandler("testbrain", cmd_testbrain))
    application.add_handler(CommandHandler("chat", cmd_chat))
    application.add_handler(CommandHandler("listbrain", cmd_listbrain))
    application.add_handler(CommandHandler("agent", cmd_agent))
    application.add_handler(CommandHandler("workflow", cmd_workflow))
    application.add_handler(CommandHandler("social", cmd_social))
    application.add_handler(CommandHandler("brainindex", cmd_brainindex))
    application.add_handler(CommandHandler("semantic", cmd_semantic))
    application.add_handler(CommandHandler("privacy", cmd_privacy))
    application.add_handler(CommandHandler("guard", cmd_guard_status))
    application.add_handler(CommandHandler(["audit", "standard"], cmd_audit))
    application.add_handler(CommandHandler("gmail_auth", cmd_gmail_auth))
    application.add_handler(CommandHandler("gmail_code", cmd_gmail_code))
    application.add_handler(CommandHandler("mailbatch", cmd_mailbatch))
    application.add_handler(CommandHandler("emailbatch", cmd_mailbatch))
    application.add_handler(CommandHandler("voiceclone", cmd_voiceclone))
    application.add_handler(CommandHandler("myvoices", cmd_myvoices))
    application.add_handler(CommandHandler("deletevoice", cmd_deletevoice))
    application.add_handler(CommandHandler("speak", cmd_speak))
    application.add_handler(CommandHandler("robot", cmd_robot))
    application.add_handler(CommandHandler("deepvoice", cmd_deepvoice))
    application.add_handler(CommandHandler("chipmunk", cmd_chipmunk))
    application.add_handler(CommandHandler("demon", cmd_demon))
    application.add_handler(CommandHandler("telephone", cmd_telephone))
    application.add_handler(CommandHandler("echo", cmd_echo))
    application.add_handler(CommandHandler("alien", cmd_alien))
    application.add_handler(CommandHandler("underwater", cmd_underwater))
    application.add_handler(CommandHandler("radio", cmd_radio))
    application.add_handler(CommandHandler("megaphone", cmd_megaphone))
    application.add_handler(CommandHandler("whisper", cmd_whisper))
    application.add_handler(CommandHandler("monster", cmd_monster))
    application.add_handler(CommandHandler("cyberpunk", cmd_cyberpunk))
    application.add_handler(CommandHandler("cave", cmd_cave))
    application.add_handler(CommandHandler("helium", cmd_helium))
    application.add_handler(CommandHandler("reverse", cmd_reverse))
    application.add_handler(CommandHandler("stopdistort", cmd_stopdistort))
    application.add_handler(CommandHandler("textvideo", handle_textvideo))
    application.add_handler(CommandHandler("stopvideo", handle_stop_video))
    application.add_handler(CommandHandler("cancel", handle_stop_video))
    application.add_handler(CommandHandler("code", handle_code))
    application.add_handler(CommandHandler("startstream", cmd_startstream))
    application.add_handler(CommandHandler("voicestream", cmd_startstream))
    application.add_handler(CommandHandler("endstream", cmd_endstream))
    application.add_handler(CommandHandler("stopstream", cmd_endstream))
    application.add_handler(CommandHandler("livevoice", cmd_livevoice))
    application.add_handler(CommandHandler(["scan", "qr"], cmd_scanner))
    application.add_handler(CommandHandler("ttv26", handle_ttv26))
    application.add_handler(CommandHandler("lyria", cmd_lyria))
    application.add_handler(CommandHandler("suno", cmd_suno))
    application.add_handler(CommandHandler("freebeat", cmd_freebeat))
    application.add_handler(CommandHandler("superagent", superagent_handler))
    application.add_handler(CommandHandler("openclaw", openclaw_handler))
    application.add_handler(CommandHandler("occ", openclaw_cloud_handler))
    application.add_handler(CommandHandler("cloud", openclaw_cloud_handler))
    application.add_handler(CommandHandler(["clcode", "codeclaude", "codeclode"], handle_claude_code))
    application.add_handler(CommandHandler("brainlist", brainlist_cmd))
    application.add_handler(CommandHandler("mesh", mesh_handler) if mesh_handler else CommandHandler("mesh", _mesh_unavailable))
    application.add_handler(CommandHandler(["3d", "text3d", "instant3d"], cmd_text_to_3d))
    application.add_handler(CommandHandler("convert3d", cmd_convert3d))
    application.add_handler(CommandHandler("readme", cmd_readme))
    application.add_handler(CommandHandler("diagnose", cmd_diagnose))
    application.add_handler(CommandHandler("savecode", cmd_savecode))
    application.add_handler(CommandHandler("gif", handle_gif_command))
    application.add_handler(CommandHandler("gifdone", finish_gif_session))
    application.add_handler(CommandHandler("gifcancel", cancel_gif_session))
    application.add_handler(CommandHandler("stickerpack", cmd_stickerpack))
    application.add_handler(CommandHandler("done", finish_stickerpack))
    application.add_handler(CommandHandler("lightmeter", cmd_lightmeter))
    application.add_handler(CommandHandler("trichome", cmd_trichome))
    application.add_handler(CommandHandler("plantid", cmd_plantid))
    application.add_handler(CommandHandler("pflanze", cmd_plantid))
    application.add_handler(CommandHandler("shellgame", cmd_shellgame))
    application.add_handler(CommandHandler("archive", cmd_archive))
    application.add_handler(CommandHandler("archivesearch", cmd_archivesearch))
    application.add_handler(CommandHandler("archivedetails", cmd_archivedetails))
    application.add_handler(CommandHandler("archivedownload", cmd_archivedownload))
    application.add_handler(CommandHandler("papersearch", cmd_papersearch))
    application.add_handler(CommandHandler("psworkspace", cmd_psworkspace))
    application.add_handler(CommandHandler("pschat", cmd_pschat))
    application.add_handler(CommandHandler(["brain", "brainweb", "braindashboard"], cmd_brainweb))
    application.add_handler(CommandHandler("dragon", cmd_dragon))
    application.add_handler(CommandHandler("spacewar", cmd_spacewar))
    application.add_handler(CommandHandler("chess", cmd_chess))
    application.add_handler(CommandHandler("superskill", cmd_superskill))
    application.add_handler(CommandHandler("sandbox", cmd_sandbox))
    application.add_handler(CommandHandler("runcode", cmd_runcode))
    application.add_handler(CommandHandler("codefile", cmd_codefile))
    application.add_handler(CommandHandler("py", cmd_py))
    application.add_handler(CommandHandler("htmlapp", cmd_htmlapp))

    # === NEU: Sendcode Handler (mit PDF, ZIP, Einzeldateien) ===
    from send_code_handler import cmd_send_code, sendcode_callback
    application.add_handler(CommandHandler("sendcode", cmd_send_code))
    application.add_handler(CallbackQueryHandler(sendcode_callback, pattern=r"^sendcode:"))

    application.add_handler(CallbackQueryHandler(handle_yt_pdf_callback, pattern=r"^ytpdf\|"))
    application.add_handler(CallbackQueryHandler(handle_email_callback, pattern=r"^email\|"))
    application.add_handler(CallbackQueryHandler(handle_audit_callback, pattern=r"^audit:"))
    application.add_handler(CallbackQueryHandler(superagent_callback, pattern=r"^super:"))
    application.add_handler(CallbackQueryHandler(openclaw_callback, pattern=r"^openclaw:"))
    application.add_handler(CallbackQueryHandler(openclaw_cloud_callback, pattern=r"^occ:"))
    application.add_handler(CallbackQueryHandler(brain_cb, pattern=r"^brain:"))
    application.add_handler(CallbackQueryHandler(papersearch_callback, pattern=r"^ps:"))
    application.add_handler(CallbackQueryHandler(brainweb_callback, pattern=r"^brainweb:"))
    application.add_handler(CallbackQueryHandler(dragon_callback, pattern=r"^dragon:"))
    application.add_handler(CallbackQueryHandler(spacewar_callback, pattern=r"^spacewar:"))
    application.add_handler(CallbackQueryHandler(chess_callback, pattern=r"^chess:"))
    application.add_handler(CallbackQueryHandler(sandbox_callback, pattern=r"^sandbox:"))
    application.add_handler(CallbackQueryHandler(lightmeter_callback, pattern=r"^lightmeter:"))
    application.add_handler(CallbackQueryHandler(trichome_callback, pattern=r"^trichome:"))
    application.add_handler(CallbackQueryHandler(plantid_callback, pattern=r"^plantid:"))
    application.add_handler(CallbackQueryHandler(handle_bg_callback, pattern=r"^bg_(remove|keep):"))
        # Sendcode Callbacks (PDF, ZIP, Einzeldateien)
    

    application.add_handler(MessageHandler((filters.PHOTO | filters.Document.IMAGE) & ~filters.COMMAND & gif_active, collect_gif_for_session), group=-1)
    application.add_handler(MessageHandler((filters.PHOTO | filters.Document.IMAGE) & ~filters.COMMAND & sticker_active, collect_sticker_for_pack))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(MessageHandler(filters.VOICE, handle_voice))
    application.add_handler(MessageHandler(filters.AUDIO, handle_audio_upload))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))


        # Send Code – sicher gekapselt
    try:
        from send_code_handler import cmd_send_code
        application.add_handler(CommandHandler("sendcode", cmd_send_code))
        logger.info("✅ send_code_handler geladen")
    except Exception as e:
        logger.warning("⚠️ send_code_handler nicht geladen: %s", e)

    async def global_error_handler(update, context):
        err = context.error
        logger.warning(f"Handler-Fehler abgefangen: {type(err).__name__}: {err}")

        # Bei reinen Netzwerk-Timeouts keine zusätzliche User-Nachricht erzeugen:
        # das verhindert Timeout-Feedback-Schleifen.
        timeoutish = isinstance(err, (asyncio.TimeoutError, TgTimedOut, TgNetworkError))
        if err is not None and not timeoutish:
            err_text = str(err).lower()
            if "timed out" in err_text or "timeout" in err_text or "network" in err_text:
                timeoutish = True
        if timeoutish:
            return

        if update and hasattr(update, "effective_chat") and update.effective_chat:
            try:
                await safe_send_message(
                    context.bot,
                    str(update.effective_chat.id),
                    "Kurze Verbindungsstoerung bitte nochmal versuchen.",
                )
            except Exception:
                pass

    application.add_error_handler(global_error_handler)
    logger.info("Alle Telegram-Handler registriert")

# ═══════════════════════════════════════════════════════════════════════════════
# KORRIGIERTE INITIALISIERUNG – PTB v20 OFFICIAL PATTERN + WATCHDOG
# ═══════════════════════════════════════════════════════════════════════════════

# WICHTIG: Bei HF Spaces ist set_webhook() oft langsam/timeouted.
# Das ist KEIN Fehler – der Webhook funktioniert trotzdem, da Telegram
# die URL bereits kennt (wenn sie vorher schon gesetzt war).
# Wir behandeln set_webhook-Timeouts daher als Warnung, nicht als Fehler.

async def _set_webhook_safe() -> bool:
    """Setzt den Webhook. Timeout bei HF Spaces ist normal und kein Fehler."""
    if not application or not WEBHOOK_URL:
        return False

    webhook_url = f"{WEBHOOK_URL}/webhook"

    for attempt in range(1, 4):
        try:
            await asyncio.wait_for(
                application.bot.set_webhook(
                    url=webhook_url,
                    allowed_updates=["message", "edited_message", "callback_query"],
                    drop_pending_updates=True,
                ),
                timeout=30.0,
            )
            logger.info(f"✅ Webhook gesetzt: {webhook_url}")
            return True
        except asyncio.TimeoutError:
            # HF Spaces Netzwerk ist langsam – das ist OK!
            logger.warning(f"⏱️ set_webhook Versuch {attempt}/3 timed out (HF Spaces – normal)")
            if attempt == 3:
                # Letzter Versuch: Trotzdem als OK werten, da Webhook meist schon funktioniert
                logger.info("   → Webhook war wahrscheinlich schon gesetzt. Fahre fort.")
                return True
            await asyncio.sleep(2)
        except Exception as e:
            logger.error(f"❌ set_webhook Versuch {attempt}/3 fehlgeschlagen: {e}")
            if attempt < 3:
                await asyncio.sleep(2)
    return False


async def _delete_webhook_safe():
    """Löscht den Webhook (für Polling-Mode)."""
    if not application:
        return
    try:
        await asyncio.wait_for(
            application.bot.delete_webhook(drop_pending_updates=False),
            timeout=10.0,
        )
        logger.info("Webhook gelöscht")
    except Exception as e:
        logger.warning(f"Webhook löschen fehlgeschlagen (ignoriert): {e}")


async def _send_startup_greeting():
    """Sendet eine Nachricht an den Owner beim Startup."""
    if not OWNER_CHAT_ID or not application:
        return
    try:
        await application.bot.send_message(
            chat_id=OWNER_CHAT_ID,
            text="🤖 Bot ist online!\nMode: Webhook\nVersion: 3.0.2\nSchreib /start für die Übersicht.",
        )
        logger.info(f"Startup-Greeting gesendet an {OWNER_CHAT_ID}")
    except Exception as e:
        logger.warning(f"Startup-Greeting fehlgeschlagen: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# WATCHDOG – Überwacht ob der Bot noch Updates verarbeitet
# ═══════════════════════════════════════════════════════════════════════════════

async def application_watchdog():
    """
    Überwacht die Bot-Gesundheit. 
    WICHTIG: Prüft NUR running + initialized. 
    bot._initialized ist bei PTB v20 NICHT zuverlässig und führt zu False-Positives.
    """
    await asyncio.sleep(20)
    logger.info("🔍 Application Watchdog gestartet")

    while True:
        try:
            if not application:
                logger.warning("Watchdog: Keine Application vorhanden")
                await asyncio.sleep(30)
                continue

            running = getattr(application, "_running", False)
            initialized = getattr(application, "_initialized", False)

            # Queue-Größe als Zusatzinfo (nicht als Kriterium)
            queue_size = "?"
            try:
                queue_size = application.update_queue.qsize()
            except Exception:
                pass

            logger.info(f"WATCHDOG | running={running} | initialized={initialized} | queue={queue_size}")

            # NUR restarten wenn Application wirklich tot
            if not running or not initialized:
                logger.error("⚠️ APPLICATION TOT – versuche Neustart...")
                try:
                    # Cleanup vor Neustart
                    try:
                        if getattr(application, "_running", False):
                            await application.stop()
                    except Exception:
                        pass
                    try:
                        if getattr(application, "_initialized", False):
                            await application.shutdown()
                    except Exception:
                        pass

                    # Frischer Start mit async with (offizielles PTB Pattern)
                    await application.initialize()
                    await application.start()
                    logger.info("✅ Application erfolgreich neu gestartet")

                    if USE_WEBHOOK and WEBHOOK_URL:
                        await _set_webhook_safe()

                except Exception as e:
                    logger.error(f"Neustart fehlgeschlagen: {e}")
            else:
                # Alle 2 Minuten: Prüfe ob Webhook noch da ist (nur Webhook-Mode)
                if USE_WEBHOOK and WEBHOOK_URL:
                    try:
                        info = await asyncio.wait_for(
                            application.bot.get_webhook_info(), 
                            timeout=10.0
                        )
                        if not info.url:
                            logger.warning("Webhook nicht mehr gesetzt – setze neu...")
                            await _set_webhook_safe()
                    except Exception:
                        pass  # Nicht kritisch

        except Exception as e:
            logger.error(f"Watchdog Fehler: {e}")

        await asyncio.sleep(25)


# ═══════════════════════════════════════════════════════════════════════════════
# WEBHOOK MODE INITIALISIERUNG
# ═══════════════════════════════════════════════════════════════════════════════

async def init_webhook_mode():
    """
    Initialisiert den Bot im Webhook-Mode.
    Nutzt das offizielle PTB v20 Pattern: initialize() → start() → set_webhook()
    """
    if not application:
        logger.error("init_webhook_mode: Keine Application vorhanden")
        return

    logger.info("🚀 Starte Webhook Mode Initialisierung...")

    try:
        # Offizielles PTB v20 Pattern
        await application.initialize()
        logger.info("✅ Application initialisiert")

        await application.start()
        logger.info("✅ Application gestartet")

        # Webhook setzen (Timeout ist bei HF Spaces normal)
        await _set_webhook_safe()

        # Startup-Greeting
        await _send_startup_greeting()

        logger.info("🎯 Webhook Mode vollständig initialisiert")

    except Exception as e:
        logger.error(f"❌ Webhook-Init fehlgeschlagen: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# POLLING MODE (FALLBACK)
# ═══════════════════════════════════════════════════════════════════════════════

async def _polling_inner():
    """Innere Polling-Schleife mit Fehlerbehandlung."""
    update_offset = _load_offset()
    consecutive_errors = 0
    max_consecutive_errors = 5

    logger.info(f"Polling-Loop gestartet (Offset: {update_offset})")
    while True:
        try:
            updates = await application.bot.get_updates(
                offset=update_offset,
                timeout=10,
                allowed_updates=["message", "edited_message", "callback_query"],
            )
            consecutive_errors = 0
            for update in updates:
                try:
                    await application.process_update(update)
                except Exception as e:
                    logger.error(f"Update-Verarbeitungsfehler: {e}")
                update_offset = update.update_id + 1
                _save_offset(update_offset)
            if not updates:
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            logger.info("Polling inner loop cancelled")
            _save_offset(update_offset)
            raise
        except (httpx.TimeoutException, asyncio.TimeoutError) as e:
            consecutive_errors += 1
            wait_time = min(2 ** consecutive_errors, 15)
            logger.warning(f"Polling-Timeout #{consecutive_errors}: {e} warte {wait_time}s")
            await asyncio.sleep(wait_time)
        except (httpx.ConnectError, httpx.NetworkError, ConnectionError) as e:
            consecutive_errors += 1
            wait_time = min(2 ** consecutive_errors, 15)
            logger.warning(f"Netzwerkfehler #{consecutive_errors}: {e} warte {wait_time}s")
            await asyncio.sleep(wait_time)
        except Exception as e:
            consecutive_errors += 1
            wait_time = min(2 ** consecutive_errors, 15)
            logger.error(f"Polling-Fehler #{consecutive_errors}: {e} warte {wait_time}s")
            await asyncio.sleep(wait_time)
        if consecutive_errors >= max_consecutive_errors:
            logger.error(f"Zu viele Fehler ({consecutive_errors}). Neustart in 30s...")
            await asyncio.sleep(60)
            consecutive_errors = 0


async def polling_loop():
    """Haupt-Polling-Loop."""
    logger.info("Starte Bot (Polling-Mode)...")

    try:
        await _delete_webhook_safe()
        await application.initialize()
        await application.start()
        logger.info("✅ Bot bereit für Polling")
        await _send_startup_greeting()
    except Exception as e:
        logger.error(f"Polling-Init fehlgeschlagen: {e}")
        return

    while True:
        try:
            await _polling_inner()
        except asyncio.CancelledError:
            logger.info("Polling gestoppt")
            break
        except Exception as e:
            logger.error(f"Polling-Loop abgestürzt: {e} Neustart in 10s")
            await asyncio.sleep(10)


# ═══════════════════════════════════════════════════════════════════════════════
# FASTAPI LIFESPAN – OFFIZIELLES PTB v20 PATTERN
# ═══════════════════════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _polling_task, _watchdog_task
    logger.info("🚀 FastAPI Lifespan Startup...")
    logger.info(f"USE_WEBHOOK: {USE_WEBHOOK}")
    logger.info(f"WEBHOOK_URL: {WEBHOOK_URL}")

    if not application:
        logger.error("❌ Keine Telegram Application verfügbar!")
        yield
        return

    # Self-Keepalive starten (Render/Railway Free-Tier Wake-Up)
    try:
        asyncio.create_task(_self_keepalive())
        logger.info("✅ Self-Keepalive gestartet")
    except Exception as e:
        logger.warning(f"Self-Keepalive konnte nicht gestartet werden: {e}")

    # WATCHDOG starten (überwacht Bot-Gesundheit)
    _watchdog_task = asyncio.create_task(application_watchdog())
    logger.info("✅ Application Watchdog gestartet")

    if USE_WEBHOOK:
        if not WEBHOOK_URL:
            logger.error("USE_WEBHOOK=true aber WEBHOOK_URL fehlt!")
        else:
            asyncio.create_task(init_webhook_mode())
            logger.info("✅ Webhook-Init-Task gestartet (asynchron)")
            logger.info(f"   → Webhook URL: {WEBHOOK_URL}/webhook")
    else:
        _polling_task = asyncio.create_task(polling_loop())
        logger.info("✅ Polling-Task gestartet")

    yield

    logger.info("🛑 FastAPI Lifespan Shutdown...")

    if _watchdog_task and not _watchdog_task.done():
        _watchdog_task.cancel()
        try:
            await asyncio.wait_for(_watchdog_task, timeout=5.0)
        except Exception:
            pass

    if _polling_task and not _polling_task.done():
        _polling_task.cancel()
        try:
            await asyncio.wait_for(_polling_task, timeout=10.0)
        except Exception:
            pass

    if application:
        try:
            if getattr(application, "_running", False):
                await application.stop()
            if getattr(application, "_initialized", False):
                await application.shutdown()
            try:
                await application.bot.session.close()
            except Exception:
                pass
            logger.info("Bot sauber heruntergefahren")
        except Exception as e:
            logger.warning(f"Fehler beim Herunterfahren: {e}")

    logger.info("Shutdown abgeschlossen")



app = FastAPI(
    title="Telllmeeedrei_BOT",
    description="Telegram Bot mit 20+ Mini-Apps und Super-Skill Generator",
    version="3.0.2",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(superskill_router)

IS_HF_SPACE = os.getenv("SPACE_ID") is not None
DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
except (PermissionError, OSError) as exc:
    DATA_DIR = Path(__file__).resolve().parent / "data"
    logger.warning(
        "DATA_DIR nicht beschreibbar (%s) – verwende Fallback '%s'",
        exc, DATA_DIR,
    )
    DATA_DIR.mkdir(parents=True, exist_ok=True)

local_static = Path(__file__).parent / "static"
if local_static.exists() and local_static.is_dir() and any(local_static.iterdir()):
    static_dir = local_static
    logger.info(f"Static (local project): {static_dir}")
else:
    static_dir = DATA_DIR / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    logger.info(f"Static (DATA_DIR): {static_dir}")

app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# ═══════════════════════════════════════════════════════════════════════════════
# MINI-APP MOUNTS – Alle essenziellen Apps sofort mounten
# ═══════════════════════════════════════════════════════════════════════════════

if diagnose_app:
    app.mount("/diagnose", diagnose_app)
    logger.info("diagnose_app mounted")

if scanner_mini_app:
    app.mount("/scanner", scanner_mini_app)
    logger.info("scanner_mini_app mounted")

if sandbox_mini_app:
    app.mount("/sandbox", sandbox_mini_app)
    logger.info("sandbox_mini_app mounted")

# Optionale Mini-Apps (werden ebenfalls gemountet, aber sind leichtgewichtig)
_lazy_apps = {
    "voice": voice_mini_app,
    "lightmeter": lightmeter_mini_app,
    "trichome": trichome_mini_app,
    "plantid": plantid_mini_app,
    "shellgame": shellgame_mini_app,
    "archive": archive_mini_app,
    "papersearch": papersearch_mini_app,
    "dragon": dragon_mini_app,
    "spacewar": spacewar_mini_app,
    "chess": chess_app,
}

for name, mini_app in _lazy_apps.items():
    if mini_app:
        try:
            route = f"/{name}"
            app.mount(route, mini_app)
            logger.info(f"{name}_mini_app mounted")
        except Exception as e:
            logger.warning(f"Mount {name} fehlgeschlagen: {e}")


static_candidates = [
    Path("/data/static"),
    Path.cwd() / "static",
    Path(__file__).parent / "static",
    Path("/home/user/app/static"),
    Path("/home/user/app"),
    Path("/opt/render/project/src/static"),
]

sandbox_static_dir = None
for candidate in static_candidates:
    if not candidate.exists():
        continue
    css_path = candidate / "css" / "sandbox.css"
    js_path = candidate / "js" / "sandbox.js"
    if css_path.exists() and js_path.exists():
        sandbox_static_dir = candidate
        logger.info(f"Sandbox Static-Ordner GEFUNDEN: {candidate}")
        break
    elif candidate.exists():
        logger.info(f"Ordner gefunden, aber css/js fehlt: {candidate}")

if sandbox_static_dir:
    app.mount("/sandbox/static", StaticFiles(directory=str(sandbox_static_dir)), name="sandbox_static")
    logger.info(f"Sandbox Static Files gemountet")

app.mount("/brain-static", StaticFiles(directory=str(static_dir)), name="brain_static")


def _ensure_superskill_assets() -> None:
    """Stellt sicher, dass SuperSkill-Assets auch dann verfügbar sind, wenn sie im Repo-Root liegen."""
    src_root = Path(__file__).parent
    css_src = src_root / "super_skill.css"
    js_src = src_root / "super_skill.js"
    html_alt_src = src_root / "super_skill_workspace_alt.html"
    html_alt_src_2 = src_root / "super_skill_workspace (1).html"
    html_src = src_root / "super_skill_workspace.html"

    css_dst = static_dir / "css" / "super_skill.css"
    js_dst = static_dir / "js" / "super_skill.js"
    html_dst = static_dir / "super_skill_workspace.html"
    html_alt_dst = static_dir / "super_skill_workspace_alt.html"

    try:
        css_dst.parent.mkdir(parents=True, exist_ok=True)
        js_dst.parent.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        logger.warning("SuperSkill Asset-Ordner konnten nicht erstellt werden: %s", e)
        return

    for src, dst in [
        (css_src, css_dst),
        (js_src, js_dst),
        (html_alt_src, html_alt_dst),
        (html_alt_src_2, html_alt_dst),
    ]:
        try:
            if src.exists() and (not dst.exists() or src.stat().st_mtime > dst.stat().st_mtime):
                dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
                logger.info("SuperSkill Asset gespiegelt: %s -> %s", src.name, dst)
        except Exception as e:
            logger.warning("SuperSkill Asset-Spiegelung fehlgeschlagen (%s): %s", src.name, e)

    # Wenn es nur die alte HTML gibt, trotzdem in static bereitstellen.
    try:
        if not html_dst.exists() and html_src.exists():
            html_dst.write_text(html_src.read_text(encoding="utf-8"), encoding="utf-8")
    except Exception as e:
        logger.warning("SuperSkill HTML-Fallback konnte nicht gespiegelt werden: %s", e)


_ensure_superskill_assets()


def _read_superskill_html() -> Optional[str]:
    legacy_content = None
    candidates = [
        static_dir / "super_skill_workspace (1).html",
        static_dir / "super_skill_workspace_alt.html",
        Path("/home/user/app/static/super_skill_workspace_alt.html"),
        Path("/app/static/super_skill_workspace_alt.html"),
        Path("/home/user/app/static/super_skill_workspace (1).html"),
        Path("/app/static/super_skill_workspace (1).html"),
        static_dir / "super_skill_workspace.html",
        Path("/home/user/app/static/super_skill_workspace.html"),
        Path("/app/static/super_skill_workspace.html"),
        Path(__file__).parent / "super_skill_workspace_alt.html",
        Path(__file__).parent / "super_skill_workspace.html",
    ]
    for html_path in candidates:
        if not html_path.exists():
            continue
        try:
            content = html_path.read_text(encoding="utf-8")
            # Alte Frontend-Version (direkter Anthropic-Call) bevorzugt NICHT ausliefern.
            if "https://api.anthropic.com/v1/messages" in content:
                logger.warning("SuperSkill HTML %s nutzt Legacy-Frontend, suche modernere Variante...", html_path)
                if legacy_content is None:
                    legacy_content = content
                continue
            return content
        except Exception as e:
            logger.warning("SuperSkill HTML konnte nicht gelesen werden (%s): %s", html_path, e)
    return legacy_content

@app.get("/superskill", response_class=HTMLResponse)
@app.get("/superskill/", response_class=HTMLResponse)
async def superskill_page():
    html = _read_superskill_html()
    if html:
        return HTMLResponse(content=html)
    logger.error("super_skill_workspace.html nicht gefunden!")
    return HTMLResponse(
        content="<h1 style='color:red;text-align:center;margin-top:50px;'>SuperSkill-Workspace nicht gefunden</h1>",
        status_code=404
    )


@app.get("/brain", response_class=HTMLResponse)
@app.get("/brain/", response_class=HTMLResponse)
async def brain_dashboard():
    brain_file = static_dir / "brain.html"
    if brain_file.exists():
        return FileResponse(brain_file)
    return HTMLResponse("""
        <h1 style="color:red;text-align:center;margin-top:50px;">brain.html nicht gefunden</h1>
        <p style="text-align:center;">Bitte lade die Datei als <code>static/brain.html</code> hoch.</p>
    """, status_code=404)


@app.get("/")
async def root():
    return {
        "status": "online",
        "bot": "Telllmeeedrei_BOT",
        "version": "3.0.2",
        "features": ["telegram", "superskill", "brain", "20+ mini-apps"],
        "timestamp": datetime.now().isoformat(),
    }


@app.get("/ping")
async def ping():
    return {"status": "alive"}


@app.get("/health")
async def health_check():
    brain_ok = brain_enabled()
    groq_ok = bool(GROQ_API_KEY)
    telegram_ok = bool(TELEGRAM_TOKEN)
    app_running = getattr(application, "_running", False) if application else False
    return {
        "status": "healthy" if all([brain_ok, groq_ok, telegram_ok, app_running]) else "degraded",
        "services": {
            "telegram": "ok" if telegram_ok else "missing_token",
            "groq": "ok" if groq_ok else "missing_key",
            "brain": "ok" if brain_ok else "disabled",
            "superskill": "ok",
            "bot_running": "ok" if app_running else "not_running",
        },
        "timestamp": datetime.now().isoformat(),
    }


@app.get("/status")
async def status():
    try:
        if application:
            webhook_info = await application.bot.get_webhook_info()
            webhook_url = webhook_info.url
        else:
            webhook_url = "no_app"
    except Exception:
        webhook_url = "error"
    return {
        "bot_mode": "webhook" if USE_WEBHOOK else "polling",
        "polling_task_running": _polling_task is not None and not _polling_task.done(),
        "watchdog_task_running": _watchdog_task is not None and not _watchdog_task.done(),
        "last_update_offset": _load_offset(),
        "webhook_set": bool(webhook_url),
        "webhook_url": webhook_url,
        "use_webhook_env": os.getenv("USE_WEBHOOK", "false"),
        "app_initialized": getattr(application, "_initialized", False) if application else False,
        "app_running": getattr(application, "_running", False) if application else False,
        "bot_initialized": getattr(application.bot, "_initialized", False) if application else False,
    }


@app.get("/dashboard")
async def dashboard_info():
    return {
        "dashboard": "streamlit run dashboard.py --server.port 8501 --server.headless true",
        "features": "100+ params, model/prompt edit, sandbox tester, superagent list",
        "streamlit": "Ready in requirements.txt"
    }


# ── Self-Keepalive (Render/Railway Free-Tier Wake-Up) ────────────────────────
async def _self_keepalive():
    """Pingt den eigenen /ping-Endpoint alle ~4 Minuten (Wake-Up Fix)."""
    await asyncio.sleep(30)

    port = int(os.getenv("PORT", "7860"))
    possible_urls = []
    if WEBHOOK_URL:
        possible_urls.append(f"{WEBHOOK_URL}/ping")
    if os.getenv("RENDER_EXTERNAL_URL"):
        possible_urls.append(f"{os.getenv('RENDER_EXTERNAL_URL').rstrip('/')}/ping")
    possible_urls.append(f"http://localhost:{port}/ping")

    logger.info("💓 Self-Keepalive startet mit URLs: %s", possible_urls)

    while True:
        success = False
        for url in possible_urls:
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.get(url)
                    if resp.status_code == 200:
                        success = True
                        break
            except Exception:
                continue

        if not success:
            logger.warning("⚠️ Self-Keepalive: Keine URL erreichbar")

        await asyncio.sleep(240)

# ═══════════════════════════════════════════════════════════════════════════════
# WEBHOOK ENDPOINT – ROBUST MIT LOGGING
# ═══════════════════════════════════════════════════════════════════════════════


_pending_webhook_updates: list = []
_recent_webhook_update_ids: Dict[int, float] = {}
_recent_webhook_ttl_seconds = 15 * 60
_recent_webhook_max_ids = 5000
_recent_webhook_last_cleanup = 0.0


def _remember_update_id(update_id: Optional[int]) -> bool:
    global _recent_webhook_last_cleanup
    if update_id is None:
        return True
    now = asyncio.get_running_loop().time()
    expiry = now - _recent_webhook_ttl_seconds
    if (now - _recent_webhook_last_cleanup > 60.0) or (len(_recent_webhook_update_ids) > _recent_webhook_max_ids):
        stale_ids = [uid for uid, ts in _recent_webhook_update_ids.items() if ts < expiry]
        for uid in stale_ids:
            _recent_webhook_update_ids.pop(uid, None)
        _recent_webhook_last_cleanup = now
    if update_id in _recent_webhook_update_ids:
        return False
    _recent_webhook_update_ids[update_id] = now
    return True


async def _enqueue_telegram_update(data: dict):
    update_id = data.get("update_id")
    if isinstance(update_id, int) and not _remember_update_id(update_id):
        logger.info("Webhook Duplicate ignoriert (update_id=%s)", update_id)
        return
    update = Update.de_json(data, application.bot)
    await application.update_queue.put(update)


async def _replay_pending_updates():
    if not _pending_webhook_updates:
        return
    logger.info(f"Replay {len(_pending_webhook_updates)} gepufferte Updates...")
    try:
        for data in list(_pending_webhook_updates):
            try:
                await _enqueue_telegram_update(data)
            except Exception as e:
                logger.warning(f"Replay Update Fehler: {e}")
    finally:
        _pending_webhook_updates.clear()


@app.post("/webhook")
async def webhook_endpoint(request: Request):
    """Robuster Webhook Endpoint mit Logging und Buffer-Logik."""
    try:
        data = await request.json()
        update_id = data.get("update_id")

        logger.info(f"📥 Webhook erhalten | update_id={update_id}")

        if not application or not getattr(application, "_running", False):
            logger.warning(f"Bot noch nicht bereit → Update gepuffert (update_id={update_id})")
            _pending_webhook_updates.append(data)
            if len(_pending_webhook_updates) > 50:
                _pending_webhook_updates.pop(0)

            # Versuche Bot zu initialisieren falls noch nicht gestartet
            if application and not _init_lock.locked():
                async def _init_and_replay():
                    ok = await fast_init_bot()
                    if ok and USE_WEBHOOK and WEBHOOK_URL:
                        await _set_webhook()
                    await _replay_pending_updates()
                asyncio.create_task(_init_and_replay())

            return {"ok": True}

        # Update verarbeiten
        await _enqueue_telegram_update(data)
        logger.debug(f"✅ Update verarbeitet: update_id={update_id}")

        # Falls es gepufferte Updates gibt, diese auch abarbeiten
        if _pending_webhook_updates:
            asyncio.create_task(_replay_pending_updates())

        return {"ok": True}

    except Exception as e:
        logger.error(f"❌ Webhook-Verarbeitungsfehler: {e}", exc_info=True)
        return {"ok": True}  # Immer 200 zurückgeben!


# ═══════════════════════════════════════════════════════════════════════════════
# API ENDPOINTS (Brain etc.)
# ═══════════════════════════════════════════════════════════════════════════════

class QueryRequest(BaseModel):
    query: str
    chat_id: Optional[str] = None


def _resolve_chat_id(chat_id: Optional[str]) -> Optional[str]:
    if chat_id and str(chat_id).strip():
        return str(chat_id).strip()
    if BOT_OWNER_ID and str(BOT_OWNER_ID).strip():
        return str(BOT_OWNER_ID).strip()
    if OWNER_CHAT_ID and str(OWNER_CHAT_ID).strip():
        return str(OWNER_CHAT_ID).strip()
    return None


@app.get("/api/brain/entries")
async def api_brain_entries(chat_id: Optional[str] = None):
    resolved_chat_id = _resolve_chat_id(chat_id)
    if not resolved_chat_id:
        return []
    try:
        entries = await asyncio.wait_for(load_all_entries(resolved_chat_id), timeout=10.0)
        return [
            {
                "id": e.get("id"),
                "title": e.get("title"),
                "entry_type": e.get("entry_type"),
                "created_at": e.get("created_at")
            } for e in entries
        ]
    except asyncio.TimeoutError:
        logger.warning("Brain entries Timeout")
        return []
    except Exception as e:
        logger.error(f"API /brain/entries Fehler: {e}")
        return []



@app.post("/api/brain/query")
async def api_brain_query(request: QueryRequest):
    resolved_chat_id = _resolve_chat_id(request.chat_id)
    if not resolved_chat_id:
        return {"success": False, "answer": "chat_id fehlt. Bitte mitgeben oder OWNER_CHAT_ID setzen."}
    try:
        result = await asyncio.wait_for(
            brain_query_agent(resolved_chat_id, request.query),
            timeout=30.0
        )
        return result
    except asyncio.TimeoutError:
        return {"success": False, "answer": "Timeout beim Brain-Agent."}
    except Exception as e:
        logger.error(f"API /brain/query Fehler: {e}")
        return {"success": False, "answer": f"Interner Fehler: {str(e)}"}


@app.get("/api/brain/download/{entry_id}")
async def api_brain_download(entry_id: str, chat_id: Optional[str] = None):
    resolved_chat_id = _resolve_chat_id(chat_id)
    if not resolved_chat_id:
        return {"error": "chat_id fehlt. Bitte mitgeben oder OWNER_CHAT_ID setzen."}
    try:
        entry = await asyncio.wait_for(load_entry(resolved_chat_id, entry_id), timeout=10.0)
        if not entry or entry.get("entry_type") != "file":
            return {"error": "Datei nicht gefunden"}

        file_bytes = base64.b64decode(entry["content"])
        metadata = json.loads(entry.get("metadata", "{}"))
        filename = metadata.get("filename", f"brain_file_{entry_id}")

        return StreamingResponse(
            iter([file_bytes]),
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Length": str(len(file_bytes))
            }
        )
    except asyncio.TimeoutError:
        return {"error": "Timeout beim Laden"}
    except Exception as e:
        logger.error(f"Download Fehler fuer {entry_id}: {e}")
        return {"error": str(e)}


if __name__ == "__main__":
    import uvicorn
    logger.info(f"Starte Server auf {HOST}:{PORT}")
    uvicorn.run(
        app,
        host=HOST,
        port=PORT,
        workers=1,
        loop="asyncio",
        log_level="info",
    )
