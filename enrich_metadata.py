#!/usr/bin/env python3
"""
Anime/Manga/Movie metadata enrichment — fixed & improved.
Sources: AniList (GraphQL), Jikan/MyAnimeList (REST).
"""

import os, time, re, logging
from difflib import SequenceMatcher
import requests
from supabase import create_client, Client

log = logging.getLogger(__name__)

SUPABASE_URL          = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY          = os.environ.get("SUPABASE_ANON_KEY", "")
SUPABASE_SERVICE_KEY  = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

# ── tuning ────────────────────────────────────────────────────────────────────
JIKAN_DELAY    = 0.6
ANILIST_DELAY  = 0.9
BATCH_PAUSE    = 1.5
MIN_SIMILARITY = 0.52          # strict enough to reject "King's Avatar" vs "Avatar: TLA"
ALWAYS_UPDATE_IMAGES = True    # overwrite existing TMDB or placeholder images

# anon client — for reading content rows
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# service-role client — bypasses RLS for genre upserts
_service_key = SUPABASE_SERVICE_KEY or SUPABASE_KEY
supabase_admin: Client = create_client(SUPABASE_URL, _service_key)

# ── helpers ───────────────────────────────────────────────────────────────────
def sim(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()

def keyword_hit(query: str, candidate: str) -> bool:
    """True if meaningful fraction of significant words from query appear in candidate."""
    stopwords = {"the","a","an","of","in","to","and","or","is","at","for",
                 "on","by","with","as","no","wa","ga","wo","de","ni","he",
                 "its","are","was","be","it","too","also","even"}
    words = [w for w in re.split(r"\W+", query.lower()) if len(w) > 3 and w not in stopwords]
    if len(words) < 2:
        return False
    hits = sum(1 for w in words if w in candidate.lower())
    # require at least 60% of significant words to match
    return hits >= max(2, int(len(words) * 0.6))

def best_title_match(query: str, candidates: list, title_fields: list[str]):
    """
    Score each candidate against all its title fields and return the best one.
    A candidate wins if:
      - its best sim score >= MIN_SIMILARITY, OR
      - keyword_hit fires on an English/synonym field AND sim >= 0.30
        (catches romaji-primary titles like "Sousou no Frieren" whose English
         title "Frieren: Beyond Journey's End" will have a high sim score on
         a different field, so this path mainly helps synonyms).
    """
    best_score, best_item = 0.0, None
    for item in candidates:
        item_best = 0.0
        for field in title_fields:
            val = item.get(field) or ""
            if not val:
                continue
            s = sim(query, val)
            if s > item_best:
                item_best = s
        if item_best > best_score:
            best_score, best_item = item_best, item

    if best_item and best_score >= MIN_SIMILARITY:
        return best_item

    # Looser pass: keyword match on English / synonym fields only (not romaji/native)
    # This avoids false positives from character overlap in romanised Japanese.
    en_like_fields = [f for f in title_fields if "en" in f or "syn" in f]
    for item in candidates:
        for field in en_like_fields:
            val = item.get(field) or ""
            s   = sim(query, val)
            if keyword_hit(query, val) and s >= 0.30:
                return item

    return None

def clean_title(title: str) -> str:
    t = re.sub(r"\s*[\(\[]\d{4}[\)\]]$", "", title).strip()
    t = re.sub(r"[：:]\s*Season\s*\d+", "", t, flags=re.I).strip()
    return t

def strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()

# ── Jikan (MyAnimeList) ───────────────────────────────────────────────────────
JIKAN_BASE = "https://api.jikan.moe/v4"

def jikan_search(title: str, is_movie: bool = False):
    """Try anime then manga endpoint. Returns raw Jikan item or None."""
    for endpoint in (["anime", "manga"] if not is_movie else ["anime"]):
        params = {"q": title, "limit": 8}
        if is_movie and endpoint == "anime":
            params["type"] = "movie"
        try:
            resp = requests.get(f"{JIKAN_BASE}/{endpoint}", params=params, timeout=12)
            if resp.status_code == 429:
                time.sleep(2)
                resp = requests.get(f"{JIKAN_BASE}/{endpoint}", params=params, timeout=12)
            resp.raise_for_status()
            data = resp.json().get("data", [])
            time.sleep(JIKAN_DELAY)
            if not data:
                continue
            # flatten all title variants per item
            for item in data:
                item["_title_en"]  = item.get("title_english") or ""
                item["_title_jp"]  = item.get("title") or ""
                item["_title_syn"] = " ".join(s.get("title","") for s in (item.get("titles") or []))
            match = best_title_match(title, data,
                                     ["_title_en", "_title_jp", "_title_syn", "title"])
            if match:
                log.info("    Jikan ✓ [%s] %s", endpoint, match.get("title"))
                return match
        except Exception as e:
            log.warning("    Jikan error (%s): %s", endpoint, e)
            time.sleep(JIKAN_DELAY)
    return None

def jikan_to_fields(item: dict, is_movie: bool) -> tuple[dict, list]:
    images = item.get("images", {})
    jpg  = images.get("jpg", {})
    webp = images.get("webp", {})
    poster = (webp.get("large_image_url") or jpg.get("large_image_url")
              or webp.get("image_url") or jpg.get("image_url") or "")
    synopsis = re.sub(r"\[Written by MAL.*?\]", "",
                      (item.get("synopsis") or item.get("background") or "")).strip()

    aired = item.get("aired") or item.get("published") or {}
    year = None
    if aired.get("from"):
        try: year = int(aired["from"][:4])
        except: pass
    year = year or item.get("year")

    score = item.get("score")
    rating = round(float(score), 1) if score else None

    duration = None
    m = re.search(r"(\d+)\s*min", item.get("duration", "") or "")
    if m: duration = int(m.group(1))

    raw_status = (item.get("status") or "").lower()
    status_map = {
        "currently airing": "ongoing", "finished airing": "completed",
        "not yet aired": "upcoming", "publishing": "ongoing",
        "finished": "completed", "on hiatus": "hiatus",
        "discontinued": "cancelled",
    }
    status = status_map.get(raw_status)
    genres = [g["name"] for g in
              (item.get("genres") or []) + (item.get("themes") or [])
              + (item.get("demographics") or [])]

    fields: dict = {}
    if synopsis:   fields["description"]     = synopsis
    if year:       fields["release_year"]     = year
    if rating:     fields["rating"]           = rating
    if poster:
        fields["poster_url"]    = poster
        fields["thumbnail_url"] = poster
    if duration:   fields["duration_minutes"] = duration
    if status:     fields["status"]           = status
    return fields, genres

# ── AniList ───────────────────────────────────────────────────────────────────
ANILIST_URL = "https://graphql.anilist.co"

# Two separate queries — one with type filter, one without
_AL_QUERY_TYPED = """
query ($search: String, $type: MediaType) {
  Page(page: 1, perPage: 8) {
    media(search: $search, type: $type) {
      title { romaji english native }
      synonyms
      description(asHtml: false)
      startDate { year }
      averageScore
      coverImage { extraLarge large medium }
      bannerImage
      status
      duration
      episodes
      genres
      format
    }
  }
}
"""

_AL_QUERY_ANY = """
query ($search: String) {
  Page(page: 1, perPage: 8) {
    media(search: $search) {
      title { romaji english native }
      synonyms
      description(asHtml: false)
      startDate { year }
      averageScore
      coverImage { extraLarge large medium }
      bannerImage
      status
      duration
      episodes
      genres
      format
    }
  }
}
"""

def _anilist_request(variables: dict) -> list:
    query = _AL_QUERY_TYPED if "type" in variables else _AL_QUERY_ANY
    try:
        resp = requests.post(ANILIST_URL,
                             json={"query": query, "variables": variables},
                             timeout=12)
        if resp.status_code == 429:
            time.sleep(3)
            resp = requests.post(ANILIST_URL,
                                 json={"query": query, "variables": variables},
                                 timeout=12)
        resp.raise_for_status()
        time.sleep(ANILIST_DELAY)
        return resp.json().get("data", {}).get("Page", {}).get("media", [])
    except Exception as e:
        log.warning("    AniList error: %s", e)
        time.sleep(ANILIST_DELAY)
        return []

def _flatten_anilist(results: list) -> list:
    """Add flat title fields to each result for matching."""
    for r in results:
        t    = r.get("title", {}) or {}
        syns = r.get("synonyms") or []   # synonyms is at media level, not inside title
        r["_t_en"]  = t.get("english") or ""
        r["_t_ro"]  = t.get("romaji")  or ""
        r["_t_na"]  = t.get("native")  or ""
        r["_t_syn"] = " | ".join(syns)
    return results

def anilist_search(title: str, is_movie: bool = False):
    """
    Try ANIME → MANGA (if not movie) → no-type filter.
    Match against all title variants (en, romaji, native, synonyms).
    """
    TITLE_FIELDS = ["_t_en", "_t_ro", "_t_na", "_t_syn"]

    type_attempts = ["ANIME", "MANGA"] if not is_movie else ["ANIME"]
    for media_type in type_attempts:
        results = _flatten_anilist(_anilist_request({"search": title, "type": media_type}))
        match = best_title_match(title, results, TITLE_FIELDS)
        if match:
            t = match.get("title", {})
            log.info("    AniList ✓ [%s] %s / %s", media_type,
                     t.get("english") or "?", t.get("romaji") or "?")
            return match

    # fallback: no type filter (catches manhwa, ONA, specials, etc.)
    results = _flatten_anilist(_anilist_request({"search": title}))
    match = best_title_match(title, results, TITLE_FIELDS)
    if match:
        t = match.get("title", {})
        log.info("    AniList ✓ [any] %s / %s",
                 t.get("english") or "?", t.get("romaji") or "?")
        return match

    log.info("    AniList ✗ no match")
    return None

def anilist_to_fields(item: dict, is_movie: bool) -> tuple[dict, list]:
    cover  = item.get("coverImage", {}) or {}
    banner = item.get("bannerImage") or ""
    poster = cover.get("extraLarge") or cover.get("large") or cover.get("medium") or ""
    description = strip_html(item.get("description") or "")
    year        = (item.get("startDate") or {}).get("year")
    raw_score   = item.get("averageScore")
    rating      = round(raw_score / 10, 1) if raw_score else None
    duration    = item.get("duration")  # minutes for anime
    raw_status  = (item.get("status") or "").upper()
    status_map  = {
        "FINISHED": "completed", "RELEASING": "ongoing",
        "NOT_YET_RELEASED": "upcoming", "CANCELLED": "cancelled", "HIATUS": "hiatus",
    }
    status = status_map.get(raw_status)
    genres = item.get("genres") or []

    fields: dict = {}
    if description: fields["description"]     = description
    if year:        fields["release_year"]     = year
    if rating:      fields["rating"]           = rating
    if poster:
        fields["poster_url"]    = poster
        fields["thumbnail_url"] = poster
    if banner:      fields["banner_url"]       = banner
    if duration:    fields["duration_minutes"] = duration
    if status:      fields["status"]           = status
    return fields, genres

# ── merge ─────────────────────────────────────────────────────────────────────
def merge(jikan_f: dict, jikan_g: list,
          anilist_f: dict, anilist_g: list) -> tuple[dict, list]:
    merged = {**jikan_f}
    genres = list(set(jikan_g + anilist_g))
    for key, val in anilist_f.items():
        if key == "description":
            merged[key] = val if len(val) > len(merged.get(key) or "") else merged.get(key, val)
        elif key in ("poster_url", "thumbnail_url", "banner_url", "rating"):
            merged[key] = val          # AniList wins on images & score
        elif key not in merged or not merged[key]:
            merged[key] = val
    return merged, genres

# ── genres ────────────────────────────────────────────────────────────────────
_genre_cache: dict = {}

def ensure_genre(name: str) -> str | None:
    name = name.strip()
    if not name: return None
    if name in _genre_cache: return _genre_cache[name]
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    try:
        ex = supabase_admin.table("genres").select("id").eq("name", name).execute()
        gid = ex.data[0]["id"] if ex.data else \
              supabase_admin.table("genres").insert({"name": name, "slug": slug}).execute().data[0]["id"]
        _genre_cache[name] = gid
        return gid
    except Exception as e:
        log.warning("ensure_genre '%s': %s", name, e)
        return None

def upsert_genres(content_id: str, genres: list) -> None:
    for gname in genres:
        gid = ensure_genre(gname)
        if not gid: continue
        try:
            supabase_admin.table("content_genres").upsert(
                {"content_id": content_id, "genre_id": gid},
                on_conflict="content_id,genre_id",
            ).execute()
        except Exception as e:
            log.warning("upsert_genres '%s': %s", gname, e)

# ── per-item enrichment ───────────────────────────────────────────────────────
def enrich_item(row: dict, dry_run: bool = False,
                force_images: bool = True) -> tuple[bool, str]:
    """Returns (changed, summary_text)."""
    content_id  = row["id"]
    raw_title   = row.get("title", "")
    is_movie    = (row.get("type") or "series").lower() == "movie"
    title       = clean_title(raw_title)

    log.info("  ► %s [%s]", raw_title, "movie" if is_movie else "series")

    # ── fetch from both sources ───────────────────────────────────────────────
    j_item = jikan_search(title, is_movie=is_movie)
    j_fields, j_genres = jikan_to_fields(j_item, is_movie) if j_item else ({}, [])

    a_item = anilist_search(title, is_movie=is_movie)
    a_fields, a_genres = anilist_to_fields(a_item, is_movie) if a_item else ({}, [])

    if not j_fields and not a_fields:
        return False, f"❌ No match — *{raw_title}*"

    merged_fields, all_genres = merge(j_fields, j_genres, a_fields, a_genres)

    # ── decide what to write ──────────────────────────────────────────────────
    update_payload: dict = {}
    for field, new_val in merged_fields.items():
        if not new_val:
            continue
        current = row.get(field)
        if field in ("poster_url", "banner_url", "thumbnail_url"):
            # always overwrite images — existing ones may be wrong/low-res
            if force_images or not current:
                update_payload[field] = new_val
        elif field == "rating":
            if not current or float(current) == 0:
                update_payload[field] = new_val
        elif field == "description":
            if not current or (new_val and len(new_val) > len(current)):
                update_payload[field] = new_val
        elif not current:
            update_payload[field] = new_val

    if not update_payload and not all_genres:
        return False, f"⏭ Nothing new — *{raw_title}*"

    changed_keys = sorted(update_payload.keys())
    sources = []
    if j_item: sources.append("MAL")
    if a_item: sources.append("AniList")

    if dry_run:
        lines = [f"🔎 *{raw_title}*",
                 f"Source: {' + '.join(sources)}",
                 f"Fields: `{', '.join(changed_keys)}`",
                 f"Genres: {', '.join(all_genres) or 'none'}"]
        return True, "\n".join(lines)

    # ── write ─────────────────────────────────────────────────────────────────
    if update_payload:
        supabase_admin.table("content").update(update_payload).eq("id", content_id).execute()
    if all_genres:
        upsert_genres(content_id, all_genres)

    lines = [f"✅ *{raw_title}*",
             f"Source: {' + '.join(sources)}",
             f"Updated: `{', '.join(changed_keys)}`",
             f"Genres: {', '.join(all_genres) or 'none'}"]
    return True, "\n".join(lines)

# ── batch runner ──────────────────────────────────────────────────────────────
def run_enrichment(dry_run: bool = False,
                   limit: int | None = None,
                   offset: int = 0,
                   title_filter: str | None = None,
                   force_images: bool = True,
                   progress_cb=None) -> dict:
    """
    Run enrichment over DB rows. Returns stats dict:
    {total, updated, skipped, failed, summaries}
    """
    query = supabase.table("content").select(
        "id,title,type,description,release_year,rating,"
        "poster_url,banner_url,thumbnail_url,duration_minutes,status"
    )
    if title_filter:
        query = query.ilike("title", f"%{title_filter}%")
    query = query.order("created_at").range(offset, offset + (limit or 9999) - 1)
    rows = query.execute().data or []

    total   = len(rows)
    updated = skipped = failed = 0
    summaries: list[str] = []

    for i, row in enumerate(rows, 1):
        if progress_cb:
            progress_cb(i, total, row.get("title", ""))
        try:
            changed, summary = enrich_item(row, dry_run=dry_run,
                                           force_images=force_images)
            summaries.append(summary)
            if changed: updated += 1
            else:       skipped += 1
        except Exception as e:
            failed += 1
            summaries.append(f"⚠️ *{row.get('title')}*: `{e}`")
            log.exception("enrich_item error")
        time.sleep(BATCH_PAUSE)

    return {"total": total, "updated": updated,
            "skipped": skipped, "failed": failed,
            "summaries": summaries}

# ── single-title search (for /search command) ─────────────────────────────────
def preview_title(title: str) -> dict:
    """Search APIs for a title and return raw preview data (no DB write)."""
    is_movie = False
    j = jikan_search(title, is_movie=is_movie)
    a = anilist_search(title, is_movie=is_movie)
    if not j and not a:
        is_movie = True
        j = jikan_search(title, is_movie=is_movie)
        a = anilist_search(title, is_movie=is_movie)
    jf, jg = jikan_to_fields(j, is_movie) if j else ({}, [])
    af, ag = anilist_to_fields(a, is_movie) if a else ({}, [])
    merged, genres = merge(jf, jg, af, ag)
    return {"fields": merged, "genres": genres,
            "jikan": bool(j), "anilist": bool(a)}

# ── db stats ──────────────────────────────────────────────────────────────────
def get_db_stats() -> dict:
    all_rows  = supabase.table("content").select("id,type,rating,poster_url,banner_url,description,status").execute().data or []
    genres_ct = supabase.table("genres").select("id", count="exact").execute().count or 0
    total = len(all_rows)
    movies  = sum(1 for r in all_rows if r.get("type") == "movie")
    series  = total - movies
    no_img  = sum(1 for r in all_rows if not r.get("poster_url"))
    no_ban  = sum(1 for r in all_rows if not r.get("banner_url"))
    no_desc = sum(1 for r in all_rows if not r.get("description"))
    no_rate = sum(1 for r in all_rows if not r.get("rating") or float(r.get("rating") or 0) == 0)
    return {
        "total": total, "series": series, "movies": movies,
        "no_poster": no_img, "no_banner": no_ban,
        "no_description": no_desc, "no_rating": no_rate,
        "genres": genres_ct,
    }
