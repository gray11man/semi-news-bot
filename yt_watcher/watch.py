# -*- coding: utf-8 -*-
"""
YouTube AI-인터뷰 감시 봇 (API 방식 / 쇼츠 제외 / 중복제거 / 시간필터)
  - playlistItems.list 로 각 채널 업로드 재생목록(UULF) 조회 → GitHub Actions에서도 차단 안 됨
  - UULF 재생목록은 쇼츠 제외, 롱폼 업로드만 포함
  - LOOKBACK_HOURS 안에 올라온 영상만 신규 (3시간 주기 대응)
  - seen_videos.json 으로 video_id 중복제거
  - 제목 한글 번역 (Gemini, 기존 봇과 동일 방식)
환경변수: YOUTUBE_API_KEY, GEMINI_KEY, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
"""
import os
import re
import json
import time
import html
import datetime as dt
import requests

from watch_config import CHANNELS, LOOKBACK_HOURS

YT_KEY     = os.environ["YOUTUBE_API_KEY"]
GEMINI_KEY = os.environ.get("GEMINI_KEY", "")
TG_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TG_CHAT    = os.environ["TELEGRAM_CHAT_ID"]
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

SEEN_PATH = os.environ.get("SEEN_PATH", "seen_videos.json")
SEEN_MAX  = 3000

YT_SEARCH        = "https://www.googleapis.com/youtube/v3/search"
YT_CHANNELS      = "https://www.googleapis.com/youtube/v3/channels"
YT_PLAYLISTITEMS = "https://www.googleapis.com/youtube/v3/playlistItems"
GEMINI_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
              "{model}:generateContent")

NOW    = dt.datetime.now(dt.timezone.utc)
CUTOFF = NOW - dt.timedelta(hours=LOOKBACK_HOURS)


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


# ──────────────── channel 식별 → 업로드 재생목록 ID ────────────────
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
    # UC... → UULF... (업로드 재생목록, 쇼츠 제외 롱폼)
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
            # UULF가 안 먹으면 채널 API로 정확한 업로드ID 재시도
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
            # ── 시간 필터 ──
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
    for ch in CHANNELS:
        for it in fetch_channel_uploads(ch):
            collected.setdefault(it["video_id"], it)

    fresh = [it for vid, it in collected.items() if vid not in seen_ids]
    fresh.sort(key=lambda x: x["published"])
    print(f"수집 {len(collected)}건 / 신규 {len(fresh)}건 "
          f"(기준 최근 {LOOKBACK_HOURS}시간)")

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
