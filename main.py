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
CACHE_TTL = 30  

DB_FILE = "streamers.db"

def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS streamers (
                username TEXT PRIMARY KEY,
                pin TEXT NOT NULL,
                yt_api_key TEXT DEFAULT '',
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
    yt_api_key: Optional[str] = ""
    channel_id: str
    video_id: Optional[str] = ""
    sub_goal: int
    ticker_text: str

# Helper to convert "12.5K" to 12500 so the frontend Progress Bar works
def parse_yt_number(num_str):
    num_str = str(num_str).upper().replace(',', '').strip()
    try:
        if 'K' in num_str:
            return int(float(num_str.replace('K', '')) * 1000)
        elif 'M' in num_str:
            return int(float(num_str.replace('M', '')) * 1000000)
        else:
            return int(num_str)
    except ValueError:
        return 0

# --- Web Scraping Helpers (No API Key Required) ---
async def scrape_youtube_data(client, channel_id, video_id):
    subs = 0
    likes = 0
    
    try:
        # Scrape Channel for Subs
        channel_url = f"https://www.youtube.com/channel/{channel_id}"
        c_res = await client.get(channel_url, headers={"Accept-Language": "en-US,en;q=0.9"})
        c_match = re.search(r'ytInitialData = ({.*?});</script>', c_res.text)
        if c_match:
            data = json.loads(c_match.group(1))
            header = data.get('header', {}).get('c4TabbedHeaderRenderer', {})
            subs_text = header.get('subscriberCountText', {}).get('simpleText', '0').split(' ')[0]
            subs = parse_yt_number(subs_text)

        # Scrape Video for Likes
        if video_id:
            vid_url = f"https://www.youtube.com/watch?v={video_id}"
            v_res = await client.get(vid_url, headers={"Accept-Language": "en-US,en;q=0.9"})
            v_match = re.search(r'ytInitialData = ({.*?});</script>', v_res.text)
            if v_match:
                v_data = json.loads(v_match.group(1))
                contents = v_data.get('contents', {}).get('twoColumnWatchNextResults', {}).get('results', {}).get('results', {}).get('contents', [])
                for item in contents:
                    if 'videoPrimaryInfoRenderer' in item:
                        likes_text = item['videoPrimaryInfoRenderer'].get('videoActions', {}).get('menuRenderer', {}).get('topLevelButtons', [])[0].get('segmentedLikeDislikeButtonRenderer', {}).get('likeButton', {}).get('toggleButtonRenderer', {}).get('defaultText', {}).get('simpleText', '0')
                        likes = parse_yt_number(likes_text)
                        break
    except Exception as e:
        print(f"Scraping Error: {e}")
        
    return subs, likes

# --- Static Frontend Routes ---
@app.get("/dashboard")
async def get_dashboard():
    return FileResponse("public/dashboard.html")

@app.get("/overlay")
async def get_overlay():
    return FileResponse("public/overlay.html")

# --- ONE Single API Endpoint for Fetching Data ---
@app.get("/api/streamer/{username}")
async def get_streamer_data(username: str):
    user = username.strip().lower()
    
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT yt_api_key, channel_id, video_id, sub_goal, ticker_text FROM streamers WHERE username = ?", (user,))
        row = cursor.fetchone()

    if not row:
        return {"registered": False, "sub_goal": 5000, "ticker_text": "AWAITING CONFIGURATION IN DASHBOARD", "subs": 0, "likes": 0}

    yt_api_key, channel_id, video_id, sub_goal, ticker_text = row
    subs, likes = 0, 0

    if channel_id:
        now = time.time()
        cache_key = f"{channel_id}:{video_id}"

        if cache_key in CACHE and CACHE[cache_key]["expires_at"] > now:
            subs = CACHE[cache_key]["data"]["subs"]
            likes = CACHE[cache_key]["data"]["likes"]
        else:
            async with httpx.AsyncClient() as client:
                # ROUTE A: Official API (If Key is Provided)
                if yt_api_key:
                    try:
                        sub_url = f"https://www.googleapis.com/youtube/v3/channels?part=statistics&id={channel_id}&key={yt_api_key}"
                        sub_res = await client.get(sub_url)
                        sub_data = sub_res.json()
                        if "items" in sub_data and len(sub_data["items"]) > 0:
                            subs = int(sub_data["items"][0]["statistics"].get("subscriberCount", 0))

                        if video_id:
                            vid_url = f"https://www.googleapis.com/youtube/v3/videos?part=statistics&id={video_id}&key={yt_api_key}"
                            vid_res = await client.get(vid_url)
                            vid_data = vid_res.json()
                            if "items" in vid_data and len(vid_data["items"]) > 0:
                                likes = int(vid_data["items"][0]["statistics"].get("likeCount", 0))
                    except Exception as e:
                        print(f"YouTube Fetch Error for {user}: {e}")
                
                # ROUTE B: Public Scraping (If No Key is Provided)
                else:
                    subs, likes = await scrape_youtube_data(client, channel_id, video_id)

            # Save to Cache
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
                INSERT INTO streamers (username, pin, yt_api_key, channel_id, video_id, sub_goal, ticker_text)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (user, data.pin, data.yt_api_key.strip(), data.channel_id.strip(), data.video_id.strip(), data.sub_goal, data.ticker_text.strip()))
            conn.commit()
            return {"status": "created", "message": "Streamer profile created and locked with PIN."}
        else:
            existing_pin = row[0]
            if existing_pin != data.pin:
                raise HTTPException(status_code=403, detail="Invalid PIN for this channel.")

            cursor.execute("""
                UPDATE streamers 
                SET yt_api_key = ?, channel_id = ?, video_id = ?, sub_goal = ?, ticker_text = ?
                WHERE username = ?
            """, (data.yt_api_key.strip(), data.channel_id.strip(), data.video_id.strip(), data.sub_goal, data.ticker_text.strip(), user))
            conn.commit()
            return {"status": "updated", "message": "Overlay settings updated successfully."}

app.mount("/", StaticFiles(directory="public", html=True), name="public")
