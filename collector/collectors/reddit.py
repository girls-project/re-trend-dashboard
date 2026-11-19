"""Reddit 공개 Atom 피드 / 승인된 OAuth API 수집.

기본 RSS는 제목·원문 링크·작성시각만 보장한다. 제공되지 않는 점수,
조회수, 댓글 수는 None으로 두며 작성자와 본문은 저장하지 않는다.
REDDIT_ACCESS_TOKEN이 있으면 공식 OAuth API로 점수·댓글 수도 가져온다.
접근 거절 시 호스트 변경이나 우회 재시도를 하지 않는다.
"""
import os
from pathlib import Path
import re
import time
from urllib.parse import urlparse
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup

from .base import KST, Post, parse_kdate
from datetime import datetime, timezone

ATOM = '{http://www.w3.org/2005/Atom}'
MEDIA = '{http://search.yahoo.com/mrss/}'
USER_AGENT = 'ReNovelTrendRadar/1.0 (+https://girls-project.github.io/re-trend-dashboard/)'


def _permalink(url, subreddit):
    parsed = urlparse(url)
    if (parsed.scheme != 'https' or parsed.hostname not in ('www.reddit.com', 'reddit.com')
            or parsed.username or parsed.password
            or not parsed.path.lower().startswith('/r/' + subreddit.lower() + '/comments/')):
        return None
    return 'https://www.reddit.com' + parsed.path


def parse_feed(text, source):
    root = ET.fromstring(text)
    if root.tag != ATOM + 'feed':
        raise ValueError('Reddit의 정상 Atom 피드가 아닙니다.')
    posts = []
    for entry in root.findall(ATOM + 'entry'):
        post_id = entry.findtext(ATOM + 'id', '')
        title = entry.findtext(ATOM + 'title', '').strip()
        url = None
        for link in entry.findall(ATOM + 'link'):
            if link.get('rel', 'alternate') == 'alternate':
                url = _permalink(link.get('href', ''), source['subreddit'])
                if url:
                    break
        if not re.fullmatch(r't3_[a-z0-9]+', post_id) or not title or not url:
            continue
        dt = parse_kdate(entry.findtext(ATOM + 'published') or entry.findtext(ATOM + 'updated'))
        content = BeautifulSoup(entry.findtext(ATOM + 'content', ''), 'html.parser')
        posts.append(Post(source=source['name'], site='reddit', post_id=post_id[3:],
                          title=title, url=url, views=None, recommend=None, comments=None,
                          has_image=entry.find(MEDIA + 'thumbnail') is not None or bool(content.find('img')),
                          posted_at=dt.isoformat(timespec='minutes') if dt else ''))
    return posts


def parse_listing(payload, source):
    data = payload.get('data')
    if not isinstance(data, dict) or not isinstance(data.get('children'), list):
        raise ValueError('Reddit API 글 목록 형식이 아닙니다.')
    posts = []
    for child in data['children']:
        if child.get('kind') != 't3':
            continue
        item = child.get('data', {})
        post_id, title = item.get('id', ''), item.get('title', '').strip()
        if str(item.get('subreddit', '')).lower() != source['subreddit'].lower():
            continue
        url = _permalink('https://www.reddit.com' + item.get('permalink', ''), source['subreddit'])
        if not re.fullmatch(r'[a-z0-9]+', post_id) or not title or not url:
            continue
        created = item.get('created_utc')
        dt = (datetime.fromtimestamp(created, timezone.utc).astimezone(KST).replace(tzinfo=None)
              if isinstance(created, (int, float)) else None)
        def metric(name):
            value = item.get(name)
            return value if type(value) is int else None
        posts.append(Post(source=source['name'], site='reddit', post_id=post_id,
                          title=title, url=url, views=None, recommend=metric('score'),
                          comments=metric('num_comments'),
                          has_image=bool(item.get('is_gallery') or item.get('post_hint') == 'image'
                                         or item.get('preview')),
                          posted_at=dt.isoformat(timespec='minutes') if dt else ''))
    return posts, data.get('after')


def collect(source_cfg, pages, delay):
    subreddit = source_cfg['subreddit']
    if not re.fullmatch(r'[A-Za-z0-9_]+', subreddit):
        raise ValueError('올바르지 않은 subreddit 이름입니다.')
    mode = source_cfg.get('mode', 'new')
    if mode not in ('new', 'hot', 'top'):
        raise ValueError('Reddit mode는 new / hot / top 중 하나여야 합니다.')
    token = os.environ.get('REDDIT_ACCESS_TOKEN', '').strip()
    use_api = bool(token)
    headers = {'User-Agent': USER_AGENT, 'Accept': 'application/json' if use_api else 'application/atom+xml'}
    if use_api:
        headers['Authorization'] = 'Bearer ' + token
    url = (f'https://oauth.reddit.com/r/{subreddit}/{mode}' if use_api
           else f'https://www.reddit.com/r/{subreddit}/{mode}.rss')
    ca = Path(__file__).resolve().parents[1] / 'renovel-ca.txt'
    posts, seen, after = [], set(), None
    for page in range(max(0, pages)):
        if page:
            time.sleep(delay)
        params = {'limit': 25}
        if mode == 'top':
            params['t'] = source_cfg.get('time_filter', 'week')
        if after:
            params['after'] = after
        response = requests.get(url, params=params, headers=headers, timeout=25,
                                verify=str(ca) if ca.exists() else True,
                                allow_redirects=False)
        if response.status_code in (401, 403):
            raise RuntimeError(f'Reddit 접근 거절(HTTP {response.status_code}). 승인된 API 연결 또는 접근 환경 확인이 필요합니다.')
        if response.status_code == 429:
            raise RuntimeError('Reddit 요청 제한(HTTP 429). 다음 수집 때 다시 확인합니다.')
        if response.status_code != 200:
            raise RuntimeError(f'Reddit 수집 실패(HTTP {response.status_code}).')
        try:
            if use_api:
                batch, next_after = parse_listing(response.json(), source_cfg)
            else:
                batch = parse_feed(response.text, source_cfg)
                next_after = 't3_' + batch[-1].post_id if batch else None
        except (ValueError, ET.ParseError):
            raise RuntimeError('Reddit이 정상 글 목록을 반환하지 않았습니다. 접근 제한 여부를 확인해주세요.') from None
        fresh = [post for post in batch if post.post_id not in seen]
        posts.extend(fresh)
        seen.update(post.post_id for post in fresh)
        if not fresh or not next_after or next_after == after:
            break
        after = next_after
    return posts
