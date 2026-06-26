#!/usr/bin/env python3
"""
Anime / Manga metadata enrichment.
Sources: AniList (GraphQL), Jikan/MyAnimeList (REST).
Covers: content table + episodes table.
"""

import os, time, re, logging
from difflib import SequenceMatcher
import requests
from supabase import create_client, Client

log = logging.getLogger(__name__)

SUPABASE_URL         = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY         = os.environ.get("SUPABASE_ANON_KEY", "")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

JIKAN_DELAY    = 0.6
ANILIST_DELAY  = 0.9
BATCH_PAUSE    = 1.5
MIN_SIMILARITY = 0.52

supabase: Client       = create_client(SUPABASE_URL, SUPABASE_KEY)
supabase_admin: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY or SUPABASE_KEY)

# ── string helpers ─────────────────────────────────────────────────────────────
def sim(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()

def keyword_hit(query: str, candidate: str) -> bool:
    stopwords = {"the","a","an","of","in","to","and","or","is","at","for",
                 "on","by","with","as","no","wa","ga","wo","de","ni","he",
                 "its","are","was","be","it","too","also","even"}
    words = [w for w in re.split(r"\W+", query.lower()) if len(w) > 3 and w not in stopwords]
    if len(words) < 2:
        return False
    hits = sum(1 for w in words if w in candidate.lower())
    return hits >= max(2, int(len(words) * 0.6))

def best_title_match(query: str, candidates: list, title_fields: list[str]):
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

def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")

# ── Jikan (MyAnimeList) ────────────────────────────────────────────────────────
JIKAN_BASE = "https://api.jikan.moe/v4"

def jikan_search(title: str, is_movie: bool = False):
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
            for item in data:
                item["_title_en"]  = item.get("title_english") or ""
                item["_title_jp"]  = item.get("title") or ""
                item["_title_syn"] = " ".join(s.get("title","") for s in (item.get("titles") or []))
            match = best_title_match(title, data, ["_title_en", "_title_jp", "_title_syn", "title"])
            if match:
                log.info("    Jikan ✓ [%s] %s", endpoint, match.get("title"))
                return match
        except Exception as e:
            log.warning("    Jikan error (%s): %s", endpoint, e)
            time.sleep(JIKAN_DELAY)
    return None

def jikan_to_fields(item: dict, is_movie: bool) -> tuple[dict, list]:
    images = item.get("images", {})
    jpg    = images.get("jpg", {})
    webp   = images.get("webp", {})
    poster = (webp.get("large_image_url") or jpg.get("large_image_url")
              or webp.get("image_url") or jpg.get("image_url") or "")
    synopsis = re.sub(r"\[Written by MAL.*?\]", "",
                      (item.get("synopsis") or item.get("background") or "")).strip()

    aired = item.get("aired") or item.get("published") or {}
    year  = None
    if aired.get("from"):
        try: year = int(aired["from"][:4])
        except: pass
    year = year or item.get("year")

    score  = item.get("score")
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
    if synopsis: fields["description"]     = synopsis
    if year:     fields["release_year"]     = year
    if rating:   fields["rating"]           = rating
    if poster:
        fields["poster_url"]    = poster
        fields["thumbnail_url"] = poster
    if duration: fields["duration_minutes"] = duration
    if status:   fields["status"]           = status
    return fields, genres

# ── AniList ────────────────────────────────────────────────────────────────────
ANILIST_URL = "https://graphql.anilist.co"

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
      streamingEpisodes { title thumbnail url site }
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
      streamingEpisodes { title thumbnail url site }
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
    for r in results:
        t    = r.get("title", {}) or {}
        syns = r.get("synonyms") or []
        r["_t_en"]  = t.get("english") or ""
        r["_t_ro"]  = t.get("romaji")  or ""
        r["_t_na"]  = t.get("native")  or ""
        r["_t_syn"] = " | ".join(syns)
    return results

def anilist_search(title: str, is_movie: bool = False):
    TITLE_FIELDS   = ["_t_en", "_t_ro", "_t_na", "_t_syn"]
    type_attempts  = ["ANIME", "MANGA"] if not is_movie else ["ANIME"]
    for media_type in type_attempts:
        results = _flatten_anilist(_anilist_request({"search": title, "type": media_type}))
        match   = best_title_match(title, results, TITLE_FIELDS)
        if match:
            t = match.get("title", {})
            log.info("    AniList ✓ [%s] %s / %s", media_type,
                     t.get("english") or "?", t.get("romaji") or "?")
            return match
    results = _flatten_anilist(_anilist_request({"search": title}))
    match   = best_title_match(title, results, TITLE_FIELDS)
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
    duration    = item.get("duration")
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

# ── merge ──────────────────────────────────────────────────────────────────────
def merge(jikan_f: dict, jikan_g: list,
          anilist_f: dict, anilist_g: list) -> tuple[dict, list]:
    merged = {**jikan_f}
    genres = list(set(jikan_g + anilist_g))
    for key, val in anilist_f.items():
        if key == "description":
            merged[key] = val if len(val) > len(merged.get(key) or "") else merged.get(key, val)
        elif key in ("poster_url", "thumbnail_url", "banner_url", "rating"):
            merged[key] = val
        elif key not in merged or not merged[key]:
            merged[key] = val
    return merged, genres

# ── genres ─────────────────────────────────────────────────────────────────────
_genre_cache: dict = {}

def ensure_genre(name: str) -> str | None:
    name = name.strip()
    if not name: return None
    if name in _genre_cache: return _genre_cache[name]
    slug = slugify(name)
    try:
        ex  = supabase_admin.table("genres").select("id").eq("name", name).execute()
        gid = ex.data[0]["id"] if ex.data else \
              supabase_admin.table("genres").upsert(
                  {"name": name, "slug": slug}, on_conflict="name"
              ).execute().data[0]["id"]
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

# ── per-item content enrichment ────────────────────────────────────────────────
def enrich_item(row: dict, dry_run: bool = False,
                force_images: bool = True) -> tuple[bool, str]:
    content_id = row["id"]
    raw_title  = row.get("title", "")
    is_movie   = (row.get("type") or "series").lower() == "movie"
    title      = clean_title(raw_title)

    log.info("  ► %s [%s]", raw_title, "movie" if is_movie else "series")

    j_item = jikan_search(title, is_movie=is_movie)
    j_fields, j_genres = jikan_to_fields(j_item, is_movie) if j_item else ({}, [])

    a_item = anilist_search(title, is_movie=is_movie)
    a_fields, a_genres = anilist_to_fields(a_item, is_movie) if a_item else ({}, [])

    if not j_fields and not a_fields:
        return False, f"❌ No match — *{raw_title}*"

    merged_fields, all_genres = merge(j_fields, j_genres, a_fields, a_genres)

    update_payload: dict = {}
    for field, new_val in merged_fields.items():
        if not new_val:
            continue
        current = row.get(field)
        if field in ("poster_url", "banner_url", "thumbnail_url"):
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

    if update_payload:
        supabase_admin.table("content").update(update_payload).eq("id", content_id).execute()
    if all_genres:
        upsert_genres(content_id, all_genres)

    lines = [f"✅ *{raw_title}*",
             f"Source: {' + '.join(sources)}",
             f"Updated: `{', '.join(changed_keys)}`",
             f"Genres: {', '.join(all_genres) or 'none'}"]
    return True, "\n".join(lines)

# ── episode enrichment ─────────────────────────────────────────────────────────
def _get_streaming_episode_map(a_item: dict) -> dict[int, dict]:
    """
    Build a dict of {episode_number: {thumbnail, title, description}}
    from AniList streamingEpisodes data.
    """
    ep_map: dict[int, dict] = {}
    if not a_item:
        return ep_map
    for ep in (a_item.get("streamingEpisodes") or []):
        ep_title = ep.get("title") or ""
        thumb    = ep.get("thumbnail") or ""
        # AniList episode titles often start with "Episode N - ..."
        m = re.match(r"Episode\s+(\d+)", ep_title, re.I)
        if m:
            num = int(m.group(1))
            ep_map[num] = {"thumbnail": thumb, "title": ep_title}
    return ep_map


def enrich_episodes_for_content(content_row: dict,
                                dry_run: bool = False,
                                force_images: bool = True) -> tuple[int, int, int]:
    """
    Enrich all episodes belonging to content_row.
    Returns (updated, skipped, failed) counts.
    """
    content_id = content_row["id"]
    raw_title  = content_row.get("title", "")
    is_movie   = (content_row.get("type") or "series").lower() == "movie"
    title      = clean_title(raw_title)

    # Fetch episodes for this content
    try:
        ep_rows = (supabase.table("episodes")
                   .select("id,episode_number,season_number,title,description,thumbnail_url,duration_seconds")
                   .eq("content_id", content_id)
                   .execute().data or [])
    except Exception as e:
        log.warning("enrich_episodes fetch error for %s: %s", raw_title, e)
        return 0, 0, 1

    if not ep_rows:
        return 0, 0, 0

    # Get AniList data for streaming episode thumbnails
    a_item = anilist_search(title, is_movie=is_movie)
    ep_map = _get_streaming_episode_map(a_item)

    # Determine fallback thumbnail from content itself
    fallback_thumb = content_row.get("poster_url") or content_row.get("thumbnail_url") or ""
    # Get from AniList if not in content_row
    if not fallback_thumb and a_item:
        cover = a_item.get("coverImage", {}) or {}
        fallback_thumb = (cover.get("extraLarge") or cover.get("large") or
                          cover.get("medium") or "")

    # Duration: prefer AniList duration (per-episode minutes), else content duration_minutes
    ep_duration_seconds = None
    if a_item and a_item.get("duration"):
        ep_duration_seconds = int(a_item["duration"]) * 60
    elif content_row.get("duration_minutes"):
        ep_duration_seconds = int(content_row["duration_minutes"]) * 60

    updated = skipped = failed = 0

    for ep in ep_rows:
        ep_id    = ep["id"]
        ep_num   = ep.get("episode_number") or 0
        payload  = {}

        # thumbnail_url
        ep_info = ep_map.get(ep_num, {})
        new_thumb = ep_info.get("thumbnail") or fallback_thumb
        if new_thumb and (force_images or not ep.get("thumbnail_url")):
            payload["thumbnail_url"] = new_thumb

        # duration_seconds
        if ep_duration_seconds and not ep.get("duration_seconds"):
            payload["duration_seconds"] = ep_duration_seconds

        # description — only fill if empty (we don't have per-episode descriptions from APIs usually)
        if not ep.get("description") and fallback_thumb:
            # Leave description alone if no per-episode source available
            pass

        if not payload:
            skipped += 1
            continue

        if dry_run:
            log.info("    [dry] episode %s fields: %s", ep_num, list(payload.keys()))
            updated += 1
            continue

        try:
            supabase_admin.table("episodes").update(payload).eq("id", ep_id).execute()
            updated += 1
        except Exception as e:
            log.warning("    episode update error (%s ep%s): %s", raw_title, ep_num, e)
            failed += 1

    return updated, skipped, failed


def run_episode_enrichment(dry_run: bool = False,
                           limit: int | None = None,
                           title_filter: str | None = None,
                           force_images: bool = True,
                           progress_cb=None) -> dict:
    """Run episode enrichment across all (or filtered) content."""
    query = supabase.table("content").select(
        "id,title,type,poster_url,thumbnail_url,duration_minutes"
    )
    if title_filter:
        query = query.ilike("title", f"%{title_filter}%")
    query = query.order("created_at").range(0, (limit or 9999) - 1)
    rows  = query.execute().data or []

    total_content          = len(rows)
    total_ep_updated       = 0
    total_ep_skipped       = 0
    total_ep_failed        = 0
    content_done           = 0
    summaries: list[str]   = []

    for i, row in enumerate(rows, 1):
        if progress_cb:
            progress_cb(i, total_content, row.get("title", ""))
        try:
            u, s, f = enrich_episodes_for_content(row, dry_run=dry_run, force_images=force_images)
            total_ep_updated += u
            total_ep_skipped += s
            total_ep_failed  += f
            content_done     += 1
            if u or f:
                tag = "🔎" if dry_run else "✅"
                summaries.append(f"{tag} *{row['title']}* — {u} ep updated, {f} failed")
        except Exception as e:
            total_ep_failed += 1
            summaries.append(f"⚠️ *{row.get('title')}*: `{e}`")
            log.exception("run_episode_enrichment error")
        time.sleep(BATCH_PAUSE)

    return {
        "total_content":    total_content,
        "content_done":     content_done,
        "ep_updated":       total_ep_updated,
        "ep_skipped":       total_ep_skipped,
        "ep_failed":        total_ep_failed,
        "summaries":        summaries,
    }

# ── batch content runner ───────────────────────────────────────────────────────
def run_enrichment(dry_run: bool = False,
                   limit: int | None = None,
                   offset: int = 0,
                   title_filter: str | None = None,
                   force_images: bool = True,
                   progress_cb=None) -> dict:
    query = supabase.table("content").select(
        "id,title,type,description,release_year,rating,"
        "poster_url,banner_url,thumbnail_url,duration_minutes,status,featured"
    )
    if title_filter:
        query = query.ilike("title", f"%{title_filter}%")
    query = query.order("created_at").range(offset, offset + (limit or 9999) - 1)
    rows  = query.execute().data or []

    total   = len(rows)
    updated = skipped = failed = 0
    summaries: list[str] = []

    for i, row in enumerate(rows, 1):
        if progress_cb:
            progress_cb(i, total, row.get("title", ""))
        try:
            changed, summary = enrich_item(row, dry_run=dry_run, force_images=force_images)
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

# ── single-title preview ───────────────────────────────────────────────────────
def preview_title(title: str) -> dict:
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

# ── db stats ───────────────────────────────────────────────────────────────────
def get_db_stats() -> dict:
    all_rows  = supabase.table("content").select(
        "id,type,rating,poster_url,banner_url,description,status,featured"
    ).execute().data or []
    genres_ct = supabase.table("genres").select("id", count="exact").execute().count or 0
    ep_ct     = supabase.table("episodes").select("id", count="exact").execute().count or 0

    no_ep_thumb = 0
    try:
        no_ep_thumb = (supabase.table("episodes")
                       .select("id", count="exact")
                       .is_("thumbnail_url", "null")
                       .execute().count or 0)
    except Exception:
        pass

    total   = len(all_rows)
    movies  = sum(1 for r in all_rows if r.get("type") == "movie")
    series  = total - movies
    no_img  = sum(1 for r in all_rows if not r.get("poster_url"))
    no_ban  = sum(1 for r in all_rows if not r.get("banner_url"))
    no_desc = sum(1 for r in all_rows if not r.get("description"))
    no_rate = sum(1 for r in all_rows if not r.get("rating") or float(r.get("rating") or 0) == 0)
    featured = sum(1 for r in all_rows if r.get("featured"))
    return {
        "total": total, "series": series, "movies": movies,
        "no_poster": no_img, "no_banner": no_ban,
        "no_description": no_desc, "no_rating": no_rate,
        "genres": genres_ct, "episodes": ep_ct,
        "no_ep_thumb": no_ep_thumb, "featured": featured,
    }

# ── anime browser helpers (for user-facing bot) ────────────────────────────────
def browse_content(page: int = 1, page_size: int = 10,
                   search: str | None = None,
                   content_type: str | None = None) -> tuple[list, int]:
    """Returns (rows, total_count) for the user-facing browser."""
    offset = (page - 1) * page_size
    query  = supabase.table("content").select(
        "id,title,type,rating,poster_url,status,release_year",
        count="exact"
    )
    if search:
        query = query.ilike("title", f"%{search}%")
    if content_type:
        query = query.eq("type", content_type)
    query  = query.order("title").range(offset, offset + page_size - 1)
    result = query.execute()
    return result.data or [], result.count or 0


def get_content_detail(content_id: str) -> dict | None:
    """Full detail row for a single content item."""
    try:
        rows = (supabase.table("content")
                .select("id,title,description,type,release_year,rating,poster_url,"
                        "banner_url,thumbnail_url,duration_minutes,language,status,featured")
                .eq("id", content_id)
                .limit(1)
                .execute().data or [])
        if not rows:
            return None
        row = rows[0]
        # attach genres
        grows = (supabase.table("content_genres")
                 .select("genres(name)")
                 .eq("content_id", content_id)
                 .execute().data or [])
        row["genres"] = [g["genres"]["name"] for g in grows if g.get("genres")]
        # episode count
        ec = (supabase.table("episodes")
              .select("id", count="exact")
              .eq("content_id", content_id)
              .execute().count or 0)
        row["episode_count"] = ec
        return row
    except Exception as e:
        log.warning("get_content_detail error: %s", e)
        return None
