# dv.py – Universal Data Processor (FINAL + kompatibel mit main.py)
import logging
import os
import mimetypes
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont
from pydub import AudioSegment
from docx import Document
import PyPDF2
import pandas as pd
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
import json

logger = logging.getLogger(__name__)

def get_mime(file_path: str) -> str:
    mime, _ = mimetypes.guess_type(file_path)
    if mime:
        return mime
    ext = os.path.splitext(file_path)[1].lower()
    fallback = {
        '.pdf': 'application/pdf',
        '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        '.png': 'image/png',
        '.jpg': 'image/jpeg',
        '.mp3': 'audio/mpeg',
        '.wav': 'audio/wav',
        '.mp4': 'video/mp4',
        '.txt': 'text/plain',
        '.csv': 'text/csv',
        '.json': 'application/json',
    }
    return fallback.get(ext, "application/octet-stream")


# ====================== LESEN ======================
def extract_content(file_path: str, max_chars: int = 12000) -> str:
    mime = get_mime(file_path)
    name = os.path.basename(file_path)

    try:
        if mime.startswith("image/"):
            img = Image.open(file_path)
            return f"📸 Bild '{name}': {img.format} | Größe: {img.size} | Modus: {img.mode}"

        elif mime.startswith("audio/"):
            audio = AudioSegment.from_file(file_path)
            return f"🎵 Audio '{name}': {len(audio)/1000:.1f}s | Kanäle: {audio.channels} | {audio.frame_rate}Hz"

        elif mime.startswith("video/"):
            return f"🎥 Video '{name}': Datei erkannt"

        elif mime == "application/pdf":
            with open(file_path, "rb") as f:
                reader = PyPDF2.PdfReader(f)
                text = "".join(p.extract_text() or "" for p in reader.pages)
            return f"📄 PDF '{name}' ({len(reader.pages)} Seiten):\n{text[:max_chars]} [...]"

        elif "wordprocessingml" in mime or mime.endswith("document"):
            doc = Document(file_path)
            text = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
            return f"📝 Word '{name}':\n{text[:max_chars]} [...]"

        elif mime in ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "text/csv"):
            df = pd.read_excel(file_path) if "excel" in mime else pd.read_csv(file_path)
            return f"📊 {mime.split('/')[-1].upper()} '{name}' ({df.shape[0]} Zeilen):\n{df.head(10).to_string()}"

        elif mime == "application/json":
            with open(file_path, encoding="utf-8") as f:
                data = json.load(f)
            return f"📋 JSON '{name}':\n{json.dumps(data, indent=2, ensure_ascii=False)[:max_chars]}"

        elif mime.startswith("text/"):
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read(max_chars)
            return f"📜 Text '{name}':\n{text}"

        return f"❓ Unbekanntes Format '{name}' (MIME: {mime})"

    except Exception as e:
        logger.error(f"extract_content Fehler bei {name}: {e}")
        return f"💥 Extract-Fehler bei {name}: {str(e)[:200]}"


# ====================== ALTE FUNKTIONEN (für main.py Kompatibilität) ======================
def resize_image(file_path: str, size: tuple = (800, 800)) -> BytesIO:
    img = Image.open(file_path).resize(size, Image.Resampling.LANCZOS)
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer


def trim_audio(file_path: str, start: float = 0.0, end: float | None = None) -> BytesIO:
    audio = AudioSegment.from_file(file_path)
    if end is None:
        end = len(audio) / 1000
    trimmed = audio[int(start * 1000):int(end * 1000)]
    buffer = BytesIO()
    trimmed.export(buffer, format="mp3")
    buffer.seek(0)
    return buffer


def save_locally(buffer: BytesIO, filename: str, folder: str = "/tmp/processed") -> str:
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, filename)
    with open(path, "wb") as f:
        f.write(buffer.getvalue())
    return path


# ====================== NEUE ERSTELL-FUNKTIONEN ======================
def create_pdf_from_text(text: str, title: str = "Generated.pdf") -> BytesIO:
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    y = 750
    for line in text.split("\n"):
        if y < 50:
            c.showPage()
            y = 750
        c.drawString(50, y, line[:90])
        y -= 15
    c.save()
    buffer.seek(0)
    return buffer


def create_docx_from_text(text: str, title: str = "Generated.docx") -> BytesIO:
    doc = Document()
    doc.add_heading(title, 0)
    doc.add_paragraph(text)
    buffer = BytesIO()
    doc.save(buffer)
    buffer.seek(0)
    return buffer


def create_excel_from_data(data: list[list], columns: list[str], title: str = "Generated.xlsx") -> BytesIO:
    df = pd.DataFrame(data, columns=columns)
    buffer = BytesIO()
    df.to_excel(buffer, index=False)
    buffer.seek(0)
    return buffer


def create_chart_from_df(df: pd.DataFrame, title: str = "Chart") -> BytesIO:
    img = Image.new("RGB", (900, 600), "#2a0044")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 24)
    except:
        font = ImageFont.load_default()

    draw.text((30, 30), title, fill="#ffccff", font=font)

    if len(df.columns) > 1:
        values = df.iloc[:, 1].head(8)
        max_val = max(values) if len(values) > 0 else 1
        for i, val in enumerate(values):
            x = 80 + i * 90
            height = int(380 * (val / max_val))
            draw.rectangle([x, 500 - height, x + 65, 500], fill="#ff66cc")
            draw.text((x + 10, 510), str(val), fill="white", font=font)

    buffer = BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer
