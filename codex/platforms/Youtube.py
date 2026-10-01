import asyncio
import os
import re
import logging
import aiohttp
import yt_dlp
from typing import Union, Optional, Tuple, List
from pyrogram.enums import MessageEntityType
from pyrogram.types import Message
from youtubesearchpython.__future__ import VideosSearch
from codex.utils.formatters import time_to_seconds
from codex import LOGGER

# --- CONFIGURATION ---
from config import API_ID, BOT_TOKEN, MONGO_DB_URI, YOUTUBE_IMG_URL

# --- SECURITY FILTER ---
class SensitiveDataFilter(logging.Filter):
    def filter(self, record):
        msg = str(record.msg)
        patterns = [r"\d{8,10}:[a-zA-Z0-9_-]{35,}", r"mongodb\+srv://\S+"]
        for pattern in patterns:
            msg = re.sub(pattern, "[PROTECTED]", msg)
        record.msg = msg
        return True

logging.getLogger().addFilter(SensitiveDataFilter())

# Apni Kiru API (Invidious based). Agar ye band ho toh yt-dlp automatically handle karega
API_URL = "https://kiru-bots.up.railway.app"
API_KEY = os.getenv("KIRU_API_KEY", "KIRU-BOT")

# --- UTILS ---
def get_clean_id(link: str) -> Optional[str]:
    if "v=" in link:
        video_id = link.split('v=')[-1].split('&')[0]
    elif "youtu.be/" in link:
        video_id = link.split('youtu.be/')[-1].split('?')[0]
    else:
        video_id = link
    clean_id = re.sub(r'[^a-zA-Z0-9_-]', '', video_id)
    return clean_id if 5 <= len(clean_id) <= 15 else None


def extract_url_from_info(info: dict, prefer_video: bool = False) -> Optional[str]:
    """Extracts the best playable stream URL from yt-dlp info dict."""
    try:
        # 1. Check direct URL
        if info.get("url"):
            return info["url"]

        # 2. Check requested formats
        requested = info.get("requested_formats")
        if requested:
            if not prefer_video:
                for fmt in requested:
                    if fmt.get("acodec") != "none" and fmt.get("url"):
                        return fmt["url"]
            else:
                for fmt in requested:
                    if fmt.get("vcodec") != "none" and fmt.get("url"):
                        return fmt["url"]

        # 3. Fallback to all formats
        formats = info.get("formats", [])
        if not prefer_video:
            # Sirf audio formats filter karein
            audio_formats = [
                f for f in formats 
                if f.get("acodec") != "none" and f.get("vcodec") == "none" and f.get("url")
            ]
            if audio_formats:
                return audio_formats[-1]["url"] # Best audio
        
        if formats:
            return formats[-1]["url"] # Sabse last format (usually best)
            
    except Exception as e:
        LOGGER.error(f"Extraction error: {e}")
    return None


async def get_direct_stream_link(link: str, media_type: str) -> Optional[str]:
    video_id = get_clean_id(link)
    if not video_id:
        return None
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(
            headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout
        ) as session:
            async with session.get(
                f"{API_URL}/download",
                params={"url": video_id, "type": media_type, "api_key": API_KEY},
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    stream = data.get("link") or data.get("stream")
                    if stream:
                        return stream
                else:
                    LOGGER.error(f"Kiru API error: HTTP {resp.status}")
    except Exception as e:
        LOGGER.error(f"Kiru API request failed: {e}")
    return None


class YouTubeAPI:
    def __init__(self):
        self.base = "https://www.youtube.com/watch?v="
        self.regex = r"(?:youtube\.com|youtu\.be)"

    async def exists(self, link: str):
        return bool(re.search(self.regex, link))

    async def url(self, message: Message) -> Optional[str]:
        messages = [message, message.reply_to_message]
        for msg in messages:
            if not msg: continue
            text = msg.text or msg.caption
            if not text: continue
            if msg.entities:
                for entity in msg.entities:
                    if entity.type == MessageEntityType.URL:
                        return text[entity.offset: entity.offset + entity.length]
            urls = re.findall(r'(https?://\S+)', text)
            if urls: return urls[0]
        return None

    async def search(self, query: str, limit: int = 1):
        try:
            search = VideosSearch(query, limit=limit)
            resp = await search.next()
            return resp.get("result", [])
        except Exception as e:
            LOGGER.error(f"Search Error: {e}")
            return []

    async def details(self, query: str, videoid: Union[bool, str] = None):
        if videoid:
            link = self.base + query if not query.startswith("http") else query
        else:
            link = query

        try:
            # Agar query ek YouTube link hai toh direct details nikalne ki koshish karein
            if await self.exists(link):
                # Using yt-dlp for more accurate details if link is provided
                ydl_opts = {"quiet": True, "no_warnings": True}
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = await asyncio.to_thread(ydl.extract_info, link, download=False)
                    title = info.get("title", "Unknown Title")
                    duration_sec = info.get("duration", 0)
                    duration_min = f"{duration_sec // 60:02d}:{duration_sec % 60:02d}"
                    thumbnail = info.get("thumbnail") or YOUTUBE_IMG_URL
                    vidid = info.get("id")
                    return title, duration_min, duration_sec, thumbnail, vidid
            
            # Agar query text hai toh search karein
            res = await self.search(link, limit=1)
            if not res:
                return None
            
            video = res[0]
            thumbnail = video.get("thumbnails")[0]["url"].split("?")[0] if video.get("thumbnails") else YOUTUBE_IMG_URL
            
            return (
                video.get("title", "Unknown Title"),
                video.get("duration", "00:00"),
                int(time_to_seconds(video.get("duration", "00:00"))),
                thumbnail,
                video.get("id"),
            )
        except Exception as e:
            LOGGER.error(f"Details Error: {e}")
            return None

    async def track(self, query: str, videoid: Union[bool, str] = None):
        det = await self.details(query, videoid)
        if not det:
            return None, None
        
        track_details = {
            "title": det[0],
            "link": self.base + det[4],
            "vidid": det[4],
            "duration_min": det[1],
            "duration_sec": det[2],
            "thumb": det[3],
        }
        return track_details, det[4]

    async def download(
        self,
        link: str,
        mystic=None,
        video: Union[bool, str] = None,
        videoid: Union[bool, str] = None,
        **kwargs,
    ) -> Tuple[Optional[str], bool]:
        if videoid:
            link = self.base + link
        
        m_type = "video" if video else "audio"

        # 1. Pehle Direct API try karein
        stream_link = await get_direct_stream_link(link, m_type)
        if stream_link:
            return stream_link, True

        # 2. Agar API fail ho toh yt-dlp use karein (With Bypass Headers)
        try:
            fmt = "bestaudio/best" if not video else "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
            
            ydl_opts = {
                "format": fmt,
                "quiet": True,
                "no_warnings": True,
                "geo_bypass": True,
                "nocheckcertificate": True,
                "noplaylist": True,
                # YouTube Bot Blocking bypass karne ke liye headers
                "headers": {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.5",
                    "Referer": "https://www.google.com/",
                }
            }
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = await asyncio.to_thread(ydl.extract_info, link, download=False)
                if not info:
                    return None, False

            url = extract_url_from_info(info, prefer_video=bool(video))
            
            if url and len(url) > 10:
                return url, True

        except Exception as e:
            LOGGER.error(f"yt-dlp final error: {e}")

        return None, False

# Global Instance
YouTube = YouTubeAPI()
