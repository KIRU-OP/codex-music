"""JioSaavn search and download adapter.

The adapter supports both JioSaavn's public API and the jio-1 compatible API.
Cloudflare Access service-token headers are optional and are read from the
runtime environment.
"""

import base64
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import aiohttp
from Crypto.Cipher import DES


JIOSAAVN_API_URL = os.environ.get(
    "JIOSAAVN_API_URL", "http://jiosaavn-api.np564605.workers.dev/"
).rstrip("/")
JIOSAAVN_QUALITY = os.environ.get("JIOSAAVN_QUALITY", "160")
JIOSAAVN_API_MODE = os.environ.get("JIOSAAVN_API_MODE", "auto").lower()
CF_ACCESS_CLIENT_ID = os.environ.get("CF_ACCESS_CLIENT_ID", "")
CF_ACCESS_CLIENT_SECRET = os.environ.get("CF_ACCESS_CLIENT_SECRET", "")
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


def _jio1_download_url(song: dict[str, Any]) -> str:
    links = song.get("downloadUrl") or []
    if not isinstance(links, list):
        return ""
    for link in links:
        if str(link.get("quality")) == str(JIOSAAVN_QUALITY) and link.get("url"):
            return link["url"]
    for link in links:
        if link.get("url"):
            return link["url"]
    return ""


def _normalise_jio1_song(song: dict[str, Any]) -> dict[str, Any]:
    artists = (song.get("artists") or {}).get("primary") or []
    images = song.get("image") or []
    thumb = ""
    if images:
        thumb = images[-1].get("url") or images[0].get("url") or ""
    duration = int(song.get("duration") or 0)
    song_id = str(song.get("id") or "")
    return {
        "title": song.get("name") or "Unknown song",
        "artist": ", ".join(
            item.get("name", "") for item in artists if item.get("name")
        ),
        "duration_sec": duration,
        "duration_min": _seconds_to_min(duration),
        "thumb": _image_url(thumb),
        "link": song.get("url") or "",
        "vidid": f"jio_{song_id}",
        "id": song_id,
        "download_url": _jio1_download_url(song),
    }


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

    @property
    def _is_jio1(self) -> bool:
        if JIOSAAVN_API_MODE in {"jio1", "jiosaavn-api"}:
            return True
        if JIOSAAVN_API_MODE == "direct":
            return False
        return not JIOSAAVN_API_URL.endswith("/api.php")

    def _request_headers(self) -> dict[str, str]:
        headers = dict(self._headers)
        if CF_ACCESS_CLIENT_ID and CF_ACCESS_CLIENT_SECRET:
            headers.update(
                {
                    "CF-Access-Client-Id": CF_ACCESS_CLIENT_ID,
                    "CF-Access-Client-Secret": CF_ACCESS_CLIENT_SECRET,
                }
            )
        return headers

    async def _get_json(
        self, url: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(
            headers=self._request_headers(), timeout=timeout
        ) as session:
            async with session.get(url, params=params) as response:
                response.raise_for_status()
                payload = await response.json(content_type=None)
                if not isinstance(payload, dict):
                    raise RuntimeError("JioSaavn returned an invalid response")
                return payload

    async def _request(self, call: str, **params: Any) -> dict[str, Any]:
        if self._is_jio1:
            if call == "search.getResults":
                payload = await self._get_json(
                    f"{JIOSAAVN_API_URL}/search/songs",
                    {
                        "query": params.get("q", ""),
                        "page": params.get("p", 0),
                        "limit": params.get("n", 5),
                    },
                )
                data = payload.get("data") or {}
                return {
                    "results": [
                        {"jio1_song": item}
                        for item in (data.get("results") or [])
                    ]
                }
            if call == "song.getDetails":
                payload = await self._get_json(
                    f"{JIOSAAVN_API_URL}/songs/{quote(str(params.get('pids', '')))}"
                )
                return {
                    "songs": [
                        {"jio1_song": item}
                        for item in (payload.get("data") or [])
                    ]
                }
            raise ValueError(f"Unsupported jio-1 API call: {call}")

        query = {
            "__call": call,
            "_format": "json",
            "_marker": "0",
            "api_version": "4",
            "ctx": "web6dot0",
            **{key: value for key, value in params.items() if value is not None},
        }
        return await self._get_json(JIOSAAVN_API_URL, query)

    async def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        payload = await self._request(
            "search.getResults", q=query, p=0, n=max(1, min(limit, 20))
        )
        songs = []
        for item in payload.get("results") or []:
            if "jio1_song" in item:
                item = item["jio1_song"]
                song = _normalise_jio1_song(item)
            else:
                song = _normalise_song(item)
            if song["id"]:
                songs.append(song)
        return songs

    async def _details_by_id(self, song_id: str) -> dict[str, Any]:
        payload = await self._request("song.getDetails", pids=song_id)
        songs = payload.get("songs") or []
        if not songs:
            raise LookupError("Song not found on JioSaavn")
        song = songs[0]
        return _normalise_jio1_song(song["jio1_song"]) if "jio1_song" in song else _normalise_song(song)

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
                async with session.get(
                    song["download_url"], headers=self._request_headers()
                ) as response:
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
