"""JioSaavn search and download adapter.

The request/normalisation flow mirrors the public jio-1 (JioSaavn API)
repository.  The bot calls JioSaavn directly so a second web service is not
required at runtime.
"""

import base64
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp
from Crypto.Cipher import DES


JIOSAAVN_API_URL = os.environ.get(
    "JIOSAAVN_API_URL", "http://jiosaavn-api.np564605.workers.dev/"
).rstrip("/")
JIOSAAVN_QUALITY = os.environ.get("JIOSAAVN_QUALITY", "160")
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", "downloads"))


def _seconds_to_min(seconds: int | str | None) -> str:
    try:
        total = int(seconds or 0)
    except (TypeError, ValueError):
        total = 0
    minutes, seconds = divmod(total, 60)
    return f"{minutes}:{seconds:02d}"


def _image_url(url: str | None) -> str:
    if not url:
        return ""
    return (
        url.replace("150x150", "500x500")
        .replace("50x50", "500x500")
        .replace("http://", "https://", 1)
    )


def _decrypt_media_url(encrypted_url: str | None) -> str:
    """Decrypt JioSaavn's media URL using the same key as jio-1."""
    if not encrypted_url:
        return ""
    try:
        cipher = DES.new(b"38346591", DES.MODE_ECB)
        decrypted_bytes = cipher.decrypt(base64.b64decode(encrypted_url))
        padding = decrypted_bytes[-1]
        if 1 <= padding <= 8 and decrypted_bytes.endswith(bytes([padding]) * padding):
            decrypted_bytes = decrypted_bytes[:-padding]
        decrypted = decrypted_bytes.decode("utf-8")
        return decrypted.replace("_96", f"_{JIOSAAVN_QUALITY}", 1)
    except Exception:
        return ""


def _song_id_from_value(value: str) -> str:
    value = value.strip()
    if value.startswith("jio_"):
        return value[4:]
    if "jiosaavn.com/song/" in value:
        path_parts = [part for part in urlparse(value).path.split("/") if part]
        if path_parts:
            return path_parts[-1]
    return value


def _artist_name(song: dict[str, Any]) -> str:
    more_info = song.get("more_info") or {}
    artist_map = more_info.get("artistMap") or {}
    artists = artist_map.get("primary_artists") or []
    if artists:
        return ", ".join(item.get("name", "") for item in artists if item.get("name"))
    return song.get("subtitle") or ""


def _normalise_song(song: dict[str, Any]) -> dict[str, Any]:
    more_info = song.get("more_info") or {}
    duration = int(more_info.get("duration") or 0)
    song_id = str(song.get("id") or "")
    return {
        "title": song.get("title") or song.get("song") or "Unknown song",
        "artist": _artist_name(song),
        "duration_sec": duration,
        "duration_min": _seconds_to_min(duration),
        "thumb": _image_url(song.get("image")),
        "link": song.get("perma_url") or "",
        "vidid": f"jio_{song_id}",
        "id": song_id,
        "download_url": _decrypt_media_url(more_info.get("encrypted_media_url")),
    }


class JioSaavnAPI:
    """Small async client exposing the shape expected by codex-music."""

    _headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131 Safari/537.36"
        ),
        "Accept": "application/json",
    }

    async def _request(self, call: str, **params: Any) -> dict[str, Any]:
        query = {
            "__call": call,
            "_format": "json",
            "_marker": "0",
            "api_version": "4",
            "ctx": "web6dot0",
            **{key: value for key, value in params.items() if value is not None},
        }
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(headers=self._headers, timeout=timeout) as session:
            async with session.get(JIOSAAVN_API_URL, params=query) as response:
                response.raise_for_status()
                payload = await response.json(content_type=None)
                if not isinstance(payload, dict):
                    raise RuntimeError("JioSaavn returned an invalid response")
                return payload

    async def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        payload = await self._request(
            "search.getResults", q=query, p=0, n=max(1, min(limit, 20))
        )
        return [
            _normalise_song(item)
            for item in (payload.get("results") or [])
            if item.get("id")
        ]

    async def _details_by_id(self, song_id: str) -> dict[str, Any]:
        payload = await self._request("song.getDetails", pids=song_id)
        songs = payload.get("songs") or []
        if not songs:
            raise LookupError("Song not found on JioSaavn")
        return _normalise_song(songs[0])

    async def track(self, query: str) -> tuple[dict[str, Any], str]:
        value = query.strip()
        if value.startswith("jio_"):
            song = await self._details_by_id(_song_id_from_value(value))
        elif "jiosaavn.com/song/" in value:
            song = await self._details_by_id(_song_id_from_value(value))
        else:
            results = await self.search(value, limit=1)
            if not results:
                raise LookupError("No song found on JioSaavn")
            song = results[0]
        return song, song["vidid"]

    async def details(self, value: str) -> tuple[str, str, int, str, str]:
        song, _ = await self.track(value)
        return (
            song["title"],
            song["duration_min"],
            song["duration_sec"],
            song["thumb"],
            song["vidid"],
        )

    async def download(self, value: str) -> str:
        song, _ = await self.track(value)
        if not song["download_url"]:
            raise RuntimeError("JioSaavn did not provide a playable download URL")

        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        file_path = DOWNLOAD_DIR / f"{song['vidid']}_{JIOSAAVN_QUALITY}.mp3"
        if file_path.exists() and file_path.stat().st_size > 10_000:
            return str(file_path)

        timeout = aiohttp.ClientTimeout(total=300)
        try:
            async with aiohttp.ClientSession(headers=self._headers, timeout=timeout) as session:
                async with session.get(song["download_url"]) as response:
                    response.raise_for_status()
                    with file_path.open("wb") as output:
                        async for chunk in response.content.iter_chunked(128 * 1024):
                            output.write(chunk)
        except Exception:
            file_path.unlink(missing_ok=True)
            raise

        if file_path.stat().st_size <= 10_000:
            file_path.unlink(missing_ok=True)
            raise RuntimeError("Downloaded JioSaavn audio is empty")
        return str(file_path)

    async def valid(self, link: str) -> bool:
        return "jiosaavn.com/song/" in link.lower()

    async def search_multi(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        return await self.search(query, limit)
