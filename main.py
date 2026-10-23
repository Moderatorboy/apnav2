import os
import sys
import json
import time
import asyncio
import shutil
import urllib.request
from datetime import datetime, timezone

import yt_dlp
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.tl.types import DocumentAttributeVideo

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ================= Configuration =================
def env_required(name):
    val = os.getenv(name, "").strip()
    if not val:
        sys.exit(f"Error: environment variable {name} set nahi hai (.env file check karo)")
    return val


API_ID = int(env_required("TG_API_ID"))
API_HASH = env_required("TG_API_HASH")
BOT_TOKEN = env_required("TG_BOT_TOKEN")
_chat = env_required("TG_CHAT_ID")                      # @username ya -100xxxxxxxxxx
CHAT_ID = int(_chat) if _chat.lstrip("-").isdigit() else _chat

# Optional: /update sirf inhi user IDs ko reply dega (comma separated). Khali = sab ko.
ALLOWED_USERS = {int(x) for x in os.getenv("TG_ALLOWED_USERS", "").replace(" ", "").split(",") if x.isdigit()}

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, "data.json")
STATE_FILE = os.path.join(BASE_DIR, "progress_state.json")
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")
SESSION_NAME = os.path.join(BASE_DIR, "vps_uploader_bot")

MB = 1024 * 1024
MAX_UPLOAD_BYTES = 2000 * MB      # Telegram MTProto limit (bot) = 2 GB
QUEUE_SIZE = int(os.getenv("QUEUE_SIZE", "1"))   # disk par max files = QUEUE_SIZE + 2
DOWNLOAD_RETRIES = 3
UPLOAD_RETRIES = 3
UPLOAD_GAP = 3                    # seconds between two uploads (flood se bachne ke liye)

BOLD_LABELS = True                # False karoge to saare labels plain text ho jayenge

FFMPEG = shutil.which("ffmpeg")   # video ka cover/thumbnail banane ke liye (apt install ffmpeg -y)
FFPROBE = shutil.which("ffprobe")


# ================= Font helpers =================
def bold(text):
    """ASCII text ko Unicode bold (𝐀𝐛𝐜 𝟏𝟐𝟑) me convert karta hai."""
    out = []
    for ch in str(text):
        if "A" <= ch <= "Z":
            out.append(chr(0x1D400 + ord(ch) - 65))
        elif "a" <= ch <= "z":
            out.append(chr(0x1D41A + ord(ch) - 97))
        elif "0" <= ch <= "9":
            out.append(chr(0x1D7CE + ord(ch) - 48))
        else:
            out.append(ch)
    return "".join(out)


def label(text):
    return bold(text) if BOLD_LABELS else text


# ================= State (resume) =================
def load_state():
    st = {"uploaded": [], "failed": [], "intro_sent": False}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            st["uploaded"] = list(loaded.get("uploaded", []))
            st["failed"] = list(loaded.get("failed", []))
            st["intro_sent"] = bool(loaded.get("intro_sent", False))
        except Exception as e:
            # Chup-chap reset karne se sab dobara upload ho jata, isliye ruk jao
            sys.exit(f"Error: {STATE_FILE} read nahi hui ({e}). File check/fix karo, phir run karo.")
    return st


state = load_state()


def save_state():
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, STATE_FILE)      # atomic: beech me kill hone par bhi file kharab nahi hogi


def mark_uploaded(lec_id):
    if lec_id not in state["uploaded"]:
        state["uploaded"].append(lec_id)
    state["failed"] = [f for f in state["failed"] if f["id"] != lec_id]
    save_state()


def mark_failed(lec_id, title, error, stage):
    state["failed"] = [f for f in state["failed"] if f["id"] != lec_id]
    state["failed"].append({
        "id": lec_id,
        "title": title,
        "error": str(error),
        "stage": stage,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })
    save_state()
    print(f"\n[FAILED] {title} -> {stage}: {error}")


# ================= Runtime status (/update ke liye) =================
status = {
    "started": time.time(),
    "downloading": None,
    "uploading": None,
    "upload_pct": 0.0,
    "upload_size": "",
    "flood_until": 0.0,
    "done_this_run": 0,
}


def sanitize_title(title):
    clean = "".join(c for c in str(title) if c.isalnum() or c in (" ", "-", "_")).strip()
    return clean or "video"


def build_caption(lec, batch_name):
    title = str(lec["title"])
    if len(title) > 300:
        title = title[:297] + "..."
    caption = (
        f"➭ {label('Index')} » {lec['orderIdx']}\n"
        f"➭ {label('Title')} » {title}\n"
        f"➭ {label('Type')} » {lec.get('type', 'wistia')}\n"
        f"➭ {label('Batch')} » {batch_name}\n"
        f"✨ {label('DOWNLOADED BY')} : CODExTRMS\n"
        f"━━━━━━━✦✗✦━━━━━━━"
    )
    return caption[:1024]            # Telegram caption limit


# ================= Batch intro (image + caption) =================
def fmt_sync_time(value):
    if not value:
        return "N/A"
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo:
            dt = dt.astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return str(value)


def build_batch_caption(data, batch_name):
    price = data.get("originalPrice")
    price_txt = "Free" if price in (0, None, "0") else str(price)
    caption = (
        f"➭ {label('Batch')} » {batch_name}\n"
        f"➭ {label('Total Videos')} » {data.get('videoCount', 'N/A')}\n"
        f"➭ {label('Total PDFs')} » {data.get('pdfCount', 'N/A')}\n"
        f"➭ {label('Popular')} » {'Yes' if data.get('isPopular') else 'No'}\n"
        f"➭ {label('Price')} » {price_txt}\n"
        f"➭ {label('Start Date')} » {data.get('startDate') or 'N/A'}\n"
        f"➭ {label('Last Synced')} » {fmt_sync_time(data.get('lastSyncedAt'))}\n"
        f"✨ {label('DOWNLOADED BY')} : CODExTRMS\n"
        f"━━━━━━━✦✗✦━━━━━━━"
    )
    return caption[:1024]


def download_image(url, path):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp, open(path, "wb") as f:
        f.write(resp.read())


async def send_batch_intro(client, data, batch_name):
    """Sabse pehle batch ki image + details bhejta hai (sirf ek baar, resume par dobara nahi)."""
    caption = build_batch_caption(data, batch_name)
    image = data.get("image")
    while True:
        try:
            if image:
                try:
                    await client.send_file(CHAT_ID, image, caption=caption)
                except FloodWaitError:
                    raise
                except Exception as e:
                    print(f"[Intro] URL se direct image nahi gayi ({e}), download karke bhej raha hoon...")
                    path = os.path.join(DOWNLOAD_DIR, "batch_cover.jpg")
                    try:
                        await asyncio.get_running_loop().run_in_executor(None, download_image, image, path)
                        await client.send_file(CHAT_ID, path, caption=caption)
                    except FloodWaitError:
                        raise
                    except Exception as e2:
                        print(f"[Intro] Image fail ({e2}), sirf text bhej raha hoon...")
                        await client.send_message(CHAT_ID, caption)
                    finally:
                        if os.path.exists(path):
                            os.remove(path)
            else:
                await client.send_message(CHAT_ID, caption)
            state["intro_sent"] = True
            save_state()
            print("[Intro] Batch image + details bhej diye.\n")
            return
        except FloodWaitError as e:
            print(f"[FloodWait on intro] {e.seconds}s ruk raha hoon...")
            await asyncio.sleep(e.seconds + 2)
        except Exception as e:
            print(f"[Intro] Bhejna fail hua ({e}), videos ka kaam chalu rakhta hoon.\n")
            return


# ================= Private channel support =================
async def ensure_chat(client):
    """Private channel (numeric ID) ho aur bot use pehchane nahi, to ek message ka wait karta hai."""
    try:
        await client.get_input_entity(CHAT_ID)
        return
    except Exception as e:
        print(f"[Chat] Channel resolve nahi hua ({e}).")

    if isinstance(CHAT_ID, str):
        sys.exit("Error: TG_CHAT_ID (@username) galat hai ya channel ab private hai. "
                 ".env me numeric ID daalo, jaise TG_CHAT_ID=-1001234567890")

    print("[Chat] Bot ko channel me ADMIN banao, phir channel me koi bhi ek message bhejo. "
          "Main wait kar raha hoon...")
    seen = asyncio.Event()

    async def _on_msg(event):
        if event.chat_id == CHAT_ID:
            seen.set()

    client.add_event_handler(_on_msg, events.NewMessage())
    try:
        await seen.wait()
    finally:
        client.remove_event_handler(_on_msg)
    print("[Chat] Channel mil gaya, aage badh raha hoon.\n")


# ================= /update text =================
def progress_bar(pct, width=12):
    filled = int(round(width * pct / 100))
    return "█" * filled + "░" * (width - filled)


def fmt_duration(sec):
    sec = int(max(sec, 0))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def build_update_text(lectures, batch_name):
    total = len(lectures)
    ids = {l["id"] for l in lectures}
    uploaded = len(ids & set(state["uploaded"]))
    failed_list = [f for f in state["failed"] if f["id"] in ids]
    failed = len(failed_list)
    remaining = total - uploaded
    pending = max(remaining - failed, 0)
    pct = (uploaded / total * 100) if total else 100.0

    lines = [
        f"📊 {bold('UPLOAD STATUS')}",
        "━━━━━━━✦✗✦━━━━━━━",
        f"➭ {label('Batch')} » {batch_name}",
        f"➭ {label('Total')} » {total}",
        f"➭ {label('Uploaded')} » {uploaded} ({pct:.1f}%)",
        f"➭ {label('Remaining')} » {remaining} (Pending {pending} + Failed {failed})",
        f"{progress_bar(pct)} {pct:.1f}%",
        "",
    ]

    if status["downloading"]:
        lines.append(f"⬇️ {label('Downloading')} » {status['downloading']}")
    if status["uploading"]:
        lines.append(f"⬆️ {label('Uploading')} » {status['uploading']} "
                     f"({status['upload_pct']:.1f}% {status['upload_size']})")
    if status["flood_until"] > time.time():
        lines.append(f"⏳ {label('FloodWait')} » {fmt_duration(status['flood_until'] - time.time())} baki")
    if not status["downloading"] and not status["uploading"]:
        lines.append("💤 Abhi koi video process nahi ho rahi")

    if status["done_this_run"] > 0 and pending > 0:
        per_video = (time.time() - status["started"]) / status["done_this_run"]
        lines.append(f"🕒 {label('ETA')} » ~{fmt_duration(per_video * pending)}")

    if failed_list:
        lines.append("")
        lines.append(f"❌ {bold('FAILED')} ({failed})")
        for f in failed_list[:15]:
            lines.append(f"• {f['title']} — Failed [{f.get('stage', '?')}]")
        if failed > 15:
            lines.append(f"... +{failed - 15} more")

    lines.append("")
    lines.append(f"✨ {label('DOWNLOADED BY')} : CODExTRMS")
    return "\n".join(lines)[:4000]


# ================= yt-dlp (async wrapper) =================
async def download_video(url, output_path):
    ydl_opts = {
        "format": "best",
        "outtmpl": output_path,
        "merge_output_format": "mp4",
        "continuedl": True,          # .part file se download resume hota hai
        "retries": 10,
        "fragment_retries": 10,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
    }

    def _run_yt():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=True)

    return await asyncio.get_running_loop().run_in_executor(None, _run_yt)


# ================= Video cover (thumbnail) + metadata =================
async def run_cmd(*args):
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    return proc.returncode, out, err


async def probe_video(path):
    """(duration_sec, width, height) deta hai, fail hone par None."""
    if not FFPROBE:
        return None
    try:
        rc, out, _ = await run_cmd(
            FFPROBE, "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height:format=duration", "-of", "json", path,
        )
        if rc != 0:
            return None
        info = json.loads(out)
        stream = info["streams"][0]
        return int(float(info["format"]["duration"])), int(stream["width"]), int(stream["height"])
    except Exception as e:
        print(f"[Probe] metadata nahi mila: {e}")
        return None


async def make_thumbnail(path, duration):
    """Video ke ek frame se cover (jpg) banata hai. ffmpeg na ho to None."""
    if not FFMPEG:
        return None
    thumb = path + ".thumb.jpg"
    ts = min(max((duration or 0) * 0.1, 1.0), 30.0)       # ~10% par, 1s se 30s ke beech
    try:
        rc, _, _ = await run_cmd(
            FFMPEG, "-y", "-loglevel", "error", "-ss", f"{ts:.2f}", "-i", path,
            "-frames:v", "1",
            "-vf", "scale='if(gt(iw,ih),320,-2)':'if(gt(iw,ih),-2,320)'",
            "-q:v", "5", thumb,
        )
        if rc == 0 and os.path.exists(thumb) and os.path.getsize(thumb) > 0:
            return thumb
    except Exception as e:
        print(f"[Thumb] cover nahi bana: {e}")
    if os.path.exists(thumb):
        os.remove(thumb)
    return None


# ================= Upload progress =================
def get_progress_callback(title):
    last_print = 0

    def progress(current, total):
        nonlocal last_print
        if not total:
            return
        percent = current / total * 100
        status["upload_pct"] = percent
        status["upload_size"] = f"{current / MB:.0f}MB/{total / MB:.0f}MB"
        if percent - last_print >= 5 or percent >= 100:
            last_print = percent
            sys.stdout.write(f"\r[Telegram Uploading] {title[:25]}... -> {percent:.1f}% "
                             f"({current / MB:.1f}MB/{total / MB:.1f}MB)")
            sys.stdout.flush()

    return progress


# ================= Main pipeline =================
async def main():
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    if not os.path.exists(DATA_FILE):
        print(f"Error: {DATA_FILE} nahi mila!")
        return

    with open(DATA_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    batch_name = data.get("name", "Complete AI/ML Batch")
    lectures = data.get("lectures", [])
    for i, lec in enumerate(lectures):
        lec.setdefault("orderIdx", i)            # data.json ka orderIdx hi Index banega
        lec.setdefault("title", f"Lecture {lec['orderIdx']}")
    lectures.sort(key=lambda l: l["orderIdx"])

    print(f"Total Lectures in JSON: {len(lectures)}")
    print(f"Already Uploaded: {len(state['uploaded'])}, Failed (retry hoga): {len(state['failed'])}")

    client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
    while True:
        try:
            await client.start(bot_token=BOT_TOKEN)
            break
        except FloodWaitError as e:
            print(f"[FloodWait on login] {e.seconds}s ruk raha hoon...")
            await asyncio.sleep(e.seconds + 2)
    print("Telegram Bot Connected Successfully!\n")

    if not FFMPEG or not FFPROBE:
        print("[Warning] ffmpeg/ffprobe nahi mila -> videos par cover nahi aayega. "
              "Fix: apt install ffmpeg -y\n")

    await ensure_chat(client)

    if not state.get("intro_sent"):
        await send_batch_intro(client, data, batch_name)

    # /update command
    @client.on(events.NewMessage(pattern=r"(?i)^/update(@\w+)?\s*$"))
    async def update_handler(event):
        from_channel_admin = event.is_channel and not event.is_group
        if ALLOWED_USERS and event.sender_id not in ALLOWED_USERS and not from_channel_admin:
            return
        await event.respond(build_update_text(lectures, batch_name))

    upload_queue = asyncio.Queue(maxsize=QUEUE_SIZE)

    # Consumer: upload worker (download ke saath parallel chalta hai)
    async def upload_worker():
        while True:
            task = await upload_queue.get()
            if task is None:
                upload_queue.task_done()
                break

            lec, video_path, caption = task
            title, lec_id = lec["title"], lec["id"]
            status["uploading"] = title
            status["upload_pct"] = 0.0
            status["upload_size"] = ""

            # Cover + video info (duration/size) taaki Telegram me proper video ban kar dikhe
            meta = await probe_video(video_path)
            thumb = await make_thumbnail(video_path, meta[0] if meta else 0)
            attributes = None
            if meta:
                attributes = [DocumentAttributeVideo(
                    duration=meta[0], w=meta[1], h=meta[2], supports_streaming=True
                )]

            try:
                attempt = 0
                while True:
                    try:
                        print(f"\n[Starting Upload] Index {lec['orderIdx']}: {title}")
                        await client.send_file(
                            CHAT_ID,
                            video_path,
                            caption=caption,
                            thumb=thumb,
                            attributes=attributes,
                            supports_streaming=True,
                            part_size_kb=512,
                            progress_callback=get_progress_callback(title),
                        )
                        print(f"\n[Upload Done] {title}")
                        mark_uploaded(lec_id)
                        status["done_this_run"] += 1
                        break

                    except FloodWaitError as e:
                        wait = e.seconds + 5
                        status["flood_until"] = time.time() + wait
                        print(f"\n[FloodWait] {wait} seconds sleep... (attempt count nahi hoga)")
                        await asyncio.sleep(wait)

                    except Exception as e:
                        attempt += 1
                        if attempt >= UPLOAD_RETRIES:
                            mark_failed(lec_id, title, e, "telegram_upload")
                            break
                        print(f"\n[Upload Error] {title}: {e} -> retry {attempt}/{UPLOAD_RETRIES - 1}")
                        await asyncio.sleep(10 * attempt)
            finally:
                status["uploading"] = None
                if thumb and os.path.exists(thumb):
                    try:
                        os.remove(thumb)
                    except OSError:
                        pass
                if os.path.exists(video_path):
                    try:
                        os.remove(video_path)       # disk free
                    except OSError:
                        pass
                upload_queue.task_done()

            await asyncio.sleep(UPLOAD_GAP)

    uploader_task = asyncio.create_task(upload_worker())

    # Producer: ek-ek karke download, queue me daalta hai
    uploaded_set = set(state["uploaded"])
    for lec in lectures:
        lec_id = lec["id"]
        title = lec["title"]

        if lec_id in uploaded_set:
            continue

        url = lec.get("link")
        if not url:
            mark_failed(lec_id, title, "link missing in data.json", "download")
            continue

        filename = f"{lec['orderIdx']:03d}_{sanitize_title(title)}.mp4"
        video_path = os.path.join(DOWNLOAD_DIR, filename)

        print(f"\n[Download Start] Index {lec['orderIdx']}: {title}")
        status["downloading"] = title
        info, last_err = None, None

        for attempt in range(1, DOWNLOAD_RETRIES + 1):
            try:
                info = await download_video(url, video_path)
                if os.path.exists(video_path) and os.path.getsize(video_path) > 0:
                    break
                raise Exception("File empty or not created")
            except Exception as e:
                info, last_err = None, e
                print(f"[Download Error] {title}: {e} (try {attempt}/{DOWNLOAD_RETRIES})")
                if attempt < DOWNLOAD_RETRIES:
                    await asyncio.sleep(5 * attempt)

        status["downloading"] = None

        if info is None:
            mark_failed(lec_id, title, last_err, "download")
            continue

        size = os.path.getsize(video_path)
        if size > MAX_UPLOAD_BYTES:
            mark_failed(lec_id, title, f"File {size / MB:.0f}MB > 2GB Telegram limit", "size_limit")
            os.remove(video_path)
            continue

        caption = build_caption(lec, batch_name)

        print(f"[Download Done] Index {lec['orderIdx']}: {title} ({size / MB:.1f}MB, queued for upload)")
        await upload_queue.put((lec, video_path, caption))

    await upload_queue.put(None)
    await uploader_task

    ids = {l["id"] for l in lectures}
    failed_list = [f for f in state["failed"] if f["id"] in ids]
    print("\n" + "=" * 40)
    print(f"Uploaded: {len(ids & set(state['uploaded']))}/{len(lectures)}")
    if failed_list:
        print(f"Failed: {len(failed_list)} (dobara run karoge to inhe retry kiya jayega)")
        for f in failed_list:
            print(f"  - {f['title']} — Failed [{f.get('stage')}]: {f.get('error')}")
    else:
        print("Sabhi videos successfully process ho gayi hain!")

    await client.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nPaused. Dobara run karo, jahan roka tha wahin se resume hoga.")
