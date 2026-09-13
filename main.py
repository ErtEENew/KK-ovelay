import time
import sqlite3
import json
import re
from typing import Optional
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import httpx

app = FastAPI()

CACHE = {}
CACHE_TTL = 30  # Cache for 30 seconds to avoid spamming the APIs

DB_FILE = "streamers.db"

def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS streamers (
                username TEXT PRIMARY KEY,
                pin TEXT NOT NULL,
                channel_id TEXT DEFAULT '',
                video_id TEXT DEFAULT '',
                sub_goal INTEGER DEFAULT 5000,
                ticker_text TEXT DEFAULT 'WELCOME TO THE STREAM'
            )
        """)
        conn.commit()

init_db()

class StreamerConfigUpdate(BaseModel):
    pin: str
    channel_id: str
    video_id: Optional[str] = ""
    sub_goal: int
    ticker_text: str

# --- 100% KEYLESS PUBLIC DATA FETCHING (Triple-Layer) ---
async def get_keyless_youtube_data(client, channel_id, video_id):
    subs = 0
    likes = 0

    # Disguise the cloud server as a standard Chrome web browser to bypass blocks
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9"
    }

    # LAYER 1: Mixerno.space API (Returns exact/abbreviated integers)
    if channel_id:
        try:
            m_res = await client.get(f"https://mixerno.space/api/youtube-channel-counter/user/{channel_id}", headers=headers, timeout=5.0)
            if m_res.status_code == 200:
                data = m_res.json()
                if "counts" in data and len(data["counts"]) > 0:
                    subs = int(data["counts"][0].get("count", 0))
        except Exception as e:
            print(f"Mixerno fetch failed: {e}")

    # LAYER 2: Fallback to Raw HTML Scraping if Layer 1 fails
    if subs == 0 and channel_id:
        try:
            c_res = await client.get(f"https://www.youtube.com/channel/{channel_id}", headers=headers, timeout=5.0)
            c_match = re.search(r'ytInitialData = ({.*?});</script>', c_res.text)
            if c_match:
                data = json.loads(c_match.group(1))
                header = data.get('header', {}).get('c4TabbedHeaderRenderer', {})
                subs_text = header.get('subscriberCountText', {}).get('simpleText', '0').split(' ')[0]
                
                # Convert abbreviated text like "1.25K" to 1250
                subs_text = subs_text.upper().replace(',', '')
                if 'K' in subs_text:
                    subs = int(float(subs_text.replace('K', '')) * 1000)
                elif 'M' in subs_text:
                    subs = int(float(subs_text.replace('M', '')) * 1000000)
                else:
                    subs = int(subs_text)
        except Exception as e:
            print(f"HTML Scrape failed: {e}")

    # LAYER 3: Fetch Live Likes using Return YouTube Dislike open database
    if video_id:
        try:
            ryd_res = await client.get(f"https://returnyoutubedislikeapi.com/votes?videoId={video_id}", headers=headers, timeout=5.0)
            if ryd_res.status_code == 200:
                likes = ryd_res.json().get("likes", 0)
        except Exception:
            pass

    return subs, likes

# --- Static Routes ---
@app.get("/dashboard")
async def get_dashboard():
    return FileResponse("public/dashboard.html")

@app.get("/overlay")
async def get_overlay():
    return FileResponse("public/overlay.html")

# --- API Endpoints ---
@app.get("/api/streamer/{username}")
async def get_streamer_data(username: str):
    user = username.strip().lower()
    
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT channel_id, video_id, sub_goal, ticker_text FROM streamers WHERE username = ?", (user,))
        row = cursor.fetchone()

    if not row:
        return {"registered": False, "sub_goal": 5000, "ticker_text": "AWAITING CONFIGURATION", "subs": 0, "likes": 0}

    channel_id, video_id, sub_goal, ticker_text = row
    subs, likes = 0, 0

    if channel_id:
        now = time.time()
        cache_key = f"{channel_id}:{video_id}"

        # Only pull from cache if the data is recent AND the subs are greater than 0
        if cache_key in CACHE and CACHE[cache_key]["expires_at"] > now and CACHE[cache_key]["data"]["subs"] > 0:
            subs = CACHE[cache_key]["data"]["subs"]
            likes = CACHE[cache_key]["data"]["likes"]
        else:
            async with httpx.AsyncClient(follow_redirects=True) as client:
                subs, likes = await get_keyless_youtube_data(client, channel_id, video_id)
            
            # NEVER cache a '0' failure. Keep retrying if it fails.
            if subs > 0:
                CACHE[cache_key] = {"data": {"subs": subs, "likes": likes}, "expires_at": now + CACHE_TTL}

    return {
        "registered": True,
        "sub_goal": sub_goal,
        "ticker_text": ticker_text,
        "subs": subs,
        "likes": likes
    }

@app.post("/api/streamer/{username}/save")
async def save_streamer_data(username: str, data: StreamerConfigUpdate):
    user = username.strip().lower()
    
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT pin FROM streamers WHERE username = ?", (user,))
        row = cursor.fetchone()

        if row is None:
            cursor.execute("""
                INSERT INTO streamers (username, pin, channel_id, video_id, sub_goal, ticker_text)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (user, data.pin, data.channel_id.strip(), data.video_id.strip(), data.sub_goal, data.ticker_text.strip()))
            conn.commit()
            return {"status": "created", "message": "Streamer profile created and locked with PIN."}
        else:
            if row[0] != data.pin:
                raise HTTPException(status_code=403, detail="Invalid PIN for this profile.")

            cursor.execute("""
                UPDATE streamers 
                SET channel_id = ?, video_id = ?, sub_goal = ?, ticker_text = ?
                WHERE username = ?
            """, (data.channel_id.strip(), data.video_id.strip(), data.sub_goal, data.ticker_text.strip(), user))
            conn.commit()
            return {"status": "updated", "message": "Overlay settings updated securely."}

app.mount("/", StaticFiles(directory="public", html=True), name="public")
