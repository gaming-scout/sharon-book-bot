import asyncio
import datetime
import os
import time
import pytz
import nest_asyncio
import edge_tts
import requests
from io import BytesIO
from PIL import Image
from pydub import AudioSegment
from moviepy.editor import ImageClip, AudioFileClip, concatenate_videoclips
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from google import genai
from google.genai import types

nest_asyncio.apply()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

client = genai.Client(api_key=GEMINI_API_KEY)
buku_path_tersimpan = None

DAFTAR_BACKSOUND = {
    "inspirational": "https://cdn.pixabay.com/download/audio/2022/05/27/audio_1808fbf07a.mp3",
    "calm": "https://cdn.pixabay.com/download/audio/2022/01/18/audio_d0a13f69d2.mp3",
    "serious": "https://cdn.pixabay.com/download/audio/2022/03/15/audio_c8c8a73467.mp3"
}

def baca_dan_rangkum_pdf(file_path):
    berkas_terupload = client.files.upload(file=file_path)
    prompt = """
    Analisis buku lengkap ini dari awal sampai akhir. Buatlah naskah voiceover Bahasa Indonesia yang sangat mendalam, detail, dan komprehensif sebagai rangkuman utuh buku ini.
    Aturan Naskah:
    1. Panjang naskah 500 - 700 kata agar durasi pembacaan narasi pas dan berbobot.
    2. Struktur: Pengantar/Premis Buku, Bedah Bab Utama & Konsep Inti, Studi Kasus/Pelajaran Nyata, Kesimpulan Reflektif.
    3. Gunakan bahasa yang mengalir, elegan, dan mudah dipahami.
    4. JANGAN menulis keterangan [Musik masuk], tanda bintang (*), atau pagar (#). Tulis HANYA teks narasi bersih.
    5. Di baris paling pertama, tulis salah satu mood musik ini saja: INSPIRATIONAL atau CALM atau SERIOUS, lalu ganti baris untuk isi naskahnya.
    """
    model_list = ["gemini-3.8-flash", "gemini-2.5-flash", "gemini-1.5-flash"]
    for attempt in range(3):
        for m in model_list:
            try:
                response = client.models.generate_content(model=m, contents=[berkas_terupload, prompt])
                if response.text:
                    return response.text.strip()
            except:
                time.sleep(2)
        time.sleep(5)
    raise Exception("Server Gemini sedang sibuk. Silakan coba lagi nanti.")

async def buat_audio_lengkap(naskah: str, mood: str, nama_file_output: str):
    file_suara = "voiceover_temp.mp3"
    file_musik = "bgm_temp.mp3"
    komunikasi_tts = edge_tts.Communicate(naskah, voice="id-ID-ArdiNeural", rate="-5%")
    await komunikasi_tts.save(file_suara)
    try:
        url_musik = DAFTAR_BACKSOUND.get(mood, DAFTAR_BACKSOUND["inspirational"])
        r = requests.get(url_musik, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        with open(file_musik, "wb") as f:
            f.write(r.content)
        suara_narator = AudioSegment.from_file(file_suara)
        musik_latar = AudioSegment.from_file(file_musik) - 18
        while len(musik_latar) < len(suara_narator) + 3000:
            musik_latar += musik_latar
        musik_latar = musik_latar[:len(suara_narator) + 2000].fade_out(2000)
        audio_gabungan = musik_latar.overlay(suara_narator, position=500)
        audio_gabungan.export(nama_file_output, format="mp3")
    except:
        if os.path.exists(nama_file_output):
            os.remove(nama_file_output)
        os.rename(file_suara, nama_file_output)

def buat_beberapa_gambar(judul: str, jumlah: int = 6):
    file_gambar_list = []
    warna_dasar = [(15, 23, 42), (30, 41, 59), (24, 28, 36), (38, 38, 38), (17, 24, 39), (31, 41, 55)]
    for i in range(jumlah):
        file_img = f"bg_scene_{i}.jpg"
        try:
            result = client.models.generate_images(
                model='imagen-3.0-generate-002',
                prompt=f"Cinematic minimalist background representing theme part {i+1} of '{judul}', high quality, atmospheric, no text",
                config=dict(number_of_images=1, output_mime_type="image/jpeg")
            )
            for generated_image in result.generated_images:
                image = Image.open(BytesIO(generated_image.image.image_bytes))
                image.save(file_img)
                file_gambar_list.append(file_img)
                break
        except:
            pass
        if len(file_gambar_list) <= i:
            img = Image.new('RGB', (1280, 720), color=warna_dasar[i % len(warna_dasar)])
            img.save(file_img)
            file_gambar_list.append(file_img)
    return file_gambar_list

async def buat_video_mp4_dinamis(file_audio: str, judul_buku: str, file_video_output: str):
    audio_clip = AudioFileClip(file_audio)
    total_durasi = audio_clip.duration
    jumlah_scene = 6
    file_gambar_list = buat_beberapa_gambar(judul_buku, jumlah_scene)
    durasi_per_scene = total_durasi / jumlah_scene
    klip_scene = [ImageClip(img).set_duration(durasi_per_scene).resize(lambda t: 1.0 + 0.05 * (t / durasi_per_scene)) for img in file_gambar_list]
    video_gabungan = concatenate_videoclips(klip_scene, method="compose")
    video_final = video_gabungan.set_audio(audio_clip)
    video_final.write_videofile(file_video_output, fps=24, codec='libx264', audio_codec='aac', preset='ultrafast', logger=None)
    audio_clip.close()
    video_final.close()

async def mulai_atau_tes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📚 **Bot Cloud 24/7 Aktif!**\n\nSilakan kirim file PDF buku Anda langsung ke chat ini.")

async def terima_file_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global buku_path_tersimpan
    doc = update.message.document
    if not doc.file_name.endswith('.pdf'):
        await update.message.reply_text("⚠️ Harap kirimkan file berformat PDF.")
        return
    await update.message.reply_text("📥 Menerima file PDF buku Anda...")
    file_telegram = await context.bot.get_file(doc.file_id)
    buku_path_tersimpan = "buku_user.pdf"
    await file_telegram.download_to_drive(buku_path_tersimpan)
    keyboard = [[InlineKeyboardButton("🎬 Mulai Rangkum & Buat Video", callback_data="proses_pdf")]]
    await update.message.reply_text(f"✅ File PDF **{doc.file_name}** berhasil diterima!\n\n👇 Klik tombol di bawah:", reply_markup=InlineKeyboardMarkup(keyboard))

async def tombol_pdf_diklik(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    if not buku_path_tersimpan or not os.path.exists(buku_path_tersimpan):
        await query.edit_message_text(text="⚠️ File PDF tidak ditemukan. Kirim ulang file PDF Anda.")
        return
    await query.edit_message_text(text="🧠 Gemini sedang membaca & menganalisis seluruh halaman buku Anda...")
    try:
        teks_ai = await asyncio.to_thread(baca_dan_rangkum_pdf, buku_path_tersimpan)
        pecah = teks_ai.split("\n", 1)
        mood = pecah[0].strip().lower()
        naskah = pecah[1].strip() if len(pecah) > 1 else pecah[0]
        if mood not in ["inspirational", "calm", "serious"]:
            mood = "inspirational"
        await context.bot.send_message(chat_id=chat_id, text="🎙️ Merekam suara narator dengan tempo tenang...")
        file_audio = "ringkasan_audio.mp3"
        await buat_audio_lengkap(naskah, mood, file_audio)
        await context.bot.send_message(chat_id=chat_id, text="🎬 Merender Video MP4 berdurasi penuh...")
        file_video = "video_full_buku.mp4"
        await buat_video_mp4_dinamis(file_audio, "Buku Pilihan Anda", file_video)
        with open(file_video, "rb") as video:
            await context.bot.send_video(chat_id=chat_id, video=video, caption="🎉 **Video Summary Full Buku Selesai!**")
    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"⚠️ Gagal memproses buku: {e}")

async def jalankan_bot():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler(["start", "tes"], mulai_atau_tes))
    app.add_handler(MessageHandler(filters.Document.ALL, terima_file_pdf))
    app.add_handler(CallbackQueryHandler(tombol_pdf_diklik))
    print("🤖 BOT CLOUD 24/7 SUDAH HIDUP!")
    app.run_polling()

if __name__ == "__main__":
    asyncio.run(jalankan_bot())
