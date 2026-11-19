"""
디시인사이드 갤러리 수집기.

목록 URL 구조 (2026년 확인):
  https://gall.dcinside.com/mgallery/board/lists/?id=aichatting&page=1
  개념글만:  ...&exception_mode=recommend

⚠️ 셀렉터 검증 필요:
  디시는 HTML 구조를 종종 바꿉니다. 처음 실행 시 반드시
  `python verify.py dcinside` 로 파싱이 맞는지 눈으로 확인하세요.
  (아래 CSS 클래스가 바뀌면 이 파일만 고치면 됩니다.)
"""
import time
import requests
from bs4 import BeautifulSoup

from .base import Post, to_int, parse_kdate

BASE = "https://gall.dcinside.com"
LIST_URL = BASE + "/mgallery/board/lists/"

HEADERS = {
    # 디시는 UA/Referer 없으면 차단합니다. 실제 브라우저 UA를 쓰세요.
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0 Safari/537.36"),
    "Referer": BASE + "/",
    "Accept-Language": "ko-KR,ko;q=0.9",
}


def collect(source_cfg: dict, pages: int, delay: float) -> list[Post]:
    board_id = source_cfg["board_id"]
    gallery_type = source_cfg.get("gallery_type", "mgallery")
    if gallery_type not in ("mgallery", "mini"):
        raise ValueError("gallery_type은 mgallery 또는 mini여야 합니다.")
    list_url = BASE + f"/{gallery_type}/board/lists/"
    mode = source_cfg.get("mode", "all")
    posts: list[Post] = []

    for page in range(1, pages + 1):
        params = {"id": board_id, "page": page}
        if mode == "recommend":
            params["exception_mode"] = "recommend"

        resp = requests.get(list_url, params=params, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        # --- 셀렉터 (검증 대상) -------------------------------------------
        rows = soup.select("table.gall_list tbody tr.ub-content")
        if not rows:
            raise RuntimeError("DC 목록을 받지 못했습니다. 빈 목록을 성공으로 처리하지 않습니다.")
        for tr in rows:
            # 공지/광고 행 스킵
            num_cell = tr.select_one("td.gall_num")
            if not num_cell or not num_cell.get_text(strip=True).isdigit():
                continue

            tit = tr.select_one("td.gall_tit a")
            if not tit:
                continue
            title = tit.get_text(strip=True)
            href = tit.get("href", "")
            url = href if href.startswith("http") else BASE + href
            post_id = num_cell.get_text(strip=True)

            views_el = tr.select_one("td.gall_count")
            rec_el = tr.select_one("td.gall_recommend")

            # 댓글 수: 제목 옆 [12] 형태
            reply_el = tr.select_one("td.gall_tit a.reply_numbox .reply_num")
            comments = to_int(reply_el.get_text()) if reply_el else 0

            # 이미지/움짤 아이콘 존재 여부
            has_image = bool(tr.select_one("td.gall_tit .icon_img, td.gall_tit em.icon_pic"))

            # 작성 시각: td.gall_date 의 title(전체 datetime) 우선, 없으면 표시 텍스트
            date_el = tr.select_one("td.gall_date")
            raw_date = ""
            if date_el:
                raw_date = date_el.get("title") or date_el.get_text(strip=True)
            dt = parse_kdate(raw_date)
            posted_at = dt.isoformat(timespec="minutes") if dt else ""
            # ------------------------------------------------------------

            posts.append(Post(
                source=source_cfg["name"],
                site="dcinside",
                post_id=post_id,
                title=title,
                url=url,
                views=to_int(views_el.get_text()) if views_el else 0,
                recommend=to_int(rec_el.get_text()) if rec_el else 0,
                comments=comments,
                has_image=has_image,
                posted_at=posted_at,
            ))

        time.sleep(delay)

    return posts
