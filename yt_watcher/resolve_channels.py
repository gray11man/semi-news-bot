# -*- coding: utf-8 -*-
"""
채널 handle(@xxx) → channel_id(UC...) 변환 (최초 1회만 실행)
실행: YOUTUBE_API_KEY=... python resolve_channels.py
출력된 channel_id 를 watch_config.py 의 CHANNELS 에 채워넣으면 됨.
"""
import os, requests
from watch_config import CHANNELS

KEY = os.environ["YOUTUBE_API_KEY"]

def resolve(handle):
    # handle 검색 → 채널 id
    r = requests.get(
        "https://www.googleapis.com/youtube/v3/search",
        params={"key": KEY, "part": "snippet", "type": "channel",
                "q": handle, "maxResults": 1}, timeout=20)
    r.raise_for_status()
    items = r.json().get("items", [])
    if items:
        return items[0]["snippet"]["channelId"], items[0]["snippet"]["title"]
    return None, None

for ch in CHANNELS:
    if ch.get("channel_id"):
        continue
    cid, title = resolve(ch.get("handle", ch["name"]))
    print(f'{{"name": "{ch["name"]}", "channel_id": "{cid}"}},   # {title}')
