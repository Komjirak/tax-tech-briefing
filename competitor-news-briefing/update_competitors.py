#!/usr/bin/env python3
"""
경쟁사 목록 자동 업데이트
#tax-tech 채널에서 아래 형식의 메시지를 감지해 competitors.json을 갱신한다.

  추가: 회사명        → 경쟁사 추가
  삭제: 회사명        → 경쟁사 제거
"""
from __future__ import annotations

import os
import json
import base64
import requests

SLACK_BOT_TOKEN  = os.environ["SLACK_BOT_TOKEN"]
GITHUB_TOKEN     = os.environ["GITHUB_TOKEN"]
SLACK_WEBHOOK_URL = os.environ["SLACK_WEBHOOK_URL"]

REPO      = "Komjirak/tax-tech-briefing"
FILE_PATH = "competitor-news-briefing/competitors.json"
CHANNEL   = "tax-tech"

# ── Slack ────────────────────────────────────────────────────────────────────

def _slack_headers() -> dict:
    return {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}


def get_channel_id() -> str:
    cursor = None
    while True:
        params = {"types": "public_channel,private_channel", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        resp = requests.get(
            "https://slack.com/api/conversations.list",
            headers=_slack_headers(),
            params=params,
            timeout=10,
        ).json()
        if not resp.get("ok"):
            raise RuntimeError(f"conversations.list 실패: {resp.get('error')}")
        for ch in resp.get("channels", []):
            if ch["name"] == CHANNEL:
                return ch["id"]
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    raise ValueError(f"채널 #{CHANNEL} 을 찾을 수 없습니다. 봇이 채널에 초대됐는지 확인하세요.")


def get_messages(channel_id: str, oldest: str) -> list[dict]:
    resp = requests.get(
        "https://slack.com/api/conversations.history",
        headers=_slack_headers(),
        params={"channel": channel_id, "oldest": oldest, "limit": 200},
        timeout=10,
    ).json()
    if not resp.get("ok"):
        raise RuntimeError(f"conversations.history 실패: {resp.get('error')}")
    return resp.get("messages", [])


def post_to_channel(channel_id: str, text: str) -> None:
    requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={**_slack_headers(), "Content-Type": "application/json"},
        json={"channel": channel_id, "text": text},
        timeout=10,
    )


# ── GitHub ───────────────────────────────────────────────────────────────────

def _gh_headers() -> dict:
    return {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}


def load_competitors() -> tuple[dict, str]:
    resp = requests.get(
        f"https://api.github.com/repos/{REPO}/contents/{FILE_PATH}",
        headers=_gh_headers(),
        timeout=10,
    ).json()
    content = json.loads(base64.b64decode(resp["content"]).decode())
    return content, resp["sha"]


def save_competitors(data: dict, sha: str) -> None:
    encoded = base64.b64encode(
        json.dumps(data, ensure_ascii=False, indent=2).encode()
    ).decode()
    now_names = ", ".join(data["competitors"])
    requests.put(
        f"https://api.github.com/repos/{REPO}/contents/{FILE_PATH}",
        headers=_gh_headers(),
        json={
            "message": f"feat: 경쟁사 목록 업데이트 — {now_names}",
            "content": encoded,
            "sha": sha,
        },
        timeout=10,
    )


# ── 메인 ─────────────────────────────────────────────────────────────────────

def main() -> None:
    data, sha = load_competitors()
    competitors: list[str] = data["competitors"]
    last_ts: str = data.get("last_checked_ts", "0")

    channel_id = get_channel_id()
    messages = get_messages(channel_id, last_ts)

    added: list[str] = []
    removed: list[str] = []
    latest_ts = last_ts

    for msg in reversed(messages):  # 오래된 순으로 처리
        ts = msg.get("ts", "0")
        if ts <= last_ts:
            continue
        if ts > latest_ts:
            latest_ts = ts

        text = msg.get("text", "").strip()

        if text.startswith("추가:"):
            company = text[3:].strip()
            if company and company not in competitors:
                competitors.append(company)
                added.append(company)
                print(f"  추가: {company}")

        elif text.startswith("삭제:"):
            company = text[3:].strip()
            if company in competitors:
                competitors.remove(company)
                removed.append(company)
                print(f"  삭제: {company}")

    data["competitors"] = competitors
    data["last_checked_ts"] = latest_ts
    save_competitors(data, sha)

    if added or removed:
        lines = []
        if added:
            lines.append(f"✅ 추가됨: {', '.join(added)}")
        if removed:
            lines.append(f"🗑 삭제됨: {', '.join(removed)}")
        lines.append(f"📋 현재 모니터링 ({len(competitors)}개): {', '.join(competitors)}")
        post_to_channel(channel_id, "\n".join(lines))
        print("Slack 확인 메시지 전송 완료")
    else:
        print("변경 없음")


if __name__ == "__main__":
    main()
