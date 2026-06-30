# -*- coding: utf-8 -*-
"""
YouTube AI-인터뷰 감시 봇
  - 축1 채널 RSS + 축2 인물검색 + 축3 키워드검색 → 신규 영상 수집
  - dedup: seen_videos.json (video_id 저장, repo 커밋으로 영속)
  - 제목 한글 번역 (Gemini)
  - 텔레그램 발송

환경변수(Secrets):
  YOUTUBE_API_KEY, GEMINI_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""
import os
import re
import json
import time
import html
import datetime as dt
from urllib.parse import quote_plus
import xml.etree.ElementTree as ET
import requests

from watch_config import CHANNELS, PEOPLE, TOPIC_KEYWORDS, LOOKBACK_HOURS

# ── 환경변수 (기존 semi-news-bot repo의 Secret 이름에 맞춤) ──
YT_KEY    = os.environ["YOUTUBE_API_KEY"]
GEMINI_KEY= os.environ.get("GEMINI_KEY", "")
TG_TOKEN  = os.environ["TELEGRAM_TOKEN"]
TG_CHAT   = os.environ["TELEGRAM_CHAT_ID"]

SEEN_PATH = os.environ.get("SEEN_PATH", "seen_videos.json")
SEEN_MAX  = 3000  # 오래된 id는 잘라서 파일 비대화 방지

YT_SEARCH = "https://www.googleapis.com/youtube/v3/search"
YT_RSS    = "https://www.youtube.com/feeds/videos.xml?channel_id={cid}"
GEMINI_URL= ("https://generativelanguage.googleapis.com/v1beta/models/"
             "{model}:generateContent")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

NOW = dt.datetime.now(dt.timezone.utc)
CUTOFF = NOW - dt.timedelta(hours=LOOKBACK_HOURS)


# ────────────────────────────────────────────────
# dedup 상태
# ────────────────────────────────────────────────
def load_seen():
    try:
        with open(SEEN_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"ids": []}

def save_seen(seen):
    seen["ids"] = seen["ids"][-SEEN_MAX:]
    with open(SEEN_PATH, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=0)


# ────────────────────────────────────────────────
# 축1: 채널 RSS (쿼터 0)
# ────────────────────────────────────────────────
def _resolve_channel_id(channel):
    """channel_id가 있으면 그대로, 없으면 handle로 search.list 1회 조회."""
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


def fetch_channel_rss(channel):
    cid = _resolve_channel_id(channel)
    if not cid:
        print(f"[rss] {channel['name']}: channel_id 못 찾음, skip")
        return []
    out = []
    try:
        r = requests.get(YT_RSS.format(cid=cid), timeout=20)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        ns = {"a": "http://www.w3.org/2005/Atom",
              "yt": "http://www.youtube.com/xml/schemas/2015",
              "media": "http://search.yahoo.com/mrss/"}
        for entry in root.findall("a:entry", ns):
            vid = entry.find("yt:videoId", ns).text
            title = entry.find("a:title", ns).text or ""
            published = entry.find("a:published", ns).text
            pub = dt.datetime.fromisoformat(published.replace("Z", "+00:00"))
            if pub < CUTOFF:
                continue
            out.append({
                "video_id": vid, "title": title,
                "channel": channel["name"], "published": pub,
                "source": "channel", "tag": "📺채널", "bear": False,
            })
    except Exception as e:
        print(f"[rss] {channel['name']} err: {e}")
    return out


# ────────────────────────────────────────────────
# 축2/3: search.list 폴링
# ────────────────────────────────────────────────
def yt_search(query, label, tag):
    params = {
        "key": YT_KEY, "part": "snippet", "type": "video",
        "order": "date", "maxResults": 10,
        "q": query,
        "publishedAfter": CUTOFF.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    out = []
    try:
        r = requests.get(YT_SEARCH, params=params, timeout=20)
        r.raise_for_status()
        for it in r.json().get("items", []):
            sn = it["snippet"]
            out.append({
                "video_id": it["id"]["videoId"],
                "title": html.unescape(sn["title"]),
                "channel": sn["channelTitle"],
                "published": dt.datetime.fromisoformat(
                    sn["publishedAt"].replace("Z", "+00:00")),
                "source": label, "tag": tag,
            })
    except Exception as e:
        print(f"[search] '{query}' err: {e}")
    return out


# ────────────────────────────────────────────────
# 제목 한글 번역 (Gemini). 키 없으면 원문 그대로.
# ────────────────────────────────────────────────
def translate_title(title):
    # 이미 한글이 절반 이상이면 번역 skip
    hangul = len(re.findall(r"[가-힣]", title))
    if not GEMINI_KEY or hangul >= len(title.replace(" ", "")) * 0.4:
        return None
    prompt = (
        "다음 유튜브 영상 제목을 자연스러운 한국어로 번역해줘. "
        "고유명사(인명/회사명)는 그대로 두고, 설명 없이 번역문 한 줄만 출력:\n"
        + title
    )
    try:
        r = requests.post(
            GEMINI_URL.format(model=GEMINI_MODEL),
            headers={"x-goog-api-key": GEMINI_KEY,
                     "Content-Type": "application/json"},
            json={"contents": [{"parts": [{"text": prompt}]}],
                  "generationConfig": {
                      "temperature": 0.2,
                      "maxOutputTokens": 256,
                      "thinkingConfig": {"thinkingBudget": 0},
                  }},
            timeout=20)
        r.raise_for_status()
        t = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        return t.split("\n")[0].strip()
    except Exception as e:
        print(f"[gemini] err: {e}")
        return None


# ────────────────────────────────────────────────
# 텔레그램 발송
# ────────────────────────────────────────────────
def send_telegram(item):
    url = f"https://www.youtube.com/watch?v={item['video_id']}"
    ko = item.get("title_ko")
    title_line = f"<b>{html.escape(ko)}</b>" if ko else f"<b>{html.escape(item['title'])}</b>"
    orig = "" if not ko else f"\n<i>{html.escape(item['title'])}</i>"
    bear = " ⚠️<b>[약세/경고]</b>" if item.get("bear") else ""
    msg = (
        f"{item['tag']}{bear}\n"
        f"{title_line}{orig}\n"
        f"📡 {html.escape(item['channel'])}\n"
        f"{url}"
    )
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": msg,
                  "parse_mode": "HTML",
                  "disable_web_page_preview": False},
            timeout=20)
    except Exception as e:
        print(f"[tg] err: {e}")


# ────────────────────────────────────────────────
# 메인
# ────────────────────────────────────────────────
def main():
    seen = load_seen()
    seen_ids = set(seen["ids"])
    collected = {}  # video_id -> item (dedup)

    # 축1 채널
    for ch in CHANNELS:
        for it in fetch_channel_rss(ch):
            collected.setdefault(it["video_id"], it)

    # 축2 인물
    for p in PEOPLE:
        items = yt_search(f"\"{p['name']}\"", "person",
                          "🐻인물" if p.get("bear") else "🎙️인물")
        for it in items:
            # 노이즈 컷: 이름의 모든 단어가 '제목'에 실제로 있어야 통과
            title_low = it["title"].lower()
            name_parts = [w for w in p["name"].lower().replace(".", " ").split() if len(w) > 1]
            if not all(part in title_low for part in name_parts):
                continue
            it["bear"] = p.get("bear", False)
            collected.setdefault(it["video_id"], it)

    # 축3 키워드
    for side, kws in TOPIC_KEYWORDS.items():
        for kw in kws:
            for it in yt_search(kw, "topic",
                                "🐻주제" if side == "bear" else "🔑주제"):
                it["bear"] = (side == "bear")
                collected.setdefault(it["video_id"], it)

    # dedup + 신규만
    fresh = [it for vid, it in collected.items() if vid not in seen_ids]
    fresh.sort(key=lambda x: x["published"])
    print(f"수집 {len(collected)}건 / 신규 {len(fresh)}건")

    for it in fresh:
        it["title_ko"] = translate_title(it["title"])
        send_telegram(it)
        seen_ids.add(it["video_id"])
        seen["ids"].append(it["video_id"])
        time.sleep(1)  # 텔레그램 rate limit 여유

    save_seen(seen)
    print("완료")


if __name__ == "__main__":
    main()
