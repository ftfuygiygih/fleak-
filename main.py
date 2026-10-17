import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto, InputMediaVideo
import sqlite3
import yt_dlp
import os
import threading
import time
import shutil
import html
from flask import Flask

# --- НАСТРОЙКИ ---
BOT_TOKEN = '8676459272:AAGe5DfsAEzJEFlLCHRrNJcvXX_f226EhYg'
ADMIN_ID = 8705859046

bot = telebot.TeleBot(BOT_TOKEN)

# --- ИНИЦИАЛИЗАЦИЯ БАЗЫ ---
conn = sqlite3.connect('bot_data.db', check_same_thread=False)
cursor = conn.cursor()
cursor.execute('CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY)')
cursor.execute('CREATE TABLE IF NOT EXISTS ads (id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT, photo_id TEXT)')
cursor.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)')
conn.commit()

# Временное хранилище ссылок (user_id -> url)
user_links = {}

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def check_subscription(user_id):
    cursor.execute("SELECT value FROM settings WHERE key='channel_id'")
    row = cursor.fetchone()
    if not row: return True
    try:
        status = bot.get_chat_member(row[0], user_id).status
        return status in ['member', 'administrator', 'creator']
    except: return False

def add_user(user_id):
    cursor.execute('INSERT OR IGNORE INTO users (user_id) VALUES (?)', (user_id,))
    conn.commit()

# --- АДМИНКА (РЕКЛАМА И КАНАЛ) ---
@bot.message_handler(commands=['channel'], func=lambda m: m.from_user.id == ADMIN_ID)
def cmd_channel(message):
    msg = bot.send_message(message.chat.id, "📢 Введи ID канала и ссылку через пробел.\nПример: `@mychannel https://t.me/mychannel`")
    bot.register_next_step_handler(msg, lambda m: [cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", ('channel_id', m.text.split()[0])), cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", ('channel_url', m.text.split()[1])), conn.commit(), bot.send_message(m.chat.id, "✅ Канал установлен!")] if len(m.text.split()) == 2 else bot.send_message(m.chat.id, "❌ Ошибка формата."))

@bot.message_handler(commands=['add'], func=lambda m: m.from_user.id == ADMIN_ID)
def cmd_add_ad(message):
    msg = bot.send_message(message.chat.id, "📝 Напиши текст рекламы:")
    bot.register_next_step_handler(msg, lambda m: bot.register_next_step_handler(bot.send_message(m.chat.id, "📸 Теперь отправь фото:"), lambda photo_msg: [cursor.execute('INSERT INTO ads (text, photo_id) VALUES (?, ?)', (m.text, photo_msg.photo[-1].file_id)), conn.commit(), bot.send_message(m.chat.id, "✅ Сохранено! /all")]) if m.text else bot.send_message(m.chat.id, "❌ Ошибка."))

@bot.message_handler(commands=['all'], func=lambda m: m.from_user.id == ADMIN_ID)
def cmd_all_ads(message):
    for ad_id, text in cursor.execute('SELECT id, text FROM ads').fetchall():
        markup = InlineKeyboardMarkup().add(
            InlineKeyboardButton("🚀 Сейчас", callback_data=f"sendnow_{ad_id}"),
            InlineKeyboardButton("🗑 Удал.", callback_data=f"delad_{ad_id}")
        )
        bot.send_message(message.chat.id, f"📌 Пост #{ad_id}\n{text[:50]}...", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.from_user.id == ADMIN_ID and call.data.startswith('delad_'))
def del_ad(call):
    cursor.execute('DELETE FROM ads WHERE id = ?', (call.data.split('_')[1],)); conn.commit()
    bot.delete_message(call.message.chat.id, call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.from_user.id == ADMIN_ID and call.data.startswith('sendnow_'))
def send_now(call):
    bot.answer_callback_query(call.id, "Рассылка начата...")
    ad = cursor.execute('SELECT text, photo_id FROM ads WHERE id = ?', (call.data.split('_')[1],)).fetchone()
    if ad: threading.Thread(target=lambda: [bot.send_photo(u[0], ad[1], caption=ad[0]) for u in cursor.execute('SELECT user_id FROM users').fetchall()]).start()


# --- КЛИЕНТСКАЯ ЧАСТЬ И СКАЧИВАНИЕ ---
@bot.message_handler(commands=['start'])
def send_welcome(message):
    add_user(message.from_user.id)
    bot.reply_to(message, "👋 Кидай мне любую ссылку (TikTok, YouTube, Reels, Pinterest) — я вытяну контент!")

@bot.callback_query_handler(func=lambda call: call.data == "check_sub")
def verify_sub(call):
    if check_subscription(call.from_user.id):
        bot.answer_callback_query(call.id, "✅ Доступ открыт!", show_alert=True)
        bot.delete_message(call.message.chat.id, call.message.message_id)
    else: bot.answer_callback_query(call.id, "❌ Ты еще не подписался!", show_alert=True)

@bot.message_handler(func=lambda message: 'http' in message.text)
def handle_link(message):
    user_id = message.from_user.id
    add_user(user_id)
    
    if not check_subscription(user_id):
        url_row = cursor.execute("SELECT value FROM settings WHERE key='channel_url'").fetchone()
        markup = InlineKeyboardMarkup().add(InlineKeyboardButton("🔥 Подписаться", url=url_row[0] if url_row else "https://t.me/"), InlineKeyboardButton("🔄 Проверить", callback_data="check_sub"))
        bot.send_message(message.chat.id, "Что бы продолжить, подпишитесь на канал 👇", reply_markup=markup)
        return

    user_links[user_id] = message.text.strip()
    markup = InlineKeyboardMarkup().add(
        InlineKeyboardButton("🎬 Видео / Карусель", callback_data="dl_vid"),
        InlineKeyboardButton("🎵 Только звук (MP3)", callback_data="dl_aud")
    )
    bot.reply_to(message, "В каком формате вытянуть файл?", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data in ["dl_vid", "dl_aud"])
def process_download(call):
    user_id = call.from_user.id
    url = user_links.get(user_id)
    if not url:
        bot.answer_callback_query(call.id, "❌ Ссылка устарела, отправь заново.")
        return

    bot.delete_message(call.message.chat.id, call.message.message_id)
    status_msg = bot.send_message(call.message.chat.id, "⏳ <code>Инициализация...</code>", parse_mode="HTML")
    
    # Запускаем загрузку в отдельном потоке
    threading.Thread(target=execute_download, args=(call.message.chat.id, status_msg.message_id, url, call.data)).start()

def execute_download(chat_id, msg_id, url, format_type):
    task_id = str(int(time.time()))
    download_dir = f"downloads/{task_id}"
    os.makedirs(download_dir, exist_ok=True)
    
    last_update = 0
    # Прогресс-бар для Телеграма (обновляется раз в 3 сек)
    def progress_hook(d):
        nonlocal last_update
        if d['status'] == 'downloading':
            now = time.time()
            if now - last_update > 3:
                last_update = now
                percent = d.get('_percent_str', '0%').strip()
                try: bot.edit_message_text(f"📥 <code>Скачиваю: {percent}</code>", chat_id, msg_id, parse_mode="HTML")
                except: pass

    is_audio = (format_type == "dl_aud")
    ydl_opts = {
        'outtmpl': f'{download_dir}/%(id)s_%(autonumber)s.%(ext)s',
        'quiet': True,
        'nocheckcertificate': True,
        'progress_hooks': [progress_hook],
        # Маскировка под реальный браузер (защита от 403 Forbidden)
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
        }
    }
    
    if is_audio:
        ydl_opts['format'] = 'bestaudio/best'
        ydl_opts['postprocessors'] = [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}]
    else:
        ydl_opts['format'] = 'best[ext=mp4]/best'

    try:
        bot.edit_message_text("🔄 <code>Подключение к серверу...</code>", chat_id, msg_id, parse_mode="HTML")
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(url, download=True)

        bot.edit_message_text("📤 <code>Отправка файла в Telegram...</code>", chat_id, msg_id, parse_mode="HTML")
        
        files = [os.path.join(download_dir, f) for f in os.listdir(download_dir)]
        files.sort()

        if not files: raise Exception("Библиотека не смогла вытянуть файл (нет доступа или пустая ссылка).")

        if len(files) == 1:
            # Одиночный файл
            with open(files[0], 'rb') as f:
                if is_audio: bot.send_audio(chat_id, f)
                elif files[0].endswith(('.mp4', '.mkv', '.webm')): bot.send_video(chat_id, f)
                else: bot.send_photo(chat_id, f)
        else:
            # Карусель
            media_group, open_files = [], []
            for f in files[:10]:
                file_obj = open(f, 'rb')
                open_files.append(file_obj)
                if f.endswith(('.mp4', '.webm')): media_group.append(InputMediaVideo(file_obj))
                else: media_group.append(InputMediaPhoto(file_obj))
            
            bot.send_media_group(chat_id, media_group)
            for f_obj in open_files: f_obj.close()

        bot.delete_message(chat_id, msg_id)

    except Exception as e:
        # Экранируем спецсимволы, чтобы лог не сломал HTML-разметку Телеграма
        error_msg = html.escape(str(e))
        # Обрезаем ошибку, если она слишком длинная (лимит ТГ)
        if len(error_msg) > 800: error_msg = error_msg[:800] + "..."
        
        bot.edit_message_text(
            f"❌ <b>Ошибка скачивания:</b>\n\n<code>{error_msg}</code>\n\n<i>Перешлите эту ошибку админу, если не понимаете в чем дело.</i>", 
            chat_id, 
            msg_id, 
            parse_mode="HTML"
        )
        print(f"Ошибка: {e}")
    finally:
        # Очистка диска
        if os.path.exists(download_dir):
            shutil.rmtree(download_dir)

# --- МИКРО-СЕРВЕР ДЛЯ RENDER ---
app = Flask(__name__)
@app.route('/')
def keep_alive(): return "Бот работает!"

if __name__ == '__main__':
    threading.Thread(target=lambda: app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))).start()
    print("Бот запущен!")
    bot.infinity_polling()
