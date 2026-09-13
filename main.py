import time
import sqlite3
import random
from typing import Optional
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import httpx

app = FastAPI()

CACHE = {}
CACHE_TTL = 30  # Cache for 30s to prevent spamming public APIs

DB_FILE = "streamers.db"

# Decentralized public YouTube instances (Bypasses Render blocks)
INVIDIOUS_INSTANCES = [
    "https://invidious.jing.rocks",
    "https://inv.tux.pizza",
    "https://invidious.nerdvpn.de",
    "https://vid.puffyan.us"
]

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

# --- 100% KEYLESS PUBLIC DATA FETCHING ---
async def get_keyless_youtube_data(client, channel_id, video_id):
    subs = 0
    likes = 0

    # 1. Fetch Likes (Return YouTube Dislike API - Extremely Reliable)
    if video_id:
        try:
            ryd_url = f"https://returnyoutubedislikeapi.com/votes?videoId={video_id}"
            ryd_res = await client.get(ryd_url, timeout=5.0)
            if ryd_res.status_code == 200:
                likes = ryd_res.json().get("likes", 0)
        except Exception as e:
            print(f"Likes Fetch Error: {e}")

    # 2. Fetch Subs (Invidious Network Mesh)
    if channel_id:
        # Shuffle instances to avoid rate limits
        instances = random.sample(INVIDIOUS_INSTANCES, len(INVIDIOUS_INSTANCES))
        for instance in instances:
            try:
                sub_url = f"{instance}/api/v1/channels/{channel_id}"
                sub_res = await client.get(sub_url, timeout=4.0)
                if sub_res.status_code == 200:
                    data = sub_res.json()
                    subs = data.get("subCount", 0)
                    break  # Success! Exit loop
            except:
                continue # Try the next instance if this one is down

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

        if cache_key in CACHE and CACHE[cache_key]["expires_at"] > now:
            subs = CACHE[cache_key]["data"]["subs"]
            likes = CACHE[cache_key]["data"]["likes"]
        else:
            async with httpx.AsyncClient() as client:
                subs, likes = await get_keyless_youtube_data(client, channel_id, video_id)
            
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
