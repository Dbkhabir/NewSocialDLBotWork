import os
import logging
import requests
import json
import time
import tempfile
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
from flask import Flask
from threading import Thread

# ==================== Logging ====================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ==================== Config ====================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ZYLA_API_KEY = os.environ.get("ZYLA_API_KEY", "12368|FQZM8X1GtUdl98NngHB9tcM2Ff5caNkaoyiXAF7E")
PORT = int(os.environ.get("PORT", 10000))
BOT_USERNAME = "@NewSocialDLBot"
BOT_VERSION = "3.2"
DEVELOPER = "@peranabik"
ZYLA_API_URL = "https://zylalabs.com/api/4146/facebook+download+api/7134/downloader"

# Limits for Render Free Plan
MAX_DOWNLOAD_SIZE = 50 * 1024 * 1024  # 50MB - server download limit
DOWNLOAD_TIMEOUT = 120  # 2 min max download time
UPLOAD_TIMEOUT = 120    # 2 min max upload time

# ==================== Flask ====================
app_flask = Flask(__name__)

@app_flask.route("/")
def home():
    return "Bot is alive!", 200

@app_flask.route("/health")
def health():
    return "OK", 200

def run_flask():
    app_flask.run(host="0.0.0.0", port=PORT)

# ==================== Helpers ====================

def is_facebook_url(url):
    domains = ["facebook.com", "fb.com", "fb.watch", "m.facebook.com", "web.facebook.com"]
    return any(d in url.lower() for d in domains)

def fetch_video_data(fb_url):
    headers = {"Authorization": f"Bearer {ZYLA_API_KEY}", "Content-Type": "application/json"}
    try:
        r = requests.post(ZYLA_API_URL, headers=headers, data=json.dumps({"url": fb_url}), timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.error(f"API: {e}")
        return None

def fmt_dur(ms):
    if not ms: return "N/A"
    s = ms // 1000
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{h}h {m}m {s}s" if h else (f"{m}m {s}s" if m else f"{s}s")

def q_icon(q):
    return {"HD": "🔵", "SD": "🟢", "Audio": "🟣"}.get(q, "⚪")

def get_size(url):
    try:
        r = requests.head(url, allow_redirects=True, timeout=10)
        return int(r.headers.get("content-length", 0))
    except:
        return 0

def fmt_size(b):
    if b <= 0: return "Unknown"
    for u in ["B", "KB", "MB", "GB"]:
        if b < 1024: return f"{b:.1f} {u}"
        b /= 1024
    return f"{b:.1f} TB"

def cleanup(p):
    try:
        if p and os.path.exists(p): os.remove(p)
    except: pass

def download_with_limit(url, ext="mp4", max_size=MAX_DOWNLOAD_SIZE, timeout=DOWNLOAD_TIMEOUT):
    """ফাইল ডাউনলোড করে - সাইজ ও টাইম লিমিট সহ"""
    try:
        start = time.time()
        r = requests.get(url, stream=True, timeout=30)
        r.raise_for_status()

        # Check content-length header first
        content_length = int(r.headers.get("content-length", 0))
        if content_length > max_size:
            logger.info(f"File too large: {fmt_size(content_length)} > {fmt_size(max_size)}")
            r.close()
            return None, content_length, "too_large"

        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=f".{ext}", dir=tempfile.gettempdir())
        downloaded = 0

        for chunk in r.iter_content(chunk_size=512 * 1024):  # 512KB chunks
            if chunk:
                # Check time limit
                if time.time() - start > timeout:
                    tmp.close()
                    cleanup(tmp.name)
                    logger.warning("Download timeout!")
                    return None, downloaded, "timeout"

                # Check size limit
                if downloaded + len(chunk) > max_size:
                    tmp.close()
                    cleanup(tmp.name)
                    logger.info(f"Download exceeded limit at {fmt_size(downloaded)}")
                    return None, downloaded, "too_large"

                tmp.write(chunk)
                downloaded += len(chunk)

        tmp.close()
        logger.info(f"Downloaded {fmt_size(downloaded)} in {time.time()-start:.1f}s")
        return tmp.name, downloaded, "ok"

    except Exception as e:
        logger.error(f"Download error: {e}")
        return None, 0, "error"

# ==================== Upload System ====================

async def smart_send(ctx, chat_id, url, mtype, qual, vdata, ext, file_size, status_cb=None):
    """
    Smart 3-tier upload:
    1. URL direct (≤20MB fast)
    2. Download + Upload (≤50MB)
    3. Direct link button (>50MB)
    """

    icon = q_icon(qual) if mtype == "video" else "🎵"
    size_label = fmt_size(file_size)

    caption = (
        f"✅ **Download Complete!**\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 {vdata['title']}\n"
        f"👤 {vdata['author']}\n"
        f"{icon} Quality: **{qual}**\n"
        f"📦 Size: **{size_label}**\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"⚡ {BOT_USERNAME}"
    )

    # ===== TIER 1: Direct URL (fastest, Telegram fetches the file) =====
    if status_cb:
        await status_cb(f"⚡ **Sending directly...**\n📦 {size_label}")

    try:
        if mtype == "video":
            await asyncio.wait_for(
                ctx.bot.send_video(
                    chat_id=chat_id, video=url, caption=caption,
                    parse_mode="Markdown", supports_streaming=True,
                    read_timeout=60, write_timeout=60,
                ),
                timeout=90
            )
        else:
            await asyncio.wait_for(
                ctx.bot.send_audio(
                    chat_id=chat_id, audio=url, caption=caption,
                    parse_mode="Markdown",
                    read_timeout=60, write_timeout=60,
                ),
                timeout=90
            )
        return True, "direct"
    except asyncio.TimeoutError:
        logger.warning("Tier 1 timeout")
    except Exception as e:
        logger.info(f"Tier 1 fail: {e}")

    # ===== Check if file is too large for server download =====
    if file_size > MAX_DOWNLOAD_SIZE:
        logger.info(f"File {size_label} exceeds server limit, giving direct link")
        return False, "too_large"

    # ===== TIER 2: Download to server + Upload =====
    if status_cb:
        await status_cb(f"📥 **Downloading to server...**\n📦 {size_label}\n⏳ Please wait...")

    path, actual_size, dl_status = download_with_limit(url, ext)

    if dl_status != "ok" or not path:
        logger.warning(f"Download failed: {dl_status}")
        return False, dl_status

    actual_size_label = fmt_size(actual_size)
    if status_cb:
        await status_cb(f"📤 **Uploading to Telegram...**\n📦 {actual_size_label}\n⏳ Almost done!")

    # Try send as video/audio
    try:
        with open(path, "rb") as f:
            if mtype == "video":
                await asyncio.wait_for(
                    ctx.bot.send_video(
                        chat_id=chat_id, video=f, caption=caption,
                        parse_mode="Markdown", supports_streaming=True,
                        filename=f"FB_{qual}_{int(time.time())}.{ext}",
                        read_timeout=UPLOAD_TIMEOUT, write_timeout=UPLOAD_TIMEOUT,
                    ),
                    timeout=UPLOAD_TIMEOUT + 30
                )
            else:
                await asyncio.wait_for(
                    ctx.bot.send_audio(
                        chat_id=chat_id, audio=f, caption=caption,
                        parse_mode="Markdown",
                        filename=f"FB_Audio_{int(time.time())}.{ext}",
                        read_timeout=UPLOAD_TIMEOUT, write_timeout=UPLOAD_TIMEOUT,
                    ),
                    timeout=UPLOAD_TIMEOUT + 30
                )
        cleanup(path)
        return True, "upload"
    except asyncio.TimeoutError:
        logger.warning("Upload as media timeout")
    except Exception as e:
        logger.warning(f"Media upload fail: {e}")

    # Try as document
    try:
        if status_cb:
            await status_cb(f"📄 **Sending as document...**\n📦 {actual_size_label}")

        with open(path, "rb") as f:
            await asyncio.wait_for(
                ctx.bot.send_document(
                    chat_id=chat_id, document=f, caption=caption,
                    parse_mode="Markdown",
                    filename=f"Facebook_{qual}_{int(time.time())}.{ext}",
                    read_timeout=UPLOAD_TIMEOUT, write_timeout=UPLOAD_TIMEOUT,
                ),
                timeout=UPLOAD_TIMEOUT + 30
            )
        cleanup(path)
        return True, "document"
    except asyncio.TimeoutError:
        logger.warning("Document upload timeout")
    except Exception as e:
        logger.error(f"Doc upload fail: {e}")

    cleanup(path)
    return False, "upload_fail"

# ==================== Commands ====================

async def set_cmds(app):
    await app.bot.set_my_commands([
        BotCommand("start", "🚀 Start the bot"),
        BotCommand("help", "📖 How to use this bot"),
        BotCommand("about", "ℹ️ About this bot"),
        BotCommand("supported", "📋 Supported link types"),
        BotCommand("stats", "📊 Your usage stats"),
        BotCommand("ping", "🏓 Check bot status"),
        BotCommand("developer", "👨‍💻 Developer info"),
        BotCommand("privacy", "🔒 Privacy policy"),
    ])

async def start_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if "downloads" not in ctx.user_data:
        ctx.user_data["downloads"] = 0
        ctx.user_data["joined"] = time.strftime("%Y-%m-%d")

    txt = (
        f"Hey **{u.first_name}**! 👋\n\n"
        f"🎬 **Facebook Video Downloader**\n\n"
        f"Download videos, reels & audio from\n"
        f"Facebook — fast, free & easy!\n\n"
        f"╔══════════════════════╗\n"
        f"║ 🔹 Send a FB video link     ║\n"
        f"║ 🔹 Choose quality               ║\n"
        f"║ 🔹 Get your file!                  ║\n"
        f"╚══════════════════════╝\n\n"
        f"📦 Small files → Sent directly\n"
        f"📦 Large files → Download link provided\n\n"
        f"💡 Send a link to get started!\n\n"
        f"🤖 {BOT_USERNAME} • v{BOT_VERSION}"
    )
    kb = [
        [InlineKeyboardButton("📖 How to Use", callback_data="cb_help"),
         InlineKeyboardButton("📋 Supported", callback_data="cb_supported")],
        [InlineKeyboardButton("ℹ️ About", callback_data="cb_about"),
         InlineKeyboardButton("🏓 Ping", callback_data="cb_ping")],
        [InlineKeyboardButton("👨‍💻 Developer", callback_data="cb_dev"),
         InlineKeyboardButton("🔒 Privacy", callback_data="cb_privacy")],
    ]
    await update.message.reply_text(txt, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

async def help_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = (
        "📖 **How to Use This Bot**\n\n"
        "**Step 1️⃣** — Find a video on Facebook\n"
        "**Step 2️⃣** — Tap `Share` → `Copy Link`\n"
        "**Step 3️⃣** — Paste the link here\n"
        "**Step 4️⃣** — Select quality (HD/SD/Audio)\n"
        "**Step 5️⃣** — Receive your file! 🎉\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "📌 **Commands:**\n\n"
        "┌ /start — 🚀 Start the bot\n"
        "├ /help — 📖 How to use\n"
        "├ /about — ℹ️ About bot\n"
        "├ /supported — 📋 Supported links\n"
        "├ /stats — 📊 Your stats\n"
        "├ /ping — 🏓 Bot status\n"
        "├ /developer — 👨‍💻 Developer\n"
        "└ /privacy — 🔒 Privacy\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "📦 **File Size Info:**\n"
        "• ≤50MB → Sent directly in chat\n"
        "• >50MB → Download link button\n\n"
        f"🤖 {BOT_USERNAME} • v{BOT_VERSION}"
    )
    await update.message.reply_text(txt, parse_mode="Markdown")

async def about_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = (
        "ℹ️ **About This Bot**\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "🎬 **Facebook Video Downloader Bot**\n\n"
        "A fast Telegram bot that downloads\n"
        "videos, reels and audio from Facebook.\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🤖 **Bot:** {BOT_USERNAME}\n"
        f"📌 **Version:** {BOT_VERSION}\n"
        f"👨‍💻 **Developer:** {DEVELOPER}\n"
        "🔧 **Language:** Python 3.11\n"
        "🌐 **API:** ZylaLabs\n"
        "☁️ **Hosting:** Render\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "🎯 **Features:**\n\n"
        "┌ 📹 FB Videos & Reels\n"
        "├ 🔵 HD / 🟢 SD Quality\n"
        "├ 🎵 Audio Extraction\n"
        "├ 🖼️ Thumbnail Preview\n"
        "├ 📦 File Size Detection\n"
        "├ ⚡ Fast & Reliable\n"
        "├ 🔗 Direct Download Links\n"
        "├ 📊 Download Stats & Ranks\n"
        "├ 🔒 Privacy Focused\n"
        "└ 🆓 100% Free Forever\n\n"
        f"Made with ❤️ by {DEVELOPER}"
    )
    await update.message.reply_text(txt, parse_mode="Markdown")

async def supported_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = (
        "📋 **Supported Link Types**\n\n"
        "✅ **Works:**\n"
        "┌ 🔗 `facebook.com/watch/...`\n"
        "├ 🔗 `facebook.com/reel/...`\n"
        "├ 🔗 `facebook.com/video/...`\n"
        "├ 🔗 `facebook.com/share/v/...`\n"
        "├ 🔗 `fb.watch/...`\n"
        "├ 🔗 `m.facebook.com/...`\n"
        "└ 🔗 `web.facebook.com/...`\n\n"
        "❌ **Doesn't Work:**\n"
        "┌ 🚫 Private videos\n"
        "├ 🚫 Live streams\n"
        "├ 🚫 Stories\n"
        "└ 🚫 Other platforms\n\n"
        f"🤖 {BOT_USERNAME}"
    )
    await update.message.reply_text(txt, parse_mode="Markdown")

async def stats_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    dl = ctx.user_data.get("downloads", 0)
    joined = ctx.user_data.get("joined", "Today")
    ranks = [(0,"🌱 Newbie"),(1,"⭐ Starter"),(5,"🔥 Regular"),
             (15,"💎 Pro"),(30,"👑 Master"),(50,"🏆 Legend")]
    rank = "🌱 Newbie"
    for threshold, r in ranks:
        if dl >= threshold: rank = r

    txt = (
        "📊 **Your Stats**\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 **User:** {u.first_name}\n"
        f"🆔 **ID:** `{u.id}`\n"
        f"📅 **Since:** {joined}\n"
        f"📥 **Downloads:** {dl}\n"
        f"🏅 **Rank:** {rank}\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "🏅 **Ranks:**\n"
        "┌ 🌱 Newbie (0)\n"
        "├ ⭐ Starter (1-4)\n"
        "├ 🔥 Regular (5-14)\n"
        "├ 💎 Pro (15-29)\n"
        "├ 👑 Master (30-49)\n"
        "└ 🏆 Legend (50+)\n\n"
        f"🤖 {BOT_USERNAME}"
    )
    await update.message.reply_text(txt, parse_mode="Markdown")

async def ping_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t1 = time.time()
    msg = await update.message.reply_text("🏓 Pinging...")
    ms = round((time.time() - t1) * 1000)
    st = "🟢 Excellent" if ms < 500 else ("🟡 Good" if ms < 1000 else "🔴 Slow")
    await msg.edit_text(
        f"🏓 **Pong!**\n\n"
        f"⚡ Latency: `{ms}ms`\n"
        f"📶 {st}\n"
        f"🕐 `{time.strftime('%H:%M:%S UTC')}`\n"
        f"📌 v{BOT_VERSION} ✅",
        parse_mode="Markdown")

async def developer_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = (
        "👨‍💻 **Developer Info**\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🧑‍💻 {DEVELOPER}\n"
        f"🤖 {BOT_USERNAME} • v{BOT_VERSION}\n\n"
        "🛠️ **Stack:**\n"
        "┌ 🐍 Python 3.11\n"
        "├ 🤖 python-telegram-bot\n"
        "├ 🌐 ZylaLabs API\n"
        "├ 🐳 Docker\n"
        "└ ☁️ Render\n\n"
        f"💬 Feedback: {DEVELOPER}"
    )
    await update.message.reply_text(txt, parse_mode="Markdown")

async def privacy_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = (
        "🔒 **Privacy Policy**\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "📌 **We collect:** Nothing permanent\n"
        "🚫 **We don't store:** Links, files, data\n"
        "🗑️ **Temp files:** Deleted immediately\n"
        "🔐 **Connection:** HTTPS encrypted\n\n"
        f"Your privacy is safe! ✅\n\n🤖 {BOT_USERNAME}"
    )
    await update.message.reply_text(txt, parse_mode="Markdown")

# ==================== Message Handler ====================

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()

    if not is_facebook_url(url):
        await update.message.reply_text(
            "🚫 **Invalid Link!**\n\n"
            "Send a valid Facebook video/reel link.\n\n"
            "💡 Example:\n`https://www.facebook.com/reel/569975832234512`",
            parse_mode="Markdown")
        return

    if "downloads" not in ctx.user_data:
        ctx.user_data["downloads"] = 0
        ctx.user_data["joined"] = time.strftime("%Y-%m-%d")

    msg = await update.message.reply_text(
        "🔍 **Processing...**\n⏳ Fetching video details.", parse_mode="Markdown")

    data = fetch_video_data(url)

    if not data or data.get("error", True):
        await msg.edit_text(
            "❌ **Video Not Found!**\n\n"
            "┌ 🔒 Might be private\n├ 🗑️ Might be deleted\n└ 🔗 Invalid link\n\n"
            "💡 Check and try again.", parse_mode="Markdown")
        return

    title = data.get("title", "Untitled")
    author = data.get("author", "Unknown")
    dur = fmt_dur(data.get("duration", 0))
    thumb = data.get("thumbnail", "")
    medias = data.get("medias", [])
    vids = [m for m in medias if m.get("type") == "video"]
    auds = [m for m in medias if m.get("type") == "audio"]

    if not vids and not auds:
        await msg.edit_text("❌ No downloadable media found!", parse_mode="Markdown")
        return

    await msg.edit_text("📦 **Checking file sizes...**", parse_mode="Markdown")

    for m in vids + auds:
        s = get_size(m["url"])
        m["size"] = s
        m["size_label"] = fmt_size(s)
        # Mark if file is large
        m["is_large"] = s > MAX_DOWNLOAD_SIZE

    ctx.user_data["video_data"] = {
        "title": title, "author": author,
        "videos": vids, "audios": auds,
        "thumbnail": thumb, "url": url,
    }

    kb = []
    for i, v in enumerate(vids):
        q = v.get("quality", "?")
        ext = v.get("extension", "mp4").upper()
        sl = v.get("size_label", "")
        large_tag = " 🔗" if v.get("is_large") else ""
        st = f" • {sl}{large_tag}" if sl != "Unknown" else large_tag
        kb.append([InlineKeyboardButton(f"{q_icon(q)} {q} ({ext}{st})", callback_data=f"v_{i}")])

    for i, a in enumerate(auds):
        ext = a.get("extension", "mp3").upper()
        sl = a.get("size_label", "")
        large_tag = " 🔗" if a.get("is_large") else ""
        st = f" • {sl}{large_tag}" if sl != "Unknown" else large_tag
        kb.append([InlineKeyboardButton(f"🎵 Audio ({ext}{st})", callback_data=f"a_{i}")])

    kb.append([InlineKeyboardButton("🔗 Open on Facebook", url=url)])

    large_note = ""
    has_large = any(m.get("is_large") for m in vids + auds)
    if has_large:
        large_note = "\n\n💡 🔗 = Large file, download link will be provided"

    info = (
        "✅ **Video Found!**\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 **Title:** {title}\n"
        f"👤 **Author:** {author}\n"
        f"⏱️ **Duration:** {dur}\n"
        f"📦 **Formats:** {len(vids)} video, {len(auds)} audio\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👇 **Select quality:**{large_note}"
    )

    await msg.delete()

    if thumb:
        try:
            await update.message.reply_photo(
                photo=thumb, caption=info,
                reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
            return
        except: pass

    await update.message.reply_text(info, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")

# ==================== Callback ====================

async def button_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    d = q.data

    back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="cb_back")]])

    if d == "cb_help":
        await q.edit_message_text(
            "📖 **How to Use**\n\n"
            "1️⃣ Copy a Facebook video link\n"
            "2️⃣ Paste it here\n3️⃣ Choose quality\n"
            "4️⃣ Get your file! 🎉\n\n"
            f"🤖 {BOT_USERNAME}", parse_mode="Markdown", reply_markup=back_kb)
        return

    if d == "cb_supported":
        await q.edit_message_text(
            "📋 **Supported**\n\n✅ facebook.com/watch\n✅ facebook.com/reel\n"
            "✅ facebook.com/video\n✅ fb.watch\n✅ m.facebook.com\n\n"
            f"❌ Private/Stories/Live\n\n🤖 {BOT_USERNAME}",
            parse_mode="Markdown", reply_markup=back_kb)
        return

    if d == "cb_about":
        await q.edit_message_text(
            f"ℹ️ **About**\n\n🤖 {BOT_USERNAME}\n📌 v{BOT_VERSION}\n"
            f"👨‍💻 {DEVELOPER}\n🆓 Free\n\nMade with ❤️",
            parse_mode="Markdown", reply_markup=back_kb)
        return

    if d == "cb_ping":
        await q.edit_message_text(
            f"🏓 **Pong!**\n🟢 Online\n🕐 {time.strftime('%H:%M:%S UTC')} ✅",
            parse_mode="Markdown", reply_markup=back_kb)
        return

    if d == "cb_dev":
        await q.edit_message_text(
            f"👨‍💻 **Developer**\n\n{DEVELOPER}\n🐍 Python • ☁️ Render",
            parse_mode="Markdown", reply_markup=back_kb)
        return

    if d == "cb_privacy":
        await q.edit_message_text(
            "🔒 **Privacy**\n\n🚫 No data stored\n🗑️ Files deleted instantly\n🔐 HTTPS ✅",
            parse_mode="Markdown", reply_markup=back_kb)
        return

    if d == "cb_back":
        u = update.effective_user
        kb = [
            [InlineKeyboardButton("📖 How to Use", callback_data="cb_help"),
             InlineKeyboardButton("📋 Supported", callback_data="cb_supported")],
            [InlineKeyboardButton("ℹ️ About", callback_data="cb_about"),
             InlineKeyboardButton("🏓 Ping", callback_data="cb_ping")],
            [InlineKeyboardButton("👨‍💻 Developer", callback_data="cb_dev"),
             InlineKeyboardButton("🔒 Privacy", callback_data="cb_privacy")],
        ]
        await q.edit_message_text(
            f"Hey **{u.first_name}**! 👋\n\n🎬 **Facebook Video Downloader**\n\n"
            f"Send any FB link!\n\n🤖 {BOT_USERNAME}",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))
        return

    # === Download ===
    vd = ctx.user_data.get("video_data")
    if not vd:
        await q.answer("⚠️ Session expired! Send link again.", show_alert=True)
        return

    dl_url = None; mtype = None; qual = None; ext = "mp4"; fsize = 0; is_large = False

    if d.startswith("v_"):
        i = int(d.split("_")[1])
        vs = vd.get("videos", [])
        if i < len(vs):
            dl_url = vs[i]["url"]; qual = vs[i].get("quality", "?")
            ext = vs[i].get("extension", "mp4"); mtype = "video"
            fsize = vs[i].get("size", 0); is_large = vs[i].get("is_large", False)

    elif d.startswith("a_"):
        i = int(d.split("_")[1])
        aus = vd.get("audios", [])
        if i < len(aus):
            dl_url = aus[i]["url"]; qual = "Audio"
            ext = aus[i].get("extension", "mp3"); mtype = "audio"
            fsize = aus[i].get("size", 0); is_large = aus[i].get("is_large", False)

    if not dl_url:
        await q.answer("❌ Link not found!", show_alert=True)
        return

    icon = q_icon(qual) if mtype == "video" else "🎵"
    size_label = fmt_size(fsize)

    # If file is known to be large, skip server download and give link directly
    if is_large and fsize > 0:
        direct_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"⬇️ Download {qual} ({size_label})", url=dl_url)],
            [InlineKeyboardButton("🔗 Open Facebook", url=vd.get("url", ""))],
        ])

        ctx.user_data["downloads"] = ctx.user_data.get("downloads", 0) + 1

        try:
            await q.edit_message_caption(
                caption=(
                    f"📥 **Download Link Ready!**\n\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"📌 {vd['title']}\n"
                    f"{icon} Quality: **{qual}**\n"
                    f"📦 Size: **{size_label}**\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"👆 Tap the button above to download!\n\n"
                    f"💡 The file is too large to send via\n"
                    f"Telegram, but you can download it\n"
                    f"directly to your device.\n\n"
                    f"📥 Downloads: {ctx.user_data['downloads']}\n\n"
                    f"🤖 {BOT_USERNAME}"
                ),
                reply_markup=direct_kb, parse_mode="Markdown")
        except:
            await ctx.bot.send_message(
                chat_id=q.message.chat_id,
                text=f"📥 **Download {qual}** ({size_label}):",
                reply_markup=direct_kb, parse_mode="Markdown")
        return

    # For small/medium files - try sending
    async def status(txt):
        try:
            await q.edit_message_caption(
                caption=f"{txt}\n\n━━━━━━━━━━━━━━━━━━━━\n"
                f"{icon} **{qual}** • {size_label}\n"
                f"📌 {vd['title']}\n━━━━━━━━━━━━━━━━━━━━",
                parse_mode="Markdown")
        except: pass

    ok, method = await smart_send(ctx, q.message.chat_id, dl_url, mtype, qual, vd, ext, fsize, status)

    if ok:
        ctx.user_data["downloads"] = ctx.user_data.get("downloads", 0) + 1
        labels = {"direct": "⚡ Direct", "upload": "📤 Upload", "document": "📄 Document"}
        try:
            await q.edit_message_caption(
                caption=(
                    f"✅ **Sent Successfully!**\n\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"📌 {vd['title']}\n"
                    f"{icon} **{qual}** • {size_label}\n"
                    f"📡 {labels.get(method, method)}\n"
                    f"📥 Downloads: {ctx.user_data['downloads']}\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"Send another link! 🔗"),
                parse_mode="Markdown")
        except: pass
    else:
        # Give download link as fallback
        ctx.user_data["downloads"] = ctx.user_data.get("downloads", 0) + 1
        fb_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"⬇️ Download {qual} ({size_label})", url=dl_url)],
            [InlineKeyboardButton("🔗 Open Facebook", url=vd.get("url", ""))]])
        try:
            await q.edit_message_caption(
                caption=(
                    f"📥 **Download Link Ready!**\n\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"📌 {vd['title']}\n"
                    f"{icon} **{qual}** • {size_label}\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"👆 Tap button to download!\n\n"
                    f"📥 Downloads: {ctx.user_data['downloads']}\n\n"
                    f"🤖 {BOT_USERNAME}"),
                reply_markup=fb_kb, parse_mode="Markdown")
        except:
            await ctx.bot.send_message(chat_id=q.message.chat_id,
                text=f"📥 Download:", reply_markup=fb_kb)

# ==================== Error ====================

async def error_handler(update, ctx):
    logger.error(f"Error: {ctx.error}")
    if update and update.effective_message:
        try:
            await update.effective_message.reply_text(
                f"⚠️ Something went wrong. Try again.\n🤖 {BOT_USERNAME}")
        except: pass

# ==================== Main ====================

async def post_init(app):
    await set_cmds(app)
    logger.info("Commands set!")

def main():
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TOKEN not set!")
        return

    Thread(target=run_flask, daemon=True).start()
    logger.info(f"Flask on :{PORT}")

    app = (Application.builder().token(TELEGRAM_BOT_TOKEN)
        .read_timeout(300).write_timeout(300).connect_timeout(120)
        .post_init(post_init).build())

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("about", about_command))
    app.add_handler(CommandHandler("supported", supported_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("ping", ping_command))
    app.add_handler(CommandHandler("developer", developer_command))
    app.add_handler(CommandHandler("privacy", privacy_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_error_handler(error_handler)

    logger.info("🚀 Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()
