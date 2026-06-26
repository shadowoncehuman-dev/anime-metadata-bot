#!/usr/bin/env python3
"""
Anime image helpers — nekos.best (images & GIFs) + waifu.im (images).
"""

import logging, random, requests

log = logging.getLogger(__name__)

HEADERS = {"User-Agent": "AniVault-TelegramBot (https://github.com/shadowoncehuman-dev/anime-metadata-bot)"}

_NEKOS_BASE = "https://nekos.best/api/v2"

NEKOS_IMAGE_CATS = ["neko", "waifu", "kitsune"]
NEKOS_GIF_CATS  = [
    "wave", "dance", "happy", "smile", "wink", "blush", "nod",
    "thumbsup", "clap", "spin", "pat", "hug", "salute", "think",
    "facepalm", "nope", "bored", "shrug",
]

_WAIFU_BASE = "https://api.waifu.im/images"
_WAIFU_TAGS = ["waifu", "maid", "uniform", "marin-kitagawa", "selfies"]


def get_nekos_image(category: str | None = None) -> str | None:
    cat = category or random.choice(NEKOS_IMAGE_CATS)
    try:
        r = requests.get(f"{_NEKOS_BASE}/{cat}", headers=HEADERS, timeout=8)
        r.raise_for_status()
        results = r.json().get("results", [])
        if results:
            return results[0]["url"]
    except Exception as e:
        log.debug("nekos image error: %s", e)
    return None


def get_nekos_gif(category: str | None = None) -> str | None:
    cat = category or random.choice(NEKOS_GIF_CATS)
    try:
        r = requests.get(f"{_NEKOS_BASE}/{cat}", headers=HEADERS, timeout=8)
        r.raise_for_status()
        results = r.json().get("results", [])
        if results:
            return results[0]["url"]
    except Exception as e:
        log.debug("nekos gif error: %s", e)
    return None


def get_waifu_image(tag: str | None = None) -> str | None:
    chosen = tag or random.choice(_WAIFU_TAGS)
    try:
        r = requests.get(
            _WAIFU_BASE,
            params={"IncludedTags": chosen, "IsNsfw": "False"},
            headers=HEADERS,
            timeout=8,
        )
        r.raise_for_status()
        items = r.json().get("items", [])
        if items:
            return items[0]["url"]
    except Exception as e:
        log.debug("waifu.im error: %s", e)
    return None


def get_welcome_image() -> str | None:
    fn = random.choice([get_nekos_image, get_waifu_image])
    url = fn()
    return url or get_waifu_image("waifu")


def get_reaction_gif(mood: str = "wave") -> str | None:
    mood_map = {
        "wave": "wave", "happy": "happy", "loading": "spin",
        "done": "thumbsup", "start": "dance", "error": "facepalm",
        "search": "think", "cancel": "nope", "welcome": "wave",
        "browse": "happy", "idle": "bored",
    }
    cat = mood_map.get(mood, random.choice(NEKOS_GIF_CATS))
    return get_nekos_gif(cat)
