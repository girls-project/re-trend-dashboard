"""
리노벨 트렌드 레이더 — 메인 실행 파일.

매 실행마다 새 글은 누적 저장소에 추가하고, 기존 글은 지표를 갱신합니다.
(한 번에 조금씩 긁어도 매일 돌리면 데이터가 계속 쌓입니다.)

사용법:
    python main.py                # config.yaml 기준으로 수집
    python main.py --pages 5      # 페이지 수 임시 오버라이드
    python main.py --days 3       # 이번 수집의 기간(일) 임시 오버라이드

결과물:
    data/store.json       # 누적 저장소(원본, 계속 유지 — 지우지 마세요)
    data/all_latest.json  # 대시보드가 읽는 파일 (기간 선택/뜨는글은 대시보드가 계산)
    data/store.csv        # 엑셀/시트 분석용 전체 누적본
    data/latest.json      # summarize.py 용 최근 7일 뜨는글
"""
import argparse
import csv
import json
import math
import re
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from collectors import dcinside, arca, reddit

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
COLLECTORS = {"dcinside": dcinside.collect, "arca": arca.collect, "reddit": reddit.collect}


def matches_source(source, query):
    query = query.lower()
    return any(query in str(source.get(key, '')).lower()
               for key in ('name', 'type', 'board_id', 'channel', 'subreddit'))


def load_config():
    with open(Path(__file__).parent / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def tag_keywords(post, watch):
    """제목에서 관심 키워드를 찾아 라벨을 붙인다."""
    hits = []
    text = post.title.lower()
    for group, words in watch.items():
        for w in words:
            if w.lower() in text:
                hits.append({"group": group, "word": w})
    post.keyword_hits = hits
    return post


def mark_meme(post, meme_cfg):
    """짧은 제목 + 이미지 + 높은 추천 = 밈/짤 후보."""
    if (post.has_image
            and len(post.title) <= meme_cfg["max_title_len"]
            and post.recommend is not None
            and post.recommend >= meme_cfg["min_recommend"]):
        post.is_meme_candidate = True
    return post


def select_hot(posts, sel):
    """
    신호 선정 정책:
      - 각 '사이트별로' 추천 상위 N%를 뽑는다 (디시/아카를 합쳐 순위 매기지 않음).
      - 단, min_recommend_floor 미만은 상위여도 제외(조용한 날 방어).
      - force_include_groups 축에 걸린 글은 순위와 무관하게 무조건 포함.
    """
    top_percent = sel.get("recommend_top_percent", 20)
    floor = sel.get("min_recommend_floor", 0)
    force_groups = set(sel.get("force_include_groups", []))

    by_site = {}
    for p in posts:
        by_site.setdefault(p.site, []).append(p)

    chosen = {}  # id(post) -> post (중복 방지)
    for site, plist in by_site.items():
        ranked = sorted((p for p in plist if p.recommend is not None),
                        key=lambda p: p.recommend, reverse=True)
        n_top = max(1, math.ceil(len(ranked) * top_percent / 100))
        for p in ranked[:n_top]:
            if p.recommend >= floor:
                chosen[id(p)] = p

    # 전략 축(브랜드/경쟁사/수요·불만)은 순위 낮아도 무조건 포함
    for p in posts:
        if any(h["group"] in force_groups for h in p.keyword_hits):
            chosen[id(p)] = p

    return list(chosen.values())


# ---- 키워드 빈도 분석 -------------------------------------------------
_JOSA = ["으로", "에서", "에게", "한테", "이라고", "라고", "까지", "부터",
         "이나", "은", "는", "이", "가", "을", "를", "에", "의", "도", "만", "과", "와", "로"]


def _strip_josa(w):
    for j in sorted(_JOSA, key=len, reverse=True):
        if len(w) > len(j) + 1 and w.endswith(j):
            return w[:-len(j)]
    return w


def _tokenize(title):
    """제목에서 한글/영문 단어를 추출(가벼운 방식). 한글은 흔한 조사만 제거."""
    toks = re.findall(r"[가-힣]+|[A-Za-z][A-Za-z0-9]+", title or "")
    out = []
    for t in toks:
        if "가" <= t[0] <= "힣":
            t = _strip_josa(t)
        out.append(t.lower())
    return out


def word_frequencies(posts, watch, freq_cfg):
    """
    반환:
      tracked  = 관심 키워드(축별) 언급 글 수 순위
      emergent = 자동 추출 단어 순위(관심 키워드/불용어 제외)
    각 항목은 total + 사이트별(dcinside/arca) 카운트를 포함.
    """
    min_len = freq_cfg.get("min_word_len", 2)
    top_n = freq_cfg.get("top_n", 40)
    stop = set(w.lower() for w in freq_cfg.get("stopwords", []))
    tracked_set = set(w.lower() for words in watch.values() for w in words)

    # (1) 관심 키워드: 글 단위로 카운트
    tracked = {}
    for p in posts:
        for h in p.keyword_hits:
            d = tracked.setdefault((h["group"], h["word"]),
                                   {"word": h["word"], "group": h["group"],
                                    "total": 0, "dcinside": 0, "arca": 0})
            d["total"] += 1
            d[p.site] = d.get(p.site, 0) + 1
    tracked_list = sorted(tracked.values(), key=lambda x: -x["total"])

    # (2) 자동 추출 단어: 제목 토큰 빈도 (관심 키워드/불용어 제외)
    overall, per = Counter(), {"dcinside": Counter(), "arca": Counter()}
    for p in posts:
        for tok in _tokenize(p.title):
            if len(tok) < min_len or tok in stop or tok in tracked_set:
                continue
            overall[tok] += 1
            per.setdefault(p.site, Counter())[tok] += 1
    emergent = [{"word": w, "total": c,
                 **{site: counts[w] for site, counts in per.items()}}
                for w, c in overall.most_common(top_n)]
    return tracked_list, emergent


# ---- 누적 저장소(store) ------------------------------------------------
STORE_PATH = DATA_DIR / "store.json"


def load_store():
    """The versioned all_latest feed is the only durable upstream store."""
    path = DATA_DIR / "all_latest.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("count") != len(data.get("posts", [])):
        raise ValueError("Existing feed count mismatch; refusing to reset store")
    allowed = {"site","post_id","source","title","url","recommend","views","comments","has_image","posted_at","keyword_hits","is_meme_candidate","tokens","first_seen","last_seen","date"}
    if any(set(post) - allowed for post in data["posts"]):
        raise ValueError("Unexpected post fields")
    return {post["source"] + ":" + str(post["post_id"]): post for post in data["posts"]}


def save_store(entries):
    # main() writes all_latest.json after it has merged and validated the batch.
    pass


def emergent_tokens(title, watch, freq_cfg):
    """제목에서 자동 추출 단어(관심 키워드/불용어/짧은 단어 제외) 리스트."""
    min_len = freq_cfg.get("min_word_len", 2)
    stop = set(w.lower() for w in freq_cfg.get("stopwords", []))
    tracked = set(w.lower() for words in watch.values() for w in words)
    return [t for t in _tokenize(title)
            if len(t) >= min_len and t not in stop and t not in tracked]


def select_hot_dicts(entries, sel):
    """select_hot의 dict 버전: 사이트별 추천 상위 N% + 전략 축 강제 포함."""
    top_percent = sel.get("recommend_top_percent", 20)
    floor = sel.get("min_recommend_floor", 0)
    force_groups = set(sel.get("force_include_groups", []))
    by_site = {}
    for e in entries:
        by_site.setdefault(e["site"], []).append(e)
    chosen = {}
    for site, lst in by_site.items():
        ranked = sorted((e for e in lst if e.get('recommend') is not None),
                        key=lambda e: e["recommend"], reverse=True)
        n_top = max(1, math.ceil(len(ranked) * top_percent / 100))
        for e in ranked[:n_top]:
            if e["recommend"] >= floor:
                chosen[f"{e['source']}:{e['post_id']}"] = e
    for e in entries:
        if any(h["group"] in force_groups for h in e.get("keyword_hits", [])):
            chosen[f"{e['source']}:{e['post_id']}"] = e
    return sorted(chosen.values(), key=lambda e: (e.get("recommend") or 0, e.get("views") or 0), reverse=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=None)
    ap.add_argument("--days", type=int, default=None,
                    help="수집 기간(일). 미지정 시 config의 collect_days 사용.")
    ap.add_argument("--source", action="append", default=[],
                    help="지정한 소스만 갱신(이름/사이트/갤러리 ID/subreddit). 여러 번 지정 가능.")
    args = ap.parse_args()

    cfg = load_config()
    targets = [src for src in cfg['sources']
               if not args.source or any(matches_source(src, q) for q in args.source)]
    if not targets:
        ap.error('일치하는 수집 소스가 없습니다.')
    default_pages = cfg.get("default_pages", 3)
    collect_days = args.days or cfg.get("collect_days", 7)
    cutoff = datetime.now() - timedelta(days=collect_days)
    delay = cfg["request_delay_seconds"]

    all_posts, succeeded = [], 0
    try:
        source_status = json.loads((DATA_DIR / 'all_latest.json').read_text(encoding='utf-8')).get('source_status', {})
    except (OSError, ValueError):
        source_status = {}
    for src in targets:
        fn = COLLECTORS.get(src["type"])
        if not fn:
            print(f"! 알 수 없는 소스 타입: {src['type']}")
            continue
        # 페이지 수: --pages 옵션 > source별 pages > default_pages
        pages = args.pages or src.get("pages", default_pages)
        print(f"→ 수집 중: {src['name']} ({pages}페이지)")
        try:
            got = fn(src, pages, delay)
            print(f"  {len(got)}건 수집")
            all_posts.extend(got)
            succeeded += 1
            source_status[src['name']] = {'status': 'ok', 'count': len(got),
                'checked_at': datetime.now().isoformat(timespec='seconds')}
            if src['type'] == 'reddit' and any(p.recommend is None for p in got):
                source_status[src['name']]['message'] = '공개 RSS: 점수·댓글·조회수 미제공'
        except Exception as e:
            # 네트워크 예외에는 요청/인증 정보가 섞일 수 있어 예외 종류만 공개한다.
            message = str(e) if type(e) is RuntimeError else type(e).__name__
            print(f"  ✗ 실패: {message}")
            source_status[src['name']] = {'status': 'error', 'message': message,
                'checked_at': datetime.now().isoformat(timespec='seconds')}

    # 중복 제거 (source + post_id) — 갤러리마다 글 번호가 겹칠 수 있으므로 source 포함
    seen, unique = set(), []
    for p in all_posts:
        key = (p.source, p.post_id)
        if key not in seen:
            seen.add(key)
            unique.append(p)

    # 수집 배치를 collect_days 이내로 제한(오래된 페이지 무시). 날짜 미파싱분은 보존.
    cutoff = datetime.now() - timedelta(days=collect_days)
    batch, undated, dropped = [], 0, 0
    for p in unique:
        keep = True
        if p.posted_at:
            try:
                keep = datetime.fromisoformat(p.posted_at) >= cutoff
            except ValueError:
                undated += 1
        else:
            undated += 1
        if keep:
            batch.append(p)
        else:
            dropped += 1

    # 태깅 + 밈 판정
    freq_cfg = cfg.get("keyword_freq", {})
    for p in batch:
        tag_keywords(p, cfg["watch_keywords"])
        mark_meme(p, cfg["meme_signal"])

    DATA_DIR.mkdir(exist_ok=True)
    now_iso = datetime.now().isoformat(timespec="seconds")

    # ---- 누적 저장소 병합 (핵심: 매 실행마다 새 글은 추가, 기존 글은 지표 갱신) ----
    store = load_store()
    added = updated = 0
    for p in batch:
        key = f"{p.source}:{p.post_id}"
        entry = {"site": p.site, "post_id": p.post_id, "source": p.source,
                 "title": p.title, "url": p.url, "recommend": p.recommend,
                 "views": p.views, "comments": p.comments, "has_image": p.has_image,
                 "posted_at": p.posted_at, "keyword_hits": p.keyword_hits,
                 "is_meme_candidate": p.is_meme_candidate,
                 "tokens": emergent_tokens(p.title, cfg["watch_keywords"], freq_cfg)}
        if key in store:
            prev = store[key]
            entry["first_seen"] = prev.get("first_seen", now_iso)
            if not entry["posted_at"]:
                entry["posted_at"] = prev.get("posted_at", "")
            updated += 1
        else:
            entry["first_seen"] = now_iso
            added += 1
        entry["last_seen"] = now_iso
        store[key] = entry

    # 유효 날짜(date): 커뮤니티 작성시각 우선, 없으면 최초 수집시각
    for e in store.values():
        e["date"] = e.get("posted_at") or e.get("first_seen", now_iso)

    # ---- 보존 기간 지난 글 정리 ----
    retention = cfg.get("store_retention_days", 90)
    rcutoff = datetime.now() - timedelta(days=retention) if succeeded else datetime.min
    kept = {}
    for k, e in store.items():
        try:
            if datetime.fromisoformat(e["date"]) >= rcutoff:
                kept[k] = e
        except (ValueError, KeyError):
            kept[k] = e  # 날짜 불명은 보존
    store = kept
    if succeeded:
        save_store(store)

    if not store:
        print("\n저장소가 비었습니다. 셀렉터 검증(python verify.py)을 먼저 확인하세요.")
        sys.exit(0)

    # ---- 대시보드용 export (기간 선택/뜨는글 계산은 대시보드가 담당) ----
    comp_map = {}
    for canon, aliases in (cfg.get("competitor_canonical") or {}).items():
        for a in aliases:
            comp_map[a] = canon
    posts_out = sorted(store.values(), key=lambda e: (e["site"], -(e.get("recommend") or 0)))
    export = {"updated_at": now_iso if succeeded else json.loads((DATA_DIR / "all_latest.json").read_text())["updated_at"], "selection": cfg.get("selection", {}),
              "competitor_map": comp_map,
              "sources": [s["name"] for s in cfg["sources"]],
              "source_status": source_status,
              "retention_days": retention, "count": len(posts_out), "posts": posts_out}
    feed_path = DATA_DIR / "all_latest.json"
    temp_path = DATA_DIR / "all_latest.next.json"
    temp_path.write_text(json.dumps(export, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(feed_path)

    # 전체 누적본 CSV (엑셀 분석용)
    with open(DATA_DIR / "store.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["소스", "날짜", "제목", "추천", "조회", "댓글", "밈후보", "키워드", "최초수집", "URL"])
        for e in posts_out:
            kw = ",".join(h["word"] for h in e["keyword_hits"])
            w.writerow([e["source"], e["date"][:16], e["title"], e["recommend"], e["views"],
                        e["comments"], "Y" if e["is_meme_candidate"] else "", kw,
                        e["first_seen"][:16], e["url"]])

    # summarize.py 호환용: 최근 7일 뜨는글 → latest.json
    week_cutoff = datetime.now() - timedelta(days=7)
    def in7(e):
        try:
            return datetime.fromisoformat(e["date"]) >= week_cutoff
        except ValueError:
            return True
    week_hot = select_hot_dicts([e for e in store.values() if in7(e)], cfg.get("selection", {}))
    (DATA_DIR / "latest.json").write_text(json.dumps(
        {"generated_at": now_iso, "period": "최근 7일", "count": len(week_hot), "posts": week_hot},
        ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- 콘솔 요약 ----
    dc = sum(1 for e in store.values() if e["site"] == "dcinside")
    ac = sum(1 for e in store.values() if e["site"] == "arca")
    rd = sum(1 for e in store.values() if e["site"] == "reddit")
    print(f"\n✓ 완료: 이번 실행 신규 {added}건 · 갱신 {updated}건")
    if dropped:
        print(f"  수집창({collect_days}일) 밖 {dropped}건 제외")
    if undated:
        print(f"  ⚠ 날짜 미파싱분은 최초수집일 기준으로 저장됨")
    print(f"  누적 저장소: 총 {len(store)}건 (디시 {dc} / 아카 {ac} / Reddit {rd}), 보존 {retention}일")
    print(f"  → data/all_latest.json (대시보드) · data/store.csv (엑셀)")
    print(f"  dashboard.html 에서 기간(오늘/최근 3·7·30일/전체)을 골라 보세요.")


if __name__ == "__main__":
    main()
