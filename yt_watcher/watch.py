# -*- coding: utf-8 -*-
"""
YouTube AI-인터뷰 감시 봇 (API 방식 / 쇼츠 제외 / 중복제거 / 시간필터)
  - 축1: 채널 화이트리스트 (playlistItems.list, UULF 업로드 재생목록 → 쇼츠 제외 롱폼만)
  - 축2: 인물/직함 기반 검색 (search.list, 구독자수/길이/블랙워드 안전필터 적용)
환경변수: YOUTUBE_API_KEY, GEMINI_KEY, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
        RUN_PERSON_SEARCH=1 일 때만 축2(인물검색) 실행
"""
import os
import re
import json
import time
import html
import datetime as dt
import requests

from watch_config import (
    CHANNELS, LOOKBACK_HOURS,
    PEOPLE, MIN_SUBSCRIBERS, MIN_DURATION_SEC,
    BLOCK_KEYWORDS, BLOCKED_CHANNEL_IDS, PERSON_SEARCH_LOOKBACK_HOURS,
    RELEVANCE_KEYWORDS,
)

YT_KEY     = os.environ["YOUTUBE_API_KEY"]
GEMINI_KEY = os.environ.get("GEMINI_KEY", "")
TG_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TG_CHAT    = os.environ["TELEGRAM_CHAT_ID"]
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

SEEN_PATH = os.environ.get("SEEN_PATH", "seen_videos.json")
SEEN_MAX  = 3000

RUN_PERSON_SEARCH = os.environ.get("RUN_PERSON_SEARCH", "0") == "1"

YT_SEARCH        = "https://www.googleapis.com/youtube/v3/search"
YT_CHANNELS      = "https://www.googleapis.com/youtube/v3/channels"
YT_PLAYLISTITEMS = "https://www.googleapis.com/youtube/v3/playlistItems"
YT_VIDEOS        = "https://www.googleapis.com/youtube/v3/videos"
GEMINI_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
              "{model}:generateContent")

NOW           = dt.datetime.now(dt.timezone.utc)
CUTOFF        = NOW - dt.timedelta(hours=LOOKBACK_HOURS)
PERSON_CUTOFF = NOW - dt.timedelta(hours=PERSON_SEARCH_LOOKBACK_HOURS)


# ────────────────────────── dedup 상태 ──────────────────────────
def load_seen():
    try:
        with open(SEEN_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {"ids": []}
    except (FileNotFoundError, json.JSONDecodeError):
        return {"ids": []}


def save_seen(seen):
    seen["ids"] = seen["ids"][-SEEN_MAX:]
    with open(SEEN_PATH, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=0)


# ──────────────── 축1: channel 식별 → 업로드 재생목록 ID ────────────────
def _resolve_channel_id(channel):
    if channel.get("channel_id"):
        return channel["channel_id"]
    handle = channel.get("handle")
    if not handle:
        return None
    try:
        r = requests.get(YT_SEARCH, params={
            "key": YT_KEY, "part": "snippet", "type": "channel",
            "q": handle, "maxResults": 1}, timeout=20)
        r.raise_for_status()
        items = r.json().get("items", [])
        if items:
            return items[0]["snippet"]["channelId"]
    except Exception as e:
        print(f"[resolve] {channel['name']} err: {e}")
    return None


def _uploads_playlist_id(channel_id):
    if channel_id and channel_id.startswith("UC"):
        return "UULF" + channel_id[2:]
    return None


def fetch_channel_uploads(channel):
    cid = _resolve_channel_id(channel)
    pid = _uploads_playlist_id(cid)
    if not pid:
        print(f"[yt] {channel['name']}: id 못 찾음, skip")
        return []
    out = []
    try:
        r = requests.get(YT_PLAYLISTITEMS, params={
            "key": YT_KEY, "part": "snippet,contentDetails",
            "playlistId": pid, "maxResults": 15}, timeout=20)
        if r.status_code != 200:
            print(f"[yt] {channel['name']}: playlistItems {r.status_code}, 채널API 재시도")
            pid2 = _uploads_via_channel_api(cid)
            if not pid2 or pid2 == pid:
                return []
            r = requests.get(YT_PLAYLISTITEMS, params={
                "key": YT_KEY, "part": "snippet,contentDetails",
                "playlistId": pid2, "maxResults": 15}, timeout=20)
            if r.status_code != 200:
                print(f"[yt] {channel['name']}: 재시도도 {r.status_code}")
                return []
        for it in r.json().get("items", []):
            sn = it["snippet"]
            cd = it.get("contentDetails", {})
            vid = cd.get("videoId") or sn.get("resourceId", {}).get("videoId")
            if not vid:
                continue
            pub_str = cd.get("videoPublishedAt") or sn.get("publishedAt")
            pub = dt.datetime.fromisoformat(pub_str.replace("Z", "+00:00"))
            if pub < CUTOFF:
                continue
            out.append({
                "video_id": vid,
                "title": html.unescape(sn["title"]),
                "channel": channel["name"],
                "published": pub,
            })
    except Exception as e:
        print(f"[yt] {channel['name']} err: {e}")
    return out


def _uploads_via_channel_api(channel_id):
    if not channel_id:
        return None
    try:
        r = requests.get(YT_CHANNELS, params={
            "key": YT_KEY, "part": "contentDetails",
            "id": channel_id}, timeout=20)
        r.raise_for_status()
        items = r.json().get("items", [])
        if items:
            return items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
    except Exception as e:
        print(f"[channelapi] err: {e}")
    return None


# ──────────────── 축2: 인물/직함 기반 검색 (안전필터 포함) ────────────────
def _parse_duration(iso_dur):
    """ISO8601 'PT1H2M3S' → 초 단위 정수"""
    m = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', iso_dur or "")
    if not m:
        return 0
    h, mi, s = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mi * 60 + s


def _fetch_subscriber_counts(channel_ids):
    if not channel_ids:
        return {}
    try:
        r = requests.get(YT_CHANNELS, params={
            "key": YT_KEY, "part": "statistics",
            "id": ",".join(channel_ids)}, timeout=20)
        r.raise_for_status()
        return {it["id"]: int(it.get("statistics", {}).get("subscriberCount", 0) or 0)
                for it in r.json().get("items", [])}
    except Exception as e:
        print(f"[subs] err: {e}")
        return {}


def _has_blocked_keyword(text):
    """제목+설명 합쳐서 블랙워드 검사 (대소문자 무시)"""
    t = text.lower()
    return any(bad.lower() in t for bad in BLOCK_KEYWORDS)


# 짧은 영단어(AI, SK 등)는 단어경계로, 그 외(한글/긴 영단어)는 substring으로 검사
_RELEVANCE_PATTERNS = [
    re.compile(r'\b' + re.escape(kw) + r'\b', re.IGNORECASE)
    if kw.isascii() and len(kw) <= 4
    else re.compile(re.escape(kw), re.IGNORECASE)
    for kw in RELEVANCE_KEYWORDS
]


def is_relevant(text):
    """제목(필요시 설명 포함)에 관심 키워드가 하나라도 있는지 검사."""
    return any(p.search(text) for p in _RELEVANCE_PATTERNS)


def fetch_person_videos(query):
    """인물/직함 쿼리로 search.list 검색 → 구독자수/영상길이/블랙워드(제목+설명) 필터링."""
    out = []
    try:
        r = requests.get(YT_SEARCH, params={
            "key": YT_KEY, "part": "snippet", "type": "video",
            "q": query, "order": "date",
            "publishedAfter": PERSON_CUTOFF.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "maxResults": 10}, timeout=20)
        r.raise_for_status()
        items = r.json().get("items", [])
    except Exception as e:
        print(f"[person] '{query}' search err: {e}")
        return out

    vids = [it["id"]["videoId"] for it in items if it.get("id", {}).get("videoId")]
    if not vids:
        return out

    try:
        r2 = requests.get(YT_VIDEOS, params={
            "key": YT_KEY, "part": "contentDetails,snippet",
            "id": ",".join(vids)}, timeout=20)
        r2.raise_for_status()
        vinfo = {v["id"]: v for v in r2.json().get("items", [])}
    except Exception as e:
        print(f"[person] '{query}' videos.list err: {e}")
        return out

    channel_ids = {v["snippet"]["channelId"] for v in vinfo.values()}
    subs = _fetch_subscriber_counts(channel_ids)

    for vid, v in vinfo.items():
        sn = v["snippet"]
        cid = sn["channelId"]

        if cid in BLOCKED_CHANNEL_IDS:
            continue
        if subs.get(cid, 0) < MIN_SUBSCRIBERS:
            continue

        dur = _parse_duration(v.get("contentDetails", {}).get("duration"))
        if dur < MIN_DURATION_SEC:
            continue

        title = sn.get("title", "")
        description = sn.get("description", "")
        # ── 제목 + 설명 둘 다 블랙워드 검사 (매집포착/초VIP/문자영업 등 리딩방 차단) ──
        if _has_blocked_keyword(title) or _has_blocked_keyword(description):
            continue
        # ── 전화번호 패턴(010-XXXX-XXXX 등) 들어있으면 영업성으로 간주, 차단 ──
        if re.search(r'\d{2,3}[-.\s]\d{3,4}[-.\s]\d{4}', description):
            continue

        pub = dt.datetime.fromisoformat(sn["publishedAt"].replace("Z", "+00:00"))
        if pub < PERSON_CUTOFF:
            continue

        out.append({
            "video_id": vid,
            "title": html.unescape(title),
            "channel": f"🔍 {sn['channelTitle']} ({query})",
            "published": pub,
        })
    return out


# ──────────────── 제목 한글 번역 (기존 봇과 동일 방식) ────────────────
def translate_title(title):
    hangul = len(re.findall(r"[가-힣]", title))
    if not GEMINI_KEY or hangul >= len(title.replace(" ", "")) * 0.4:
        return None
    prompt = ("다음 유튜브 영상 제목을 자연스러운 한국어로 번역해줘. "
              "고유명사(인명/회사명)는 그대로 두고, 설명 없이 번역문 한 줄만 출력:\n"
              + title)
    try:
        r = requests.post(
            GEMINI_URL.format(model=GEMINI_MODEL),
            headers={"x-goog-api-key": GEMINI_KEY,
                     "Content-Type": "application/json"},
            json={"contents": [{"parts": [{"text": prompt}]}],
                  "generationConfig": {
                      "temperature": 0.2,
                      "maxOutputTokens": 256,
                      "thinkingConfig": {"thinkingBudget": 0}}},
            timeout=20)
        r.raise_for_status()
        t = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        return t.split("\n")[0].strip()
    except Exception as e:
        print(f"[gemini] err: {e}")
        return None


# ────────────────────────── 텔레그램 ──────────────────────────
def send_telegram(item):
    url = f"https://www.youtube.com/watch?v={item['video_id']}"
    ko = item.get("title_ko")
    title_line = f"<b>{html.escape(ko)}</b>" if ko else f"<b>{html.escape(item['title'])}</b>"
    orig = "" if not ko else f"\n<i>{html.escape(item['title'])}</i>"
    msg = (f"📺 {html.escape(item['channel'])}\n"
           f"{title_line}{orig}\n"
           f"{url}")
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": msg,
                  "parse_mode": "HTML",
                  "disable_web_page_preview": False},
            timeout=20)
    except Exception as e:
        print(f"[tg] err: {e}")


# ────────────────────────── 메인 ──────────────────────────
def main():
    seen = load_seen()
    seen_ids = set(seen["ids"])

    collected = {}

    # 축1: 채널 화이트리스트 (매 실행마다 항상 동작)
    for ch in CHANNELS:
        for it in fetch_channel_uploads(ch):
            collected.setdefault(it["video_id"], it)

    # 축2: 인물/직함 검색 (RUN_PERSON_SEARCH=1 일 때만)
    if RUN_PERSON_SEARCH:
        for query in PEOPLE:
            for it in fetch_person_videos(query):
                collected.setdefault(it["video_id"], it)
            time.sleep(0.3)

    fresh = [it for vid, it in collected.items() if vid not in seen_ids]
    fresh.sort(key=lambda x: x["published"])

    before_n = len(fresh)
    fresh = [it for it in fresh if is_relevant(it["title"])]
    filtered_out = before_n - len(fresh)

    axis2_msg = f"인물축 {PERSON_SEARCH_LOOKBACK_HOURS}h" if RUN_PERSON_SEARCH else "인물축 OFF"
    print(f"수집 {len(collected)}건 / 신규(필터전) {before_n}건 / "
          f"관련성필터로 {filtered_out}건 제외 / 최종발송 {len(fresh)}건 "
          f"(채널축 {LOOKBACK_HOURS}h / {axis2_msg})")

    for it in fresh:
        it["title_ko"] = translate_title(it["title"])
        send_telegram(it)
        seen_ids.add(it["video_id"])
        seen["ids"].append(it["video_id"])
        time.sleep(1)

    save_seen(seen)
    print("완료")


if __name__ == "__main__":
    main()
