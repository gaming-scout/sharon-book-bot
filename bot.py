import asyncio
import datetime
import os
import time
import pytz
import nest_asyncio
import requests
from io import BytesIO
from PIL import Image
from moviepy.editor import ImageClip, AudioFileClip, concatenate_videoclips
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from google import genai
from google.genai import types

nest_asyncio.apply()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
VOICE_ID = os.getenv("VOICE_ID")

client = genai.Client(api_key=GEMINI_API_KEY)
buku_path_tersimpan = None

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
    model_list = ["gemini-2.5-flash", "gemini-1.5-flash"]
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
    berhasil_eleven = False

    if ELEVENLABS_API_KEY and VOICE_ID:
        try:
            url_eleven = f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE_ID}"
            headers = {
                "Accept": "audio/mpeg",
                "Content-Type": "application/json",
                "xi-api-key": ELEVENLABS_API_KEY
            }
            data = {
                "text": naskah,
                "model_id": "eleven_multilingual_v2",
                "voice_settings": {
                    "stability": 0.45,
                    "similarity_boost": 0.8,
                    "style": 0.2,
                    "use_speaker_boost": True
                }
            }
            res = requests.post(url_eleven, json=data, headers=headers, timeout=60)
            if res.status_code == 200:
                with open(file_suara, "wb") as f:
                    f.write(res.content)
                berhasil_eleven = True
        except Exception as e:
            print(f"Info ElevenLabs: {e}")

    if not berhasil_eleven:
        import edge_tts
        komunikasi_tts = edge_tts.Communicate(naskah, voice="id-ID-ArdiNeural", rate="-5%")
        await komunikasi_tts.save(file_suara)

    if os.path.exists(nama_file_output):
        os.remove(nama_file_output)
    os.rename(file_suara, nama_file_output)

def buat_beberapa_gambar(judul: str, jumlah: int = 6):
    file_gambar_list = []
    warna_cadangan = [(30, 58, 138), (76, 29, 149), (15, 118, 110), (51, 65, 85), (124, 45, 18), (67, 56, 202)]
    
    for i in range(jumlah):
        file_img = f"bg_scene_{i}.jpg"
        sukses_generate = False
        try:
            prompt_img = f"Bright cinematic aesthetic background representing conceptual theme part {i+1} for book summary, vibrant lighting, high quality, colorful, beautiful scenery, no text"
            result = client.models.generate_images(
                model='imagen-3.0-generate-002',
                prompt=prompt_img,
                config=dict(number_of_images=1, output_mime_type="image/jpeg")
            )
            for generated_image in result.generated_images:
                image = Image.open(BytesIO(generated_image.image.image_bytes))
                image.save(file_img)
                file_gambar_list.append(file_img)
                sukses_generate = True
                break
        except:
            pass
        
        if not sukses_generate:
            img = Image.new('RGB', (1280, 720), color=warna_cadangan[i % len(warna_cadangan)])
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
    await update.message.reply_text("📚 **Bot Cloud (ElevenLabs & Bersih Error) Aktif!**\n\nSilakan kirim file PDF buku Anda langsung ke chat ini.")

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
        await context.bot.send_message(chat_id=chat_id, text="🎙️ Merekam suara narator...")
        file_audio = "ringkasan_audio.mp3"
        await buat_audio_lengkap(naskah, mood, file_audio)
        await context.bot.send_message(chat_id=chat_id, text="🎬 Merender Video MP4 berdurasi penuh dengan latar belakang dinamis...")
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
    print("🤖 BOT CLOUD BERSIH SUDAH HIDUP!")
    app.run_polling()

if __name__ == "__main__":
    asyncio.run(jalankan_bot())
