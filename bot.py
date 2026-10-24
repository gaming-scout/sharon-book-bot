"""Read original PDF text with the corresponding PDF page on screen."""
import asyncio
import logging
import os
from pathlib import Path
import re
import subprocess
import tempfile
import uuid

import fitz
import httpx
import imageio_ffmpeg
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

LOG = logging.getLogger(__name__)
ROOT = Path(os.getenv("BOOK_DATA_DIR", "/data/books"))
MAX_UPLOAD = 19 * 1024 * 1024
MAX_VIDEO = 45 * 1024 * 1024


def chunks(text, limit=1800):
    """Split without losing or rewriting words; keep TTS requests bounded."""
    words = text.split()
    current = []
    size = 0
    for word in words:
        if current and size + len(word) + 1 > limit:
            yield " ".join(current)
            current, size = [], 0
        current.append(word)
        size += len(word) + 1
    if current:
        yield " ".join(current)


def extract_page(pdf, index, image):
    with fitz.open(pdf) as doc:
        page = doc[index]
        text = page.get_text("text", sort=True).strip()
        scale = min(1600 / page.rect.width, 1600 / page.rect.height)
        page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False).save(image)
        return text


def inspect_pdf(pdf):
    with fitz.open(pdf) as doc:
        if doc.needs_pass:
            raise ValueError("PDF terkunci. Kirim salinan tanpa password.")
        return len(doc)


async def narrate(text, target):
    key, voice = os.getenv("ELEVENLABS_API_KEY"), os.getenv("VOICE_ID")
    if key and voice:
        async with httpx.AsyncClient(timeout=180) as client:
            response = await client.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{voice}",
                headers={"xi-api-key": key, "Accept": "audio/mpeg"},
                json={"text": text, "model_id": "eleven_multilingual_v2",
                      "voice_settings": {"stability": 0.5, "similarity_boost": 0.8}},
            )
            if response.status_code == 200 and response.content:
                target.write_bytes(response.content)
                return
            # Never log headers, credential-bearing URLs or provider response bodies.
            LOG.warning("ElevenLabs status %s; using configured fallback voice", response.status_code)
    import edge_tts
    await edge_tts.Communicate(
        text, voice=os.getenv("TTS_VOICE", "id-ID-ArdiNeural"), rate="-5%"
    ).save(str(target))


def render_video(image, audio, output):
    command = [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error",
               "-loop", "1", "-framerate", "5", "-i", str(image), "-i", str(audio),
               "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2:color=0x16202a,setsar=1",
               "-c:v", "libx264", "-preset", "veryfast", "-tune", "stillimage",
               "-pix_fmt", "yuv420p", "-crf", "25", "-threads", "1",
               "-c:a", "aac", "-b:a", "96k", "-shortest", "-movflags", "+faststart", str(output)]
    subprocess.run(command, check=True, capture_output=True, timeout=900)
    if output.stat().st_size > MAX_VIDEO:
        raise ValueError("Video bagian ini terlalu besar untuk dikirim.")


async def start(update, context):
    await update.message.reply_text(
        "📚 Kirim PDF untuk dibacakan sesuai teks aslinya, tanpa rangkuman. "
        "Video menampilkan halaman PDF yang sedang dibaca, termasuk ilustrasinya.\n\n"
        "Hasil dikirim per halaman/bagian. PDF scan tanpa teks memerlukan OCR. "
        "Gunakan /batal untuk berhenti setelah bagian yang sedang diproses."
    )


async def cancel(update, context):
    context.user_data["cancel"] = True
    await update.message.reply_text("Proses akan berhenti setelah bagian yang sedang diproses selesai.")


async def receive(update, context):
    if context.user_data.get("busy"):
        await update.message.reply_text("Buku sebelumnya masih diproses. Gunakan /batal terlebih dahulu.")
        return
    document = update.message.document
    if (document.file_size or 0) > MAX_UPLOAD:
        await update.message.reply_text("Kirim PDF di bawah 19 MB; pecah buku menjadi beberapa PDF bila perlu.")
        return
    folder = ROOT / str(update.effective_user.id)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (uuid.uuid4().hex + ".pdf")
    try:
        remote = await context.bot.get_file(document.file_id)
        await remote.download_to_drive(path)
        pages = await asyncio.to_thread(inspect_pdf, path)
        old = context.user_data.get("pdf")
        if old:
            Path(old).unlink(missing_ok=True)
        context.user_data["pdf"] = str(path)
        context.user_data["pages"] = pages
        context.user_data["job"] = path.stem
        await update.message.reply_text(
            f"✅ PDF diterima: {pages} halaman. Teks asli akan dibaca berurutan. "
            "Gambar latar mengikuti halaman, bukan kalimat. Halaman tanpa teks akan dilaporkan.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                "🔊 Baca buku & tampilkan halaman", callback_data="read:" + path.stem)]]),
        )
    except Exception:
        path.unlink(missing_ok=True)
        await update.message.reply_text("PDF tidak dapat dibuka/diunduh. Pastikan tidak terkunci dan di bawah 19 MB.")


async def process_book(chat_id, context):
    data = context.user_data
    sent, empty = 0, []
    page_number = 0
    try:
        with tempfile.TemporaryDirectory(prefix="reader-") as tmp:
            work = Path(tmp)
            for index in range(data["pages"]):
                page_number = index + 1
                if data.get("cancel"):
                    break
                image = work / "page.png"
                text = await asyncio.to_thread(extract_page, data["pdf"], index, image)
                parts = list(chunks(text))
                if not parts:
                    empty.append(page_number)
                    with image.open("rb") as photo:
                        await context.bot.send_photo(chat_id, photo, caption=f"Halaman {page_number}: tanpa teks yang bisa diekstrak. Jika ada tulisan dalam gambar, diperlukan OCR.")
                    continue
                for part, original in enumerate(parts, 1):
                    if data.get("cancel"):
                        break
                    await context.bot.send_message(chat_id, f"🎙️ Halaman {page_number}/{data['pages']}, bagian {part}/{len(parts)}…")
                    audio, video = work / "voice.mp3", work / "page.mp4"
                    await narrate(original, audio)
                    await asyncio.to_thread(render_video, image, audio, video)
                    with video.open("rb") as stream:
                        await context.bot.send_video(
                            chat_id, stream, supports_streaming=True,
                            caption=f"📖 Bacaan asli • halaman {page_number} • bagian {part}/{len(parts)}",
                            read_timeout=300, write_timeout=300,
                        )
                    sent += 1
                    audio.unlink(missing_ok=True)
                    video.unlink(missing_ok=True)
            state = "Dibatalkan" if data.get("cancel") else "Selesai"
            await context.bot.send_message(chat_id, f"{state}. Terkirim {sent} video. Halaman tanpa teks: {len(empty)}. Tidak ada rangkuman atau narasi tambahan.")
    except Exception as exc:
        LOG.error("Reading failed on page %s: %s", page_number, type(exc).__name__)
        await context.bot.send_message(chat_id, f"Proses berhenti pada halaman {page_number}; {sent} video sudah terkirim. Gagal pada layanan suara, render, atau pengiriman. Coba lagi nanti.")
    finally:
        data["busy"] = False


async def clicked(update, context):
    query = update.callback_query
    await query.answer()
    data = context.user_data
    if query.data != "read:" + data.get("job", "") or not Path(data.get("pdf", "")).is_file():
        await query.message.reply_text("Kirim ulang PDF; tombol ini sudah tidak berlaku.")
        return
    if data.get("busy"):
        return
    data["busy"], data["cancel"] = True, False
    await query.edit_message_text("📖 Membaca teks asli buku per halaman…")
    context.application.create_task(process_book(query.message.chat_id, context), update=update)


def main():
    logging.basicConfig(level=logging.WARNING)
    app = Application.builder().token(os.environ["TELEGRAM_TOKEN"]).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("batal", cancel))
    app.add_handler(MessageHandler(filters.Document.PDF, receive))
    app.add_handler(CallbackQueryHandler(clicked, pattern=r"^read:"))
    print("BOT PEMBACA PDF ASLI SIAP", flush=True)
    app.run_polling()


if __name__ == "__main__":
    main()
