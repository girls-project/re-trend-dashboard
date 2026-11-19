"""
아카라이브 채널 수집기.

목록 URL 구조 (2026년 확인):
  https://arca.live/b/characterai?p=1
  개념글(베스트):  https://arca.live/b/characterai?mode=best&p=1

일반 HTTP 요청만 사용합니다. 접근 제한을 우회하지 않습니다.

⚠️ 셀렉터 검증 필요: `.vrow` 등 클래스가 바뀌면 이 파일만 고치면 됩니다.
"""
import time
from bs4 import BeautifulSoup

import requests as _scraper

from .base import Post, to_int, parse_kdate

BASE = "https://arca.live"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0 Safari/537.36"),
    "Accept-Language": "ko-KR,ko;q=0.9",
}


def _get(url, params):
    # cloudscraper 와 requests 모두 .get(url, params=, headers=) 시그니처 호환
    return _scraper.get(url, params=params, headers=HEADERS, timeout=20)


def collect(source_cfg: dict, pages: int, delay: float) -> list[Post]:
    channel = source_cfg["channel"]
    mode = source_cfg.get("mode", "all")
    list_url = f"{BASE}/b/{channel}"
    posts: list[Post] = []

    for page in range(1, pages + 1):
        params = {"p": page}
        if mode == "best":
            params["mode"] = "best"

        resp = _get(list_url, params)
        if resp.status_code != 200:
            raise RuntimeError(f"아카라이브 HTTP {resp.status_code}. 기존 글을 유지합니다.")
        soup = BeautifulSoup(resp.text, "lxml")

        # --- 셀렉터 (검증 대상) -------------------------------------------
        rows = soup.select("a.vrow.column, div.vrow")  # 목록 행
        for row in rows:
            if "notice" in (row.get("class") or []):
                continue  # 공지 제외

            title_el = row.select_one(".title, .vcol.col-title a, a.title")
            if not title_el:
                continue
            title = title_el.get_text(strip=True)

            href = row.get("href") or (title_el.get("href") if title_el.name == "a" else "")
            if not href:
                continue
            url = href if href.startswith("http") else BASE + href
            post_id = url.rstrip("/").split("/")[-1].split("?")[0]

            views_el = row.select_one(".col-view")
            rec_el = row.select_one(".col-rate")
            reply_el = row.select_one(".comment-count")

            has_image = bool(row.select_one(".ion-image, .ion-android-image, .badge"))

            # 작성 시각: <time datetime="..."> 속성 우선, 없으면 텍스트
            # (주의: ".col-time"은 <time> 태그를 감싸는 바깥 span이라 datetime 속성이
            #  없다 — "time"을 먼저 찾아야 실제 <time datetime="..."> 태그가 잡힌다.)
            time_el = row.select_one("time") or row.select_one(".col-time")
            raw_date = ""
            if time_el:
                raw_date = time_el.get("datetime") or time_el.get("title") or time_el.get_text(strip=True)
            dt = parse_kdate(raw_date)
            posted_at = dt.isoformat(timespec="minutes") if dt else ""
            # ------------------------------------------------------------

            posts.append(Post(
                source=source_cfg["name"],
                site="arca",
                post_id=post_id,
                title=title,
                url=url,
                views=to_int(views_el.get_text()) if views_el else 0,
                recommend=to_int(rec_el.get_text()) if rec_el else 0,
                comments=to_int(reply_el.get_text()) if reply_el else 0,
                has_image=has_image,
                posted_at=posted_at,
            ))

        time.sleep(delay)

    if not posts:
        raise RuntimeError("아카라이브가 정상 글 목록을 반환하지 않았습니다.")
    return posts
