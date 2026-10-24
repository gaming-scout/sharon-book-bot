import asyncio
import os
import time
import nest_asyncio
import requests
from PyPDF2 import PdfReader
from PIL import Image
if not hasattr(Image, 'ANTIALIAS'):
    Image.ANTIALIAS = Image.LANCZOS
from moviepy.editor import ImageClip, AudioFileClip, concatenate_videoclips
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from openai import OpenAI

# Terapkan nest_asyncio agar tidak terjadi konflik event loop di background
nest_asyncio.apply()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
VOICE_ID = os.getenv("VOICE_ID")

client = OpenAI(api_key=OPENAI_API_KEY)
buku_path_tersimpan = None

def baca_dan_rangkum_pdf(file_path):
    print("Membaca teks dari file PDF lokal...")
    reader = PdfReader(file_path)
    teks_lengkap = ""
    
    # Mengekstrak maksimal 100 halaman pertama agar efisien & aman dari limit
    for i, page in enumerate(reader.pages):
        if i >= 100:
            break
        teks_halaman = page.extract_text()
        if teks_halaman:
            teks_lengkap += teks_halaman + "\n"

    prompt = f"""
    Analisis buku berikut ini secara mendalam. Buatlah naskah voiceover Bahasa Indonesia yang detail dan komprehensif sebagai rangkuman utuh buku ini.
    Aturan Naskah:
    1. Panjang naskah 500 - 700 kata agar durasi pembacaan narasi pas dan berbobot.
    2. Struktur: Pengantar/Premis Buku, Bedah Bab Utama & Konsep Inti, Studi Kasus/Pelajaran Nyata, Kesimpulan Reflektif.
    3. Gunakan bahasa yang mengalir, elegan, dan mudah dipahami.
    4. JANGAN menulis keterangan [Musik masuk], tanda bintang (*), atau pagar (#). Tulis HANYA teks narasi bersih.
    5. Di baris paling pertama, tulis salah satu mood musik ini saja: INSPIRATIONAL atau CALM atau SERIOUS, lalu ganti baris untuk isi naskahnya.
    
    Isi Buku (Teks Ekstraksi):
    {teks_lengkap[:100000]}
    """
    
    print("Mengirim teks ke OpenAI (GPT-4o-Mini) untuk dirangkum...")
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Anda adalah asisten penulis naskah video profesional dan ahli bedah buku."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        raise Exception(f"Gagal memproses AI OpenAI: {e}")

async def buat_audio_lengkap(naskah: str, nama_file_output: str):
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

def buat_beberapa_gambar(jumlah: int = 6):
    # Karena Gemini Imagen dihapus, kita menggunakan background estetik warna solid yang bersih
    file_gambar_list = []
    warna_cadangan = [(30, 58, 138), (76, 29, 149), (15, 118, 110), (51, 65, 85), (124, 45, 18), (67, 56, 202)]
    
    for i in range(jumlah):
        file_img = f"bg_scene_{i}.jpg"
        img = Image.new('RGB', (1280, 720), color=warna_cadangan[i % len(warna_cadangan)])
        img.save(file_img)
        file_gambar_list.append(file_img)
            
    return file_gambar_list

async def buat_video_mp4_dinamis(file_audio: str, file_video_output: str):
    audio_clip = AudioFileClip(file_audio)
    total_durasi = audio_clip.duration
    jumlah_scene = 6
    file_gambar_list = buat_beberapa_gambar(jumlah_scene)
    durasi_per_scene = total_durasi / jumlah_scene
    klip_scene = [ImageClip(img).set_duration(durasi_per_scene) for img in file_gambar_list]
    video_gabungan = concatenate_videoclips(klip_scene, method="compose")
    video_final = video_gabungan.set_audio(audio_clip)
    video_final.write_videofile(file_video_output, fps=24, codec='libx264', audio_codec='aac', preset='ultrafast', logger=None)
    audio_clip.close()
    video_final.close()

async def mulai_atau_tes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📚 **Bot (Powered by ChatGPT) Aktif!**\n\nSilakan kirim file PDF buku Anda ke sini.")

async def terima_file_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global buku_path_tersimpan
    doc = update.message.document
    if not doc or not doc.file_name.lower().endswith('.pdf'):
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
    await query.edit_message_text(text="🧠 ChatGPT sedang menganalisis halaman buku Anda (proses ini mungkin butuh 1-2 menit)...")
    try:
        teks_ai = await asyncio.to_thread(baca_dan_rangkum_pdf, buku_path_tersimpan)
        pecah = teks_ai.split("\n", 1)
        naskah = pecah[1].strip() if len(pecah) > 1 else pecah[0]
        
        await context.bot.send_message(chat_id=chat_id, text="🎙️ Merekam suara narator...")
        file_audio = "ringkasan_audio.mp3"
        await buat_audio_lengkap(naskah, file_audio)
        
        await context.bot.send_message(chat_id=chat_id, text="🎬 Merender Video MP4 berdurasi penuh...")
        file_video = "video_full_buku.mp4"
        await buat_video_mp4_dinamis(file_audio, file_video)
        
        with open(file_video, "rb") as video:
            await context.bot.send_video(chat_id=chat_id, video=video, caption="🎉 **Video Summary Full Buku Selesai!**")
    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"⚠️ Gagal memproses buku: {e}")

def jalankan_bot():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", mulai_atau_tes))
    app.add_handler(MessageHandler(filters.Document.PDF, terima_file_pdf))
    app.add_handler(CallbackQueryHandler(tombol_pdf_diklik))
    print("🤖 BOT CLOUD SIAP MENERIMA PDF!")
    app.run_polling()

if __name__ == "__main__":
    jalankan_bot()
