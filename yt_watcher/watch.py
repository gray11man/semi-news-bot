# -*- coding: utf-8 -*-
"""
YouTube AI-인터뷰 감시 봇 (채널 전용 / 쇼츠 제외 / 중복제거 / 시간필터)
  - 양질 채널의 업로드 재생목록(UULF) RSS만 감시 → 쇼츠 자동 제외, 롱폼만
  - LOOKBACK_HOURS 안에 올라온 영상만 신규로 간주 (3시간 주기 대응)
  - seen_videos.json 으로 video_id 중복제거 (이미 보낸 건 절대 재전송 안 함)
  - 제목 한글 번역 (Gemini, 기존 봇과 동일 방식)
환경변수: YOUTUBE_API_KEY, GEMINI_KEY, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
"""
import os
import re
import json
import time
import html
import datetime as dt
import xml.etree.ElementTree as ET
import requests

from watch_config import CHANNELS, LOOKBACK_HOURS

# ── 환경변수 (기존 semi-news-bot repo Secret 이름에 맞춤) ──
YT_KEY     = os.environ["YOUTUBE_API_KEY"]
GEMINI_KEY = os.environ.get("GEMINI_KEY", "")
TG_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TG_CHAT    = os.environ["TELEGRAM_CHAT_ID"]
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

SEEN_PATH = os.environ.get("SEEN_PATH", "seen_videos.json")
SEEN_MAX  = 3000

YT_SEARCH = "https://www.googleapis.com/youtube/v3/search"
YT_RSS    = "https://www.youtube.com/feeds/videos.xml?playlist_id={pid}"
GEMINI_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
              "{model}:generateContent")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

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


# ──────────────── channel_id → 업로드 재생목록 ID(UULF) ────────────────
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
    # UC... 채널ID의 앞 UC를 UULF로 바꾸면 '쇼츠 제외 업로드 재생목록'
    if channel_id and channel_id.startswith("UC"):
        return "UULF" + channel_id[2:]
    return None


def fetch_channel_rss(channel):
    cid = _resolve_channel_id(channel)
    pid = _uploads_playlist_id(cid)
    if not pid:
        print(f"[rss] {channel['name']}: id 못 찾음, skip")
        return []
    out = []
    try:
        r = requests.get(YT_RSS.format(pid=pid),
                         headers={"User-Agent": UA}, timeout=20)
        if r.status_code != 200:
            print(f"[rss] {channel['name']}: HTTP {r.status_code}")
            return []
        root = ET.fromstring(r.content)
        ns = {"a": "http://www.w3.org/2005/Atom",
              "yt": "http://www.youtube.com/xml/schemas/2015"}
        for entry in root.findall("a:entry", ns):
            vid_el = entry.find("yt:videoId", ns)
            if vid_el is None:
                continue
            vid = vid_el.text
            title = (entry.find("a:title", ns).text or "").strip()
            pub_el = entry.find("a:published", ns)
            if pub_el is None:
                continue
            pub = dt.datetime.fromisoformat(pub_el.text.replace("Z", "+00:00"))
            # ── 시간 필터: LOOKBACK 안에 올라온 것만 ──
            if pub < CUTOFF:
                continue
            out.append({
                "video_id": vid,
                "title": title,
                "channel": channel["name"],
                "published": pub,
            })
    except Exception as e:
        print(f"[rss] {channel['name']} err: {e}")
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

    # 1) 수집 (채널별 RSS, 시간필터 적용됨)
    collected = {}
    for ch in CHANNELS:
        for it in fetch_channel_rss(ch):
            collected.setdefault(it["video_id"], it)   # 같은 영상 1회만

    # 2) 중복제거 — 이미 보낸 video_id 제외
    fresh = [it for vid, it in collected.items() if vid not in seen_ids]
    fresh.sort(key=lambda x: x["published"])
    print(f"수집 {len(collected)}건 / 신규 {len(fresh)}건 "
          f"(기준 최근 {LOOKBACK_HOURS}시간)")

    # 3) 전송 + seen 기록
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
