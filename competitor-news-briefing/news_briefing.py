#!/usr/bin/env python3
"""
Tax Tech 경쟁사 뉴스 브리핑 봇
매일 09:00, 15:00 KST에 실행되어 Slack #tax-tech 채널로 발송
"""

from __future__ import annotations

import os
import hashlib
import feedparser
import requests
from urllib.parse import quote_plus
from datetime import datetime, timedelta, timezone
from google import genai

# ── 설정 ────────────────────────────────────────────────────────────────────

COMPETITORS = ["삼쩜삼", "혜움", "세이브텍스", "토스인컴", "자비스", "캐시노트", "머니핀", "세무통"]

SLACK_WEBHOOK_URL = os.environ["SLACK_WEBHOOK_URL"]
GEMINI_API_KEY    = os.environ["GEMINI_API_KEY"]

LOOKBACK_HOURS = 8  # 6시간 주기 + 2시간 버퍼
KST = timezone(timedelta(hours=9))

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
}

# 전용 뉴스 소스 (Google News site: 필터)
DEDICATED_SOURCES = [
    {"label": "AI Times", "site": "aitimes.com",    "query": "세금 OR 세무 OR 핀테크"},
    {"label": "세무일보", "site": "taxtimes.co.kr", "query": "세금 OR 세무"},
]

# ── 뉴스 수집 ────────────────────────────────────────────────────────────────

def cutoff_time() -> datetime:
    return datetime.now(KST) - timedelta(hours=LOOKBACK_HOURS)


def _parse_feedparser_date(entry) -> datetime | None:
    if not hasattr(entry, "published_parsed") or entry.published_parsed is None:
        return None
    return datetime(*entry.published_parsed[:6], tzinfo=timezone.utc).astimezone(KST)


def fetch_google_news(keyword: str) -> list[dict]:
    """Google News RSS — 네이버·Daum 등 주요 매체 포함"""
    url = f"https://news.google.com/rss/search?q={quote_plus(keyword)}&hl=ko&gl=KR&ceid=KR:ko"
    try:
        feed = feedparser.parse(url)
        cutoff = cutoff_time()
        results = []
        for entry in feed.entries:
            pub_date = _parse_feedparser_date(entry)
            if pub_date is None or pub_date < cutoff:
                continue
            source_title = entry.get("source", {}).get("title", "Google News")
            results.append({
                "title": entry.title,
                "link": entry.link,
                "source": source_title,
                "date": pub_date,
                "keyword": keyword,
                "content": entry.get("summary", ""),
            })
        return results
    except Exception as e:
        print(f"  [WARN] Google News [{keyword}]: {e}")
        return []


def fetch_dedicated_source(source: dict) -> list[dict]:
    """AI Times / 세무일보 — Google News site: 필터로 경쟁사 언급 기사 수집"""
    results = []
    cutoff = cutoff_time()
    for keyword in COMPETITORS:
        query = quote_plus(f"{keyword} {source['query']} site:{source['site']}")
        url = f"https://news.google.com/rss/search?q={query}&hl=ko&gl=KR&ceid=KR:ko"
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries:
                pub_date = _parse_feedparser_date(entry)
                if pub_date is None or pub_date < cutoff:
                    continue
                results.append({
                    "title": entry.title,
                    "link": entry.link,
                    "source": source["label"],
                    "date": pub_date,
                    "keyword": keyword,
                    "content": entry.get("summary", ""),
                })
        except Exception as e:
            print(f"  [WARN] {source['label']} [{keyword}]: {e}")
    return results


# ── 중복 제거 ─────────────────────────────────────────────────────────────────

def deduplicate(articles: list[dict]) -> list[dict]:
    seen: set[str] = set()
    unique = []
    for article in articles:
        key = hashlib.md5(article["title"][:40].encode("utf-8")).hexdigest()
        if key not in seen:
            seen.add(key)
            unique.append(article)
    return unique


# ── Gemini 요약 ───────────────────────────────────────────────────────────────

def summarize(article: dict, client: genai.Client) -> str:
    prompt = (
        "다음 뉴스 기사를 읽고, 경쟁사 동향·전략적 시사점 중심으로 핵심 내용을 "
        "정확히 3줄로 요약해줘. 각 줄은 반드시 '• '로 시작해야 해.\n\n"
        f"제목: {article['title']}\n"
        f"내용: {article['content']}\n\n"
        "핵심 3줄 요약:"
    )
    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
        )
        return response.text.strip()
    except Exception as e:
        print(f"  [WARN] 요약 실패: {e}")
        return "• 요약을 생성할 수 없습니다."


# ── Slack 메시지 ──────────────────────────────────────────────────────────────

def build_slack_payload(articles: list[dict]) -> dict:
    now_str = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"📰 Tax Tech 경쟁사 뉴스 브리핑  |  {now_str} KST"},
        },
        {"type": "divider"},
    ]

    for article in articles:
        date_str = article["date"].strftime("%Y-%m-%d %H:%M")
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*<{article['link']}|{article['title']}>*\n"
                    f"📌 {article['source']}  ·  📅 {date_str}  ·  🏷 #{article['keyword']}\n\n"
                    f"{article['summary']}"
                ),
            },
        })
        blocks.append({"type": "divider"})

    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": f"총 *{len(articles)}건* 수집  |  최근 {LOOKBACK_HOURS}시간 기준  |  Tax Tech Monitor Bot"}],
    })
    return {"blocks": blocks}


def post_to_slack(payload: dict) -> bool:
    resp = requests.post(
        SLACK_WEBHOOK_URL,
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=10,
    )
    if resp.status_code != 200:
        print(f"  [ERROR] Slack 응답: {resp.status_code} {resp.text}")
    return resp.status_code == 200


def post_no_news_notice() -> None:
    payload = {
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"📭 *Tax Tech 경쟁사 뉴스 브리핑*  |  {datetime.now(KST).strftime('%Y-%m-%d %H:%M')} KST\n최근 {LOOKBACK_HOURS}시간 내 수집된 뉴스가 없습니다.",
                },
            }
        ]
    }
    post_to_slack(payload)


# ── 메인 ──────────────────────────────────────────────────────────────────────

def main() -> None:
    client = genai.Client(api_key=GEMINI_API_KEY)
    print(f"[{datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')} KST] 뉴스 수집 시작")

    all_articles: list[dict] = []

    # Google News (네이버·Daum 포함)
    for competitor in COMPETITORS:
        items = fetch_google_news(competitor)
        all_articles.extend(items)
        print(f"  Google News [{competitor}]: {len(items)}건")

    # AI Times + 세무일보 (Google News site: 필터)
    for source in DEDICATED_SOURCES:
        items = fetch_dedicated_source(source)
        all_articles.extend(items)
        print(f"  {source['label']}: {len(items)}건")

    # 중복 제거 + 최신순 정렬
    articles = deduplicate(all_articles)
    articles.sort(key=lambda x: x["date"], reverse=True)
    print(f"\n총 {len(articles)}건 (중복 제거 후)")

    if not articles:
        print("수집된 뉴스 없음 — 빈 브리핑 전송")
        post_no_news_notice()
        return

    # Gemini 요약
    print("\n기사 요약 중...")
    for i, article in enumerate(articles):
        print(f"  [{i + 1}/{len(articles)}] {article['title'][:50]}...")
        article["summary"] = summarize(article, client)

    # Slack 전송
    payload = build_slack_payload(articles)
    ok = post_to_slack(payload)
    print(f"\nSlack 전송 {'✅ 성공' if ok else '❌ 실패'}")


if __name__ == "__main__":
    main()
