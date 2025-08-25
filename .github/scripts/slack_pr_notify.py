#!/usr/bin/env python3
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, parse_qs, urlencode
from datetime import datetime, timedelta

import requests

# =========================
# Env
# =========================
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
SLACK_CHANNEL_ID = os.environ.get("SLACK_CHANNEL_ID")
EVENT_PATH = os.environ.get("GITHUB_EVENT_PATH")
SC_WS_TOKEN = os.environ.get("SC_WS_TOKEN", "")  # optional

# GIBS config (overridable via env)
GIBS_LAYER = os.environ.get("GIBS_LAYER")  # if unset, try a set of defaults below
GIBS_DATE = os.environ.get("GIBS_DATE")    # YYYY-MM-DD
GIBS_BBOX = os.environ.get("GIBS_BBOX", "-180,-90,180,90")       # world
GIBS_SIZE = os.environ.get("GIBS_SIZE", "2048,1024")             # width,height

# Optional: a second (regional) GIBS image, e.g., South Island NZ
GIBS_SECONDARY_BBOX = os.environ.get("GIBS_SECONDARY_BBOX")      # e.g., "166,-47,172,-41"
GIBS_SECONDARY_SIZE = os.environ.get("GIBS_SECONDARY_SIZE", "1536,1024")

if not SLACK_BOT_TOKEN or not SLACK_CHANNEL_ID or not EVENT_PATH:
    print("Missing SLACK_BOT_TOKEN, SLACK_CHANNEL_ID, or GITHUB_EVENT_PATH", file=sys.stderr)
    sys.exit(1)

# =========================
# Regex detectors
# =========================
SC_REGEX       = re.compile(r"(https?://(?:www\.)?soundcloud\.com/[^\s)]+)", re.I)
NASA_REGEX     = re.compile(r"(https?://(?:www\.)?images\.nasa\.gov/details/[^\s)]+)", re.I)
YT_REGEX       = re.compile(r"(https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/)[^\s)]+)", re.I)

EARTHDATA_URL  = re.compile(r"(https?://(?:www\.)?search\.earthdata\.nasa\.gov/[^\s)]+)", re.I)
CMR_ID_REGEX   = re.compile(r"\b([CG]\d{3,}-[A-Za-z0-9_]+)\b")  # e.g. C12345-LPCLOUD, G9876543-NSIDC_ECS

# Popular True Color layers to try if GIBS_LAYER not set
GIBS_TRUECOLOR_CANDIDATES = [
    "MODIS_Terra_CorrectedReflectance_TrueColor",
    "MODIS_Aqua_CorrectedReflectance_TrueColor",
    "VIIRS_SNPP_CorrectedReflectance_TrueColor",
    "VIIRS_NOAA20_CorrectedReflectance_TrueColor",
]

# =========================
# Helpers
# =========================
def safe_get(d: Dict, *path, default=None):
    cur = d
    for p in path:
        if isinstance(cur, dict) and p in cur:
            cur = cur[p]
        else:
            return default
    return cur

def fetch_json(url: str, headers: Optional[Dict[str, str]] = None, timeout: int = 20) -> Optional[Dict[str, Any]]:
    try:
        r = requests.get(url, headers=headers or {"Accept": "application/json"}, timeout=timeout)
        if r.ok:
            return r.json()
        print(f"GET {url} -> HTTP {r.status_code}: {r.text[:200]}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"GET {url} failed: {e}", file=sys.stderr)
        return None

def post_json(url: str, payload: Dict[str, Any], headers: Optional[Dict[str, str]] = None, timeout: int = 20) -> Dict[str, Any]:
    try:
        r = requests.post(url, headers=headers or {"Content-Type": "application/json"}, json=payload, timeout=timeout)
        try:
            data = r.json()
        except Exception:
            data = {"raw": r.text}
        if not r.ok:
            print(f"POST {url} -> HTTP {r.status_code}: {str(data)[:400]}", file=sys.stderr)
        return data
    except Exception as e:
        print(f"POST {url} failed: {e}", file=sys.stderr)
        return {"ok": False, "error": str(e)}

def mask_token(token: str, visible: int = 0, mask_char: str = "*", min_mask: int = 8) -> str:
    if not token:
        return ""
    masked_len = max(len(token) - visible, min_mask)
    return mask_char * masked_len

# =========================
# CMR / Earthdata helpers
# =========================
def extract_cmr_ids_from_earthdata_url(u: str) -> List[str]:
    ids: List[str] = []
    try:
        qs = parse_qs(urlparse(u).query)
        # direct params
        for key in ("p", "g"):
            for v in qs.get(key, []):
                if CMR_ID_REGEX.match(v):
                    ids.append(v)
        # project-style nested params: pg[0][g]=G...
        for k, vals in qs.items():
            if k.startswith("pg") and "[g]" in k:
                for v in vals:
                    if CMR_ID_REGEX.match(v):
                        ids.append(v)
    except Exception:
        pass
    # de-duplicate, preserve order
    seen = set()
    unique = []
    for cid in ids:
        if cid not in seen:
            seen.add(cid)
            unique.append(cid)
    return unique

def cmr_concept_lookup(concept_id: str) -> Optional[Dict[str, Any]]:
    url = f"https://cmr.earthdata.nasa.gov/search/concepts/{concept_id}.json"
    return fetch_json(url, headers={"Accept": "application/json"})

def build_earthdata_card(concept_id: str, concept_json: Dict[str, Any]) -> Dict[str, Any]:
    obj = concept_json.get("Collection") or concept_json.get("Granule") or {}
    meta = concept_json.get("meta") or {}

    short_name = safe_get(obj, "ShortName", default="") or safe_get(obj, "CollectionReference", "ShortName", default="")
    version_id = safe_get(obj, "VersionId", default="")
    dataset_id = safe_get(obj, "DataSetId", default="")
    provider   = safe_get(meta, "provider-id", default="") or safe_get(obj, "ProviderId", default="")
    time_start = safe_get(obj, "Temporal", "RangeDateTime", "BeginningDateTime", default="") or safe_get(obj, "Temporal", "TemporalRangeType", default="")
    time_end   = safe_get(obj, "Temporal", "RangeDateTime", "EndingDateTime", default="")

    if concept_id.startswith("C"):
        ed_link = f"https://search.earthdata.nasa.gov/search?q={concept_id}&p={concept_id}"
        label   = "Open in Earthdata Search (Collection)"
    else:
        ed_link = f"https://search.earthdata.nasa.gov/search?q={concept_id}&g={concept_id}"
        label   = "Open in Earthdata Search (Granule)"

    lines = [f"*Earthdata / CMR:* `{concept_id}`"]
    if short_name or version_id:
        sv = f"{short_name or '—'}"
        if version_id:
            sv += f" / {version_id}"
        lines.append(f"*Short Name / Version:* {sv}")
    if dataset_id:
        lines.append(f"*Dataset ID:* {dataset_id}")
    if provider:
        lines.append(f"*Provider:* {provider}")
    if time_start or time_end:
        lines.append(f"*Temporal:* {time_start or '—'} — {time_end or '—'}")
    lines.append(f"<{ed_link}|{label}>")

    return {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}}

# =========================
# GIBS helpers
# =========================
def gibs_wms_url(layer: str, date_str: str, bbox: str, size: str) -> str:
    w, h = [s.strip() for s in size.split(",")]
    params = {
        "service": "WMS",
        "request": "GetMap",
        "version": "1.1.1",
        "layers": layer,
        "styles": "",
        "format": "image/png",
        "transparent": "false",
        "srs": "EPSG:4326",
        "bbox": bbox,
        "width": w,
        "height": h,
        "time": date_str,
    }
    return f"https://gibs.earthdata.nasa.gov/wms/epsg4326/best/wms.cgi?{urlencode(params)}"

def try_gibs_image(bbox: str, size: str) -> Tuple[Optional[str], Optional[str]]:
    layers = [GIBS_LAYER] if GIBS_LAYER else GIBS_TRUECOLOR_CANDIDATES
    if GIBS_DATE:
        date_candidates = [GIBS_DATE]
    else:
        today = datetime.utcnow().date()
        date_candidates = [(today - timedelta(days=d)).isoformat() for d in range(0, 3)]
    for lyr in layers:
        if not lyr:
            continue
        for d in date_candidates:
            url = gibs_wms_url(lyr, d, bbox, size)
            try:
                r = requests.head(url, timeout=15)
                if not r.ok or "image" not in r.headers.get("Content-Type", ""):
                    r = requests.get(url, stream=True, timeout=20)
                if r.ok and "image" in r.headers.get("Content-Type", ""):
                    return url, f"{lyr} ({d})"
            except Exception:
                continue
    return None, None

# =========================
# Load PR event
# =========================
with open(EVENT_PATH, "r", encoding="utf-8") as f:
    event = json.load(f)

pr = event.get("pull_request", {}) or {}
title = pr.get("title", "") or ""
body  = pr.get("body", "") or ""
url   = pr.get("html_url", "") or ""
author = safe_get(pr, "user", "login", default="unknown") or "unknown"
head = safe_get(pr, "head", "ref", default="?") or "?"
base = safe_get(pr, "base", "ref", default="?") or "?"

haystack = f"{title}\n\n{body}"

# =========================
# 1) SoundCloud → Odesli
# =========================
sc_match = SC_REGEX.search(haystack)
odesli_url: Optional[str] = None
platforms: List[Tuple[str, str]] = []

if sc_match:
    sc_url = sc_match.group(1)
    odesli_api = f"https://api.song.link/v1-alpha.1/links?url={requests.utils.quote(sc_url, safe='')}"
    data = fetch_json(odesli_api)
    if data:
        odesli_url = data.get("pageUrl") or None
        for name, meta in (data.get("linksByPlatform") or {}).items():
            out_url = meta.get("url")
            if out_url:
                platforms.append((name, out_url))

# =========================
# 2) NASA images.nasa.gov details card
# =========================
nasa_match = NASA_REGEX.search(haystack)
nasa_card: Optional[Dict[str, Any]] = None

if nasa_match:
    try:
        nasa_url = nasa_match.group(1)
        media_id = nasa_url.split("/details/")[1]
    except Exception:
        media_id = None

    if media_id:
        search_api = f"https://images-api.nasa.gov/search?nasa_id={requests.utils.quote(media_id, safe='')}"
        nasa_json = fetch_json(search_api)
        try:
            item = (safe_get(nasa_json or {}, "collection", "items", default=[]) or [])[0]
        except Exception:
            item = None

        if item:
            meta = (item.get("data") or [{}])[0]
            links = item.get("links") or []
            thumb = None
            for l in links:
                if l.get("rel") == "preview" or l.get("render") == "image":
                    thumb = l.get("href")
                    break
            nasa_card = {
                "title": meta.get("title") or media_id,
                "desc": meta.get("description") or "",
                "date": meta.get("date_created") or "",
                "center": meta.get("center") or "",
                "nasaId": meta.get("nasa_id") or media_id,
                "thumb": thumb,
                "canonical": f"https://images.nasa.gov/details/{media_id}",
            }

# =========================
# 3) YouTube link
# =========================
yt_match = YT_REGEX.search(haystack)

# =========================
# 4) Earthdata / CMR concepts
# =========================
earthdata_urls = EARTHDATA_URL.findall(haystack)
cmr_ids_from_urls: List[str] = []
for u in earthdata_urls:
    cmr_ids_from_urls.extend(extract_cmr_ids_from_earthdata_url(u))

cmr_ids_in_text = CMR_ID_REGEX.findall(haystack)
cmr_all_ids: List[str] = []
seen_ids = set()
for cid in cmr_ids_from_urls + cmr_ids_in_text:
    if cid not in seen_ids:
        seen_ids.add(cid)
        cmr_all_ids.append(cid)

earthdata_blocks: List[Dict[str, Any]] = []
for cid in cmr_all_ids[:5]:
    cj = cmr_concept_lookup(cid)
    if not cj:
        continue
    try:
        earthdata_blocks.append(build_earthdata_card(cid, cj))
    except Exception as e:
        print(f"Failed to build Earthdata card for {cid}: {e}", file=sys.stderr)

# =========================
# 5) WebSocket snippet (masked token)
# =========================
if SC_WS_TOKEN:
    snippet = "\n".join([
        "```js",
        "const signalingChannel = new WebSocket(",
        f"  'wss://api.soundcloud.com/realtime?token={mask_token(SC_WS_TOKEN)}'",
        ");",
        "",
        "signalingChannel.onopen = () => {",
        "  console.log('WebSocket connection opened.');",
        "};",
        "",
        "signalingChannel.onmessage = (event) => {",
        "  console.log('Received:', event.data);",
        "};",
        "```",
        "",
        "_Runtime note: The real token is injected at runtime from the secret `SC_WS_TOKEN` — not shown here._"
    ])
else:
    snippet = "_No SC WebSocket token provided; skipping snippet._"

# =========================
# 6) Build Slack blocks
# =========================
blocks: List[Dict[str, Any]] = [
    {
        "type": "section",
        "text": {"type": "mrkdwn",
                 "text": f"*PR:* <{url}|{title}>\n*Author:* {author}\n*Branch:* `{head}` → `{base}`"}
    }
]

if sc_match:
    sc_url = sc_match.group(1)
    blocks.append({"type": "divider"})
    text = f"*Detected SoundCloud URL:*\n{sc_url}\n"
    text += f"*Songlink:* <{odesli_url}|Open universal link>" if odesli_url else "_Songlink could not be resolved_"
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text}})
    if platforms:
        plat_lines = "\n".join([f"• *{name}:* <{u}|open>" for name, u in platforms])
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": plat_lines}})

if nasa_card:
    blocks.append({"type": "divider"})
    if nasa_card.get("thumb"):
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn",
                     "text": (
                         f"*NASA Media:*\n"
                         f"*Title:* {nasa_card['title']}\n"
                         f"*Date:* {nasa_card['date']}\n"
                         f"*Center:* {nasa_card['center']}\n"
                         f"<{nasa_card['canonical']}|Open on images.nasa.gov>"
                     )},
            "accessory": {
                "type": "image",
                "image_url": nasa_card["thumb"],
                "alt_text": "NASA media thumbnail"
            }
        })
    else:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn",
                     "text": f"*NASA Media:*\n*Title:* {nasa_card['title']}\n<{nasa_card['canonical']}|Open on images.nasa.gov>"}
        })

if yt_match:
    blocks.append({"type": "divider"})
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*YouTube:* {yt_match.group(1)}"}})

if earthdata_blocks:
    blocks.append({"type": "divider"})
    blocks.extend(earthdata_blocks)

# --- GIBS world image ---
gibs_url, gibs_label = try_gibs_image(GIBS_BBOX, GIBS_SIZE)
if gibs_url:
    blocks.append({"type": "divider"})
    blocks.append({
        "type": "image",
        "image_url": gibs_url,
        "alt_text": "GIBS global True Color",
        "title": {"type": "plain_text", "text": f"GIBS World — {gibs_label}", "emoji": True}
    })
    blocks.append({
        "type": "context",
        "elements": [
            {"type": "mrkdwn", "text": f"*BBOX:* `{GIBS_BBOX}`  *Size:* `{GIBS_SIZE}`"}
        ]
    })

# --- Optional: secondary regional GIBS image (e.g., South Island NZ) ---
if GIBS_SECONDARY_BBOX:
    gibs_url2, gibs_label2 = try_gibs_image(GIBS_SECONDARY_BBOX, GIBS_SECONDARY_SIZE)
    if gibs_url2:
        blocks.append({"type": "divider"})
        blocks.append({
            "type": "image",
            "image_url": gibs_url2,
            "alt_text": "GIBS regional True Color",
            "title": {"type": "plain_text", "text": f"GIBS Region — {gibs_label2}", "emoji": True}
        })
        blocks.append({
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": f"*BBOX:* `{GIBS_SECONDARY_BBOX}`  *Size:* `{GIBS_SECONDARY_SIZE}`"}
            ]
        })

blocks.append({"type": "divider"})
blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*SoundCloud WebSocket snippet (masked token):*\n{snippet}"}})

# =========================
# 7) Post to Slack
# =========================
slack_payload = {"channel": SLACK_CHANNEL_ID, "text": f"PR: {title}", "blocks": blocks}
slack_headers = {"Content-Type": "application/json; charset=utf-8", "Authorization": f"Bearer {SLACK_BOT_TOKEN}"}

resp = post_json("https://slack.com/api/chat.postMessage", slack_payload, headers=slack_headers)
ok = bool(resp.get("ok"))
if not ok:
    print(f"Slack API error: {resp}", file=sys.stderr)
    sys.exit(1)
else:
    print(json.dumps({"posted_to": resp.get("channel"), "ts": resp.get("ts")}))