import asyncio
import html
import re
import sqlite3
import time
from contextlib import asynccontextmanager
from typing import Optional
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field


# ============================================================
# CONFIGURATION
# ============================================================

DB_FILE = "streamers.db"

# How long fetched statistics remain fresh.
# OBS clients can poll every 10 seconds, but the backend only
# needs to contact external services every 30 seconds.
CACHE_TTL = 30

# How long a failed refresh may continue serving the previous
# known subscriber count.
STALE_CACHE_TTL = 10 * 60

# Timeout for each external request.
REQUEST_TIMEOUT = 7.0

# Maximum time allowed for the entire external refresh.
REFRESH_TIMEOUT = 20.0


# ============================================================
# FASTAPI
# ============================================================

app = FastAPI(title="Streamer Overlay Backend")


# ============================================================
# IN-MEMORY CACHE
# ============================================================

# Structure:
#
# CACHE = {
#     "UCxxxxxxxx": {
#         "subs": 12345,
#         "likes": 500,
#         "updated_at": 1234567890,
#         "expires_at": 1234567920,
#     }
# }
#
# We cache by CHANNEL ID rather than username.
#
CACHE = {}

# Prevent multiple OBS browser sources from simultaneously
# fetching the same channel.
REFRESH_LOCKS = {}


# ============================================================
# HTTP CLIENT
# ============================================================

HTTP_CLIENT: Optional[httpx.AsyncClient] = None


# ============================================================
# HTTP HEADERS
# ============================================================

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) "
        "Chrome/153.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}


YOUTUBE_COOKIES = {
    "CONSENT": "YES+cb.20230214-11-p0.en+FX+430",
    "SOCS": "CAEQAw",
}


# ============================================================
# KEYLESS PUBLIC SOURCES
# ============================================================

# These are unofficial/public services.
#
# They can disappear, become rate-limited, or change their API.
# Therefore the application tries several sources.
#
INVIDIOUS_INSTANCES = [
    "https://invidious.nerdvpn.de",
    "https://inv.tux.pizza",
    "https://invidious.fdn.fr",
    "https://invidious.perennialte.ch",
]


# ============================================================
# DATABASE
# ============================================================

def init_db():
    """
    Create the streamers table if it doesn't already exist.
    """

    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS streamers (
                username TEXT PRIMARY KEY,
                pin TEXT NOT NULL,
                channel_id TEXT DEFAULT '',
                video_id TEXT DEFAULT '',
                sub_goal INTEGER DEFAULT 5000,
                ticker_text TEXT DEFAULT 'WELCOME TO THE STREAM'
            )
            """
        )

        conn.commit()


init_db()


# ============================================================
# PYDANTIC MODEL
# ============================================================

class StreamerConfigUpdate(BaseModel):
    pin: str = Field(min_length=1)
    channel_id: str = ""
    video_id: Optional[str] = ""
    sub_goal: int = Field(default=5000, ge=1)
    ticker_text: str = ""


# ============================================================
# GENERAL HELPERS
# ============================================================

def clean_username(username: str) -> str:
    """
    Normalize streamer username.
    """

    return username.strip().lower()


def clean_channel_id(channel_id: str) -> str:
    """
    Remove whitespace around the channel ID.
    """

    return channel_id.strip()


def parse_number(value) -> int:
    """
    Convert common YouTube number formats into integers.

    Examples:

        2410
        "2,410"
        "2.41K"
        "1.2M"
        "12.5K subscribers"
    """

    if value is None:
        return 0

    text = str(value).strip().upper()

    # Remove HTML entities.
    text = html.unescape(text)

    # Remove words that commonly appear beside counts.
    text = (
        text.replace("SUBSCRIBERS", "")
        .replace("SUBSCRIBER", "")
        .replace("LIKES", "")
        .replace("LIKE", "")
        .strip()
    )

    # Remove commas.
    text = text.replace(",", "")

    # Find first usable number.
    match = re.search(
        r"([0-9]+(?:\.[0-9]+)?)\s*([KMB]?)",
        text,
    )

    if not match:
        return 0

    number = float(match.group(1))
    suffix = match.group(2)

    multiplier = {
        "": 1,
        "K": 1_000,
        "M": 1_000_000,
        "B": 1_000_000_000,
    }.get(suffix, 1)

    return int(number * multiplier)


def is_valid_channel_id(channel_id: str) -> bool:
    """
    A normal YouTube channel ID starts with UC and is 24 characters.
    """

    return bool(
        re.fullmatch(r"UC[a-zA-Z0-9_-]{22}", channel_id)
    )


# ============================================================
# YOUTUBE URL / CHANNEL ID RESOLUTION
# ============================================================

def extract_channel_id_from_text(value: str) -> Optional[str]:
    """
    Extract a UC... channel ID directly from a supplied string.

    This allows users to paste things such as:

        UCxxxxxxxxxxxxxxxxxxxxxx

        https://www.youtube.com/channel/UCxxxxxxxxxxxxxxxxxxxxxx

    Returns None if a direct channel ID cannot be extracted.
    """

    if not value:
        return None

    value = value.strip()

    # Direct channel ID.
    match = re.search(
        r"(UC[a-zA-Z0-9_-]{22})",
        value,
    )

    if match:
        return match.group(1)

    return None


def normalize_youtube_input(value: str) -> str:
    """
    Normalize a YouTube channel input.

    This function does NOT perform a network request.

    It simply cleans common URL forms.
    """

    value = value.strip()

    if not value:
        return ""

    direct_id = extract_channel_id_from_text(value)

    if direct_id:
        return direct_id

    return value


async def resolve_channel_id(
    client: httpx.AsyncClient,
    channel_input: str,
) -> Optional[str]:
    """
    Resolve different YouTube channel inputs to a stable UC... ID.

    Supported examples:

        UCxxxxxxxxxxxxxxxxxxxxxx

        https://www.youtube.com/channel/UCxxxxxxxxxxxxxxxxxxxxxx

        https://www.youtube.com/@creator

        https://youtube.com/@creator

        @creator

    The most reliable case is always a direct UC channel ID.
    """

    value = normalize_youtube_input(channel_input)

    if not value:
        return None

    # --------------------------------------------------------
    # 1. Already a channel ID
    # --------------------------------------------------------

    if is_valid_channel_id(value):
        return value

    # --------------------------------------------------------
    # 2. @handle or YouTube URL
    # --------------------------------------------------------

    if value.startswith("@"):
        url = f"https://www.youtube.com/{value}"
    elif value.startswith("http://") or value.startswith("https://"):
        url = value
    else:
        # Treat plain text as a YouTube handle.
        url = f"https://www.youtube.com/@{value}"

    try:
        response = await client.get(
            url,
            headers=BROWSER_HEADERS,
            cookies=YOUTUBE_COOKIES,
            timeout=REQUEST_TIMEOUT,
        )

        if response.status_code != 200:
            return None

        text = response.text

        # ----------------------------------------------------
        # Method A:
        # canonical URL
        # ----------------------------------------------------

        canonical_patterns = [
            r'<link\s+rel="canonical"\s+href="https://www\.youtube\.com/channel/(UC[a-zA-Z0-9_-]{22})"',
            r'<meta\s+itemprop="url"\s+content="https://www\.youtube\.com/channel/(UC[a-zA-Z0-9_-]{22})"',
        ]

        for pattern in canonical_patterns:
            match = re.search(pattern, text, re.IGNORECASE)

            if match:
                return match.group(1)

        # ----------------------------------------------------
        # Method B:
        # browseId
        # ----------------------------------------------------

        browse_patterns = [
            r'"browseId":"(UC[a-zA-Z0-9_-]{22})"',
            r'"channelId":"(UC[a-zA-Z0-9_-]{22})"',
            r'"externalId":"(UC[a-zA-Z0-9_-]{22})"',
        ]

        for pattern in browse_patterns:
            match = re.search(pattern, text)

            if match:
                return match.group(1)

        # ----------------------------------------------------
        # Method C:
        # URL itself
        # ----------------------------------------------------

        match = re.search(
            r"/channel/(UC[a-zA-Z0-9_-]{22})",
            response.url.path,
        )

        if match:
            return match.group(1)

    except Exception as exc:
        print(f"[YouTube] Channel resolution failed: {exc}")

    return None


# ============================================================
# SOURCE 1 — AXERN
# ============================================================

async def fetch_from_axern(
    client: httpx.AsyncClient,
    channel_id: str,
) -> int:
    """
    Attempt to retrieve the subscriber count from Axern.

    Returns 0 when unavailable.
    """

    try:
        url = (
            "https://axern.space/api/get"
            f"?platform=youtube"
            f"&type=channel"
            f"&id={channel_id}"
        )

        response = await client.get(
            url,
            headers=BROWSER_HEADERS,
            timeout=REQUEST_TIMEOUT,
        )

        if response.status_code != 200:
            return 0

        data = response.json()

        # Different versions may expose different fields.
        candidates = [
            data.get("estSubCount"),
            data.get("subCount"),
            data.get("subscriberCount"),
        ]

        for candidate in candidates:
            parsed = parse_number(candidate)

            if parsed > 0:
                return parsed

    except Exception as exc:
        print(f"[Axern] Failed: {exc}")

    return 0


# ============================================================
# SOURCE 2 — INVIDIOUS
# ============================================================

async def fetch_from_invidious(
    client: httpx.AsyncClient,
    channel_id: str,
) -> int:
    """
    Try several Invidious instances.

    The first successful positive subscriber count is returned.
    """

    for instance in INVIDIOUS_INSTANCES:

        try:
            url = (
                f"{instance.rstrip('/')}"
                f"/api/v1/channels/{channel_id}"
            )

            response = await client.get(
                url,
                headers=BROWSER_HEADERS,
                timeout=REQUEST_TIMEOUT,
            )

            if response.status_code != 200:
                continue

            data = response.json()

            count = parse_number(
                data.get("subCount")
            )

            if count > 0:
                print(
                    f"[Invidious] Success: {instance}"
                )

                return count

        except Exception as exc:
            print(
                f"[Invidious] {instance} failed: {exc}"
            )

            continue

    return 0


# ============================================================
# SOURCE 3 — DIRECT YOUTUBE HTML
# ============================================================

def extract_subscriber_count_from_youtube_html(
    text: str,
) -> int:
    """
    Extract subscriber count from YouTube's public HTML.

    YouTube changes its internal HTML periodically, so several
    patterns are intentionally attempted.
    """

    if not text:
        return 0

    patterns = [
        # Accessibility label
        r'"subscriberCountText"\s*:\s*\{\s*"accessibility"\s*:\s*\{\s*"accessibilityData"\s*:\s*\{\s*"label"\s*:\s*"([^"]+)"',

        # simpleText
        r'"subscriberCountText"\s*:\s*\{\s*"simpleText"\s*:\s*"([^"]+)"',

        # Generic subscriber text
        r'"subscriberCountText"\s*:\s*"([^"]+)"',

        # Common metadata forms
        r'"subscriberCount"\s*:\s*"([^"]+)"',

        # JSON-like values
        r'"subscriberCount"\s*:\s*(\d+)',
    ]

    for pattern in patterns:

        matches = re.findall(
            pattern,
            text,
            re.IGNORECASE,
        )

        for match in matches:

            count = parse_number(match)

            if count > 0:
                return count

    return 0


async def fetch_from_youtube_html(
    client: httpx.AsyncClient,
    channel_id: str,
) -> int:
    """
    Fetch the public YouTube channel page and attempt to extract
    the subscriber count.
    """

    try:
        url = (
            f"https://www.youtube.com/channel/"
            f"{channel_id}"
        )

        response = await client.get(
            url,
            headers=BROWSER_HEADERS,
            cookies=YOUTUBE_COOKIES,
            timeout=REQUEST_TIMEOUT,
        )

        if response.status_code != 200:
            print(
                f"[YouTube HTML] HTTP {response.status_code}"
            )

            return 0

        count = extract_subscriber_count_from_youtube_html(
            response.text
        )

        if count > 0:
            print("[YouTube HTML] Subscriber count found.")

        return count

    except Exception as exc:
        print(
            f"[YouTube HTML] Failed: {exc}"
        )

        return 0


# ============================================================
# LIVE VIDEO LIKES
# ============================================================

async def fetch_video_likes(
    client: httpx.AsyncClient,
    video_id: str,
) -> int:
    """
    Fetch public like information from Return YouTube Dislike.

    This is separate from subscriber retrieval.
    """

    if not video_id:
        return 0

    try:
        url = (
            "https://returnyoutubedislikeapi.com/votes"
            f"?videoId={video_id}"
        )

        response = await client.get(
            url,
            headers=BROWSER_HEADERS,
            timeout=REQUEST_TIMEOUT,
        )

        if response.status_code != 200:
            return 0

        data = response.json()

        return parse_number(
            data.get("likes", 0)
        )

    except Exception as exc:
        print(
            f"[Likes] Failed: {exc}"
        )

        return 0


# ============================================================
# COMPLETE KEYLESS FETCH
# ============================================================

async def get_keyless_youtube_data(
    client: httpx.AsyncClient,
    channel_id: str,
    video_id: str = "",
):
    """
    Main keyless YouTube data pipeline.

    Subscriber sources are attempted in order:

        1. Axern
        2. Invidious
        3. YouTube HTML

    Likes:

        Return YouTube Dislike API

    Returns:

        {
            "subs": int,
            "likes": int,
            "source": str
        }
    """

    subs = 0
    source = "none"

    # --------------------------------------------------------
    # SOURCE 1
    # --------------------------------------------------------

    if channel_id:

        subs = await fetch_from_axern(
            client,
            channel_id,
        )

        if subs > 0:
            source = "axern"

    # --------------------------------------------------------
    # SOURCE 2
    # --------------------------------------------------------

    if subs <= 0 and channel_id:

        subs = await fetch_from_invidious(
            client,
            channel_id,
        )

        if subs > 0:
            source = "invidious"

    # --------------------------------------------------------
    # SOURCE 3
    # --------------------------------------------------------

    if subs <= 0 and channel_id:

        subs = await fetch_from_youtube_html(
            client,
            channel_id,
        )

        if subs > 0:
            source = "youtube_html"

    # --------------------------------------------------------
    # LIKES
    # --------------------------------------------------------

    likes = 0

    if video_id:
        likes = await fetch_video_likes(
            client,
            video_id,
        )

    return {
        "subs": subs,
        "likes": likes,
        "source": source,
    }


# ============================================================
# CACHE HELPERS
# ============================================================

def get_cached_data(channel_id: str):
    """
    Return fresh cached data if available.
    """

    item = CACHE.get(channel_id)

    if not item:
        return None

    if item["expires_at"] > time.time():
        return item

    return None


def get_stale_cached_data(channel_id: str):
    """
    Return recently expired data.

    This is important.

    If YouTube temporarily blocks a request, the overlay should
    NOT suddenly display:

        0 subscribers

    Instead, it should continue displaying the last known value.
    """

    item = CACHE.get(channel_id)

    if not item:
        return None

    age = time.time() - item["updated_at"]

    if age <= STALE_CACHE_TTL:
        return item

    return None


def save_cache(
    channel_id: str,
    subs: int,
    likes: int,
    source: str,
):
    """
    Save a successful result.
    """

    now = time.time()

    CACHE[channel_id] = {
        "subs": subs,
        "likes": likes,
        "source": source,
        "updated_at": now,
        "expires_at": now + CACHE_TTL,
    }


# ============================================================
# REFRESH LOCK
# ============================================================

def get_refresh_lock(channel_id: str) -> asyncio.Lock:
    """
    Get one lock per channel.

    If 20 OBS browser sources request the same streamer
    simultaneously, only one of them will refresh YouTube.
    """

    if channel_id not in REFRESH_LOCKS:
        REFRESH_LOCKS[channel_id] = asyncio.Lock()

    return REFRESH_LOCKS[channel_id]


# ============================================================
# GET FRESH STATS
# ============================================================

async def get_channel_stats(
    channel_id: str,
    video_id: str,
):
    """
    Get statistics while avoiding duplicate upstream requests.
    """

    # --------------------------------------------------------
    # 1. Fresh cache
    # --------------------------------------------------------

    cached = get_cached_data(channel_id)

    if cached:
        return (
            cached["subs"],
            cached["likes"],
            cached["source"],
        )

    # --------------------------------------------------------
    # 2. Lock this channel
    # --------------------------------------------------------

    lock = get_refresh_lock(channel_id)

    async with lock:

        # Another request may have refreshed the cache while
        # this request was waiting for the lock.
        cached = get_cached_data(channel_id)

        if cached:
            return (
                cached["subs"],
                cached["likes"],
                cached["source"],
            )

        # ----------------------------------------------------
        # 3. Perform external refresh
        # ----------------------------------------------------

        try:

            if HTTP_CLIENT is None:
                raise RuntimeError(
                    "HTTP client is not initialized."
                )

            result = await asyncio.wait_for(
                get_keyless_youtube_data(
                    HTTP_CLIENT,
                    channel_id,
                    video_id,
                ),
                timeout=REFRESH_TIMEOUT,
            )

            subs = result["subs"]
            likes = result["likes"]
            source = result["source"]

            # ------------------------------------------------
            # Successful subscriber fetch
            # ------------------------------------------------

            if subs > 0:

                # If the current request failed to get likes,
                # preserve the previous like count.
                if likes <= 0:
                    previous = CACHE.get(channel_id)

                    if previous:
                        likes = previous["likes"]

                save_cache(
                    channel_id,
                    subs,
                    likes,
                    source,
                )

                print(
                    f"[Stats] {channel_id} -> "
                    f"{subs:,} subscribers "
                    f"(source={source})"
                )

                return subs, likes, source

            # ------------------------------------------------
            # Subscriber refresh failed.
            #
            # Use stale value instead of 0.
            # ------------------------------------------------

            stale = get_stale_cached_data(channel_id)

            if stale:

                print(
                    f"[Stats] Refresh failed for "
                    f"{channel_id}; serving stale "
                    f"{stale['subs']:,} subscribers."
                )

                return (
                    stale["subs"],
                    stale["likes"],
                    "stale",
                )

            return 0, likes, "unavailable"

        except asyncio.TimeoutError:

            print(
                f"[Stats] Refresh timeout for "
                f"{channel_id}"
            )

        except Exception as exc:

            print(
                f"[Stats] Refresh failed: {exc}"
            )

        # ----------------------------------------------------
        # Final stale fallback
        # ----------------------------------------------------

        stale = get_stale_cached_data(channel_id)

        if stale:
            return (
                stale["subs"],
                stale["likes"],
                "stale",
            )

        return 0, 0, "unavailable"


# ============================================================
# STATIC ROUTES
# ============================================================

@app.get("/dashboard")
async def get_dashboard():
    return FileResponse(
        "public/dashboard.html"
    )


@app.get("/overlay")
async def get_overlay():
    return FileResponse(
        "public/overlay.html"
    )


# ============================================================
# STREAMER API — GET
# ============================================================

@app.get("/api/streamer/{username}")
async def get_streamer_data(username: str):

    user = clean_username(username)

    # --------------------------------------------------------
    # Read streamer configuration
    # --------------------------------------------------------

    with sqlite3.connect(DB_FILE) as conn:

        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT
                channel_id,
                video_id,
                sub_goal,
                ticker_text
            FROM streamers
            WHERE username = ?
            """,
            (user,),
        )

        row = cursor.fetchone()

    # --------------------------------------------------------
    # Streamer does not exist
    # --------------------------------------------------------

    if not row:

        return {
            "registered": False,
            "sub_goal": 5000,
            "ticker_text": "AWAITING CONFIGURATION",
            "subs": 0,
            "likes": 0,
            "source": "none",
        }

    channel_input, video_id, sub_goal, ticker_text = row

    channel_input = clean_channel_id(
        channel_input or ""
    )

    video_id = (video_id or "").strip()

    # --------------------------------------------------------
    # No channel configured
    # --------------------------------------------------------

    if not channel_input:

        return {
            "registered": True,
            "sub_goal": sub_goal,
            "ticker_text": ticker_text,
            "subs": 0,
            "likes": 0,
            "source": "not_configured",
        }

    # --------------------------------------------------------
    # Resolve channel ID
    # --------------------------------------------------------

    try:

        if is_valid_channel_id(channel_input):

            channel_id = channel_input

        else:

            if HTTP_CLIENT is None:
                raise RuntimeError(
                    "HTTP client is not initialized."
                )

            channel_id = await resolve_channel_id(
                HTTP_CLIENT,
                channel_input,
            )

    except Exception as exc:

        print(
            f"[Channel Resolve] Error: {exc}"
        )

        channel_id = None

    # --------------------------------------------------------
    # Could not resolve channel
    # --------------------------------------------------------

    if not channel_id:

        return {
            "registered": True,
            "sub_goal": sub_goal,
            "ticker_text": ticker_text,
            "subs": 0,
            "likes": 0,
            "source": "channel_unresolved",
        }

    # --------------------------------------------------------
    # Get subscriber statistics
    # --------------------------------------------------------

    subs, likes, source = await get_channel_stats(
        channel_id,
        video_id,
    )

    # --------------------------------------------------------
    # Response
    # --------------------------------------------------------

    return {
        "registered": True,

        "channel_id": channel_id,

        "sub_goal": sub_goal,

        "ticker_text": ticker_text,

        "subs": subs,

        "likes": likes,

        "source": source,
    }


# ============================================================
# STREAMER API — SAVE
# ============================================================

@app.post("/api/streamer/{username}/save")
async def save_streamer_data(
    username: str,
    data: StreamerConfigUpdate,
):

    user = clean_username(username)

    channel_input = clean_channel_id(
        data.channel_id
    )

    video_id = (
        data.video_id or ""
    ).strip()

    ticker_text = (
        data.ticker_text or ""
    ).strip()

    # --------------------------------------------------------
    # Basic validation
    # --------------------------------------------------------

    if not user:

        raise HTTPException(
            status_code=400,
            detail="Username is required.",
        )

    if not data.pin.strip():

        raise HTTPException(
            status_code=400,
            detail="PIN is required.",
        )

    if not channel_input:

        raise HTTPException(
            status_code=400,
            detail="YouTube channel ID or URL is required.",
        )

    if data.sub_goal < 1:

        raise HTTPException(
            status_code=400,
            detail="Subscriber goal must be at least 1.",
        )

    # --------------------------------------------------------
    # Normalize direct channel ID if supplied
    # --------------------------------------------------------

    direct_channel_id = extract_channel_id_from_text(
        channel_input
    )

    if direct_channel_id:

        channel_input = direct_channel_id

    # --------------------------------------------------------
    # Check existing streamer
    # --------------------------------------------------------

    with sqlite3.connect(DB_FILE) as conn:

        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT pin
            FROM streamers
            WHERE username = ?
            """,
            (user,),
        )

        row = cursor.fetchone()

        # ----------------------------------------------------
        # CREATE
        # ----------------------------------------------------

        if row is None:

            cursor.execute(
                """
                INSERT INTO streamers (
                    username,
                    pin,
                    channel_id,
                    video_id,
                    sub_goal,
                    ticker_text
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    user,
                    data.pin.strip(),
                    channel_input,
                    video_id,
                    data.sub_goal,
                    ticker_text,
                ),
            )

            conn.commit()

            return {
                "status": "created",
                "message": (
                    "Streamer profile created "
                    "and locked with PIN."
                ),
            }

        # ----------------------------------------------------
        # UPDATE
        # ----------------------------------------------------

        if row[0] != data.pin:

            raise HTTPException(
                status_code=403,
                detail="Invalid PIN for this profile.",
            )

        cursor.execute(
            """
            UPDATE streamers
            SET
                channel_id = ?,
                video_id = ?,
                sub_goal = ?,
                ticker_text = ?
            WHERE username = ?
            """,
            (
                channel_input,
                video_id,
                data.sub_goal,
                ticker_text,
                user,
            ),
        )

        conn.commit()

    # --------------------------------------------------------
    # Clear old cache if channel configuration changed.
    # --------------------------------------------------------

    #
    # We don't know the old channel here after the UPDATE,
    # so stale entries are harmless. The next request will
    # use the new channel ID as the cache key.
    #

    return {
        "status": "updated",
        "message": (
            "Overlay settings updated securely."
        ),
    }


# ============================================================
# STARTUP / SHUTDOWN
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI):

    global HTTP_CLIENT

    HTTP_CLIENT = httpx.AsyncClient(
        follow_redirects=True,
        http2=True,
        timeout=httpx.Timeout(
            REQUEST_TIMEOUT,
            connect=5.0,
        ),
        limits=httpx.Limits(
            max_connections=50,
            max_keepalive_connections=20,
        ),
    )

    print("--------------------------------------------")
    print("Streamer Overlay Backend Started")
    print("Keyless YouTube statistics enabled")
    print(f"Cache TTL: {CACHE_TTL}s")
    print("--------------------------------------------")

    try:

        yield

    finally:

        if HTTP_CLIENT is not None:

            await HTTP_CLIENT.aclose()

            HTTP_CLIENT = None


# ============================================================
# APPLY LIFESPAN
# ============================================================

app.router.lifespan_context = lifespan


# ============================================================
# STATIC FILES
# ============================================================

app.mount(
    "/",
    StaticFiles(
        directory="public",
        html=True,
    ),
    name="public",
)
