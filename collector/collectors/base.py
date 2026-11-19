"""수집기 공통 모듈: 게시글 데이터 모델과 공용 헬퍼."""
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta, timezone
import re

KST = timezone(timedelta(hours=9))


@dataclass
class Post:
    source: str          # 예: "디시 AI채팅 갤러리"
    site: str            # "dcinside" | "arca" | "reddit"
    post_id: str
    title: str
    url: str
    author: str = ""
    views: int | None = 0            # None = 해당 소스에서 제공하지 않음
    recommend: int | None = 0        # Reddit API에서는 순투표 점수, RSS에서는 None
    comments: int | None = 0
    has_image: bool = False
    posted_at: str = ""              # 사이트에 표시된 시간 문자열
    collected_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    # 후처리로 채워지는 필드
    keyword_hits: list = field(default_factory=list)   # 매칭된 관심 키워드
    is_meme_candidate: bool = False
    is_selected: bool = False        # 신호 선정(뜨는글)에 포함되었는지

    def to_dict(self):
        return asdict(self)


def to_int(text: str) -> int:
    """'1,234', '1.2만' 같은 표기를 정수로."""
    if text is None:
        return 0
    text = str(text).strip().replace(",", "")
    if not text:
        return 0
    m = re.search(r"([\d.]+)\s*만", text)
    if m:
        return int(float(m.group(1)) * 10000)
    m = re.search(r"\d+", text)
    return int(m.group()) if m else 0


def parse_kdate(s, now=None):
    """
    디시/아카의 다양한 날짜 표기를 datetime으로 변환. 실패 시 None.
    지원 형식:
      2026-09-01T14:23:11+09:00 (아카 <time datetime>), 2026.09.01 14:23:11,
      2026-09-01, 09.01(월-일→올해), 14:23(시:분→오늘)
    """
    if not s:
        return None
    now = now or datetime.now()
    s = str(s).strip()
    # ISO (아카 datetime 속성). UTC/오프셋 포함이면 한국시간(KST)으로 변환 후
    # naive로 저장 — 그래야 디시(원래 KST 표시)와 기준이 같아져서 기간 필터가 맞는다.
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone(KST).replace(tzinfo=None)
        return dt
    except Exception:
        pass
    t = s.replace(".", "-").replace("/", "-").strip("- ")
    # 날짜+시간
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})[ T]+(\d{1,2}):(\d{2})", t)
    if m:
        y, mo, d, h, mi = map(int, m.groups())
        try:
            return datetime(y, mo, d, h, mi)
        except ValueError:
            return None
    # 날짜만
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", t)
    if m:
        y, mo, d = map(int, m.groups())
        try:
            return datetime(y, mo, d)
        except ValueError:
            return None
    # 월-일만 (올해로 가정, 미래면 작년)
    m = re.fullmatch(r"(\d{1,2})-(\d{1,2})", t)
    if m:
        mo, d = map(int, m.groups())
        try:
            dt = datetime(now.year, mo, d)
        except ValueError:
            return None
        if dt > now + timedelta(days=1):
            dt = dt.replace(year=now.year - 1)
        return dt
    # 시:분만 (오늘)
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", t)
    if m:
        h, mi = map(int, m.groups())
        return datetime(now.year, now.month, now.day, h, mi)
    return None
