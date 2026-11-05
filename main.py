import datetime
import html
import os
import re
import sys
import feedparser
import requests
from google import genai
from google.genai.errors import APIError

# 1. 환경 설정 (비밀 키)
TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
TELEGRAM_CHAT_ID = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
GEMINI_API_KEY = (os.getenv("GEMINI_API_KEY") or "").strip()

POSTS_DIR = "_posts"
HISTORY_FILE = "sent_history.txt"
MAX_HISTORY = 300
BLOG_BASE_URL = "https://nj-ktiger.github.io/blog"

# '노마드 & 기어' 컨셉에 맞춘 글로벌 테크 / 기어 / 디자인 RSS 피드 목록
RSS_FEEDS = [
    {"name": "Yanko Design (혁신 기어/디자인)", "url": "https://www.yankodesign.com/feed/"},
    {"name": "The Gadgeteer (노마드 가젯/기어 리뷰)", "url": "https://the-gadgeteer.com/feed/"},
    {"name": "Product Hunt (신규 테크/아이디어)", "url": "https://www.producthunt.com/feed"},
]


def clean_html(raw_html: str, max_len: int = 500) -> str:
    """RSS 요약문에서 HTML 태그와 불필요한 공백을 제거합니다."""
    if not raw_html:
        return ""
    text = re.sub(r"<[^>]+>", " ", raw_html)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_len:
        return text[:max_len] + "..."
    return text


def load_sent_history() -> set[str]:
    """이미 발송/발행한 아이템 링크 목록을 불러옵니다."""
    if not os.path.exists(HISTORY_FILE):
        return set()
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        return {
            line.strip()
            for line in f
            if line.strip() and not line.strip().startswith("#")
        }


def save_sent_history(new_link: str) -> None:
    """새로 발행한 아이템 링크를 기록 파일에 추가합니다."""
    existing = []
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            existing = [
                line.strip()
                for line in f
                if line.strip() and not line.strip().startswith("#")
            ]

    if new_link not in existing:
        existing.append(new_link)

    existing = existing[-MAX_HISTORY:]

    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        f.write("# 발송/발행 완료된 아이템 링크 기록 (중복 방지용)\n")
        for link in existing:
            f.write(f"{link}\n")


def get_candidate_items(sent_links: set[str], per_feed_limit: int = 4) -> list[dict]:
    """여러 RSS 피드에서 아직 다루지 않은 최신 후보 아이템들을 수집합니다."""
    candidates = []
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) NomadGearBot/1.0"
    }

    for feed_info in RSS_FEEDS:
        source_name = feed_info["name"]
        url = feed_info["url"]
        try:
            resp = requests.get(url, headers=headers, timeout=15)
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)
        except Exception as e:
            print(f"[경고] {source_name} 피드 수집 실패: {e}")
            continue

        count = 0
        for entry in feed.entries:
            link = getattr(entry, "link", "").strip()
            title = getattr(entry, "title", "").strip()
            if not link or not title or link in sent_links:
                continue

            raw_summary = getattr(entry, "summary", "") or getattr(
                entry, "description", ""
            )
            summary = clean_html(raw_summary)

            candidates.append(
                {
                    "source": source_name,
                    "title": title,
                    "link": link,
                    "summary": summary,
                }
            )
            count += 1
            if count >= per_feed_limit:
                break

    return candidates


def generate_with_gemini(client: genai.Client, prompt: str) -> str:
    """
    Gemini 모델 호출 (gemini-3.5-flash-lite -> gemini-3.1-flash-lite -> gemini-3.8-flash)
    서버 과부하나 쿼터 변동에도 안정적으로 글을 생성합니다.
    """
    models_to_try = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3.8-flash"]

    last_error = None
    for model_name in models_to_try:
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
            )
            if response.text and response.text.strip():
                print(f"   -> [{model_name}] 모델을 사용하여 블로그 글 생성을 완료했습니다.")
                return response.text.strip()
        except APIError as e:
            print(f"[알림] {model_name} 호출 중 에러 발생: {e}. 다른 모델 시도 중...")
            last_error = e
        except Exception as e:
            print(f"[경고] {model_name} 호출 실패: {e}")
            last_error = e

    raise RuntimeError(f"Gemini API 호출에 모두 실패했습니다: {last_error}")


def curate_and_generate_post(candidates: list[dict]) -> tuple[dict, dict]:
    """
    후보 아이템 중 베스트 1개를 선별하고 GitHub Pages(Jekyll) 블로그 포스트를 생성합니다.
    """
    client = genai.Client(api_key=GEMINI_API_KEY)

    candidates_text = "\n\n".join(
        [
            f"[{idx}] 출처: {item['source']}\n"
            f"- 제품명/제목: {item['title']}\n"
            f"- 요약/특징: {item['summary']}\n"
            f"- 링크: {item['link']}"
            for idx, item in enumerate(candidates)
        ]
    )

    prompt = f"""
당신은 테크 및 라이프스타일 트렌드 큐레이터 '노마드 & 기어'의 전문 테크 블로거입니다.

아래 [후보 아이템 목록] 중에서 한국 2030 독자(디지털 노마드, 데스크테리어족, 스마트 기어/가젯 마니아, 생산성 도구에 관심 많은 직장인/크리에이터)의 호기심을 가장 강하게 자극할 수 있는 베스트 아이템 **단 1개**를 골라주세요.
(너무 개발자 전용인 백엔드/인프라 도구는 피하고, 실생활/업무/라이프스타일/하드웨어 기어/매력적인 신제품 위주로 선정하세요.)

선정한 아이템으로 GitHub Pages 블로그에 바로 발행될 고품질 마크다운(Markdown) 블로그 글을 작성하세요.

[후보 아이템 목록]
{candidates_text}

[출력 형식 규칙 - 아래 키워드와 형식을 정확히 지켜서 출력하세요]
SELECTED_INDEX: [선택한 아이템 번호 숫자]
TITLE: [클릭을 유도하는 매력적인 한국어 블로그 제목]
SLUG: [파일 및 URL 경로에 쓰일 간결한 영문 슬러그 (예: casio-smart-keyboard, fold-desk-mat)]
SUMMARY_3LINES:
- [3줄 핵심 요약 1: 어떤 제품인가요?]
- [3줄 핵심 요약 2: 가장 혁신적이거나 차별화된 기능은?]
- [3줄 핵심 요약 3: 누구에게 추천하나요?]
---POST_CONTENT_START---
[여기에 마크다운 본문을 작성하세요]
구성은 다음과 같습니다:
## 📌 3줄 핵심 요약
(위 3줄 요약 내용 포함)

## 💡 제품 소개 및 첫인상
(제품의 배경과 디자인, 첫인상 소개)

## ✨ 핵심 매력 포인트 & 주요 기능
(기능 2~3가지를 소제목과 함께 친절하고 감각적인 말투(~해요, ~해보세요)로 상세히 소개)

## 🎯 이런 분들에게 추천해요
(어떤 사람에게 특히 유용한지 2~3가지 대상)

## ☕ 노마드 & 기어 에디터의 한마디
(에디터 관점에서의 총평 및 기대감)
---POST_CONTENT_END---
"""

    full_text = generate_with_gemini(client, prompt)

    selected_idx = 0
    title = ""
    slug = ""
    summary_3lines = ""
    post_content = ""

    idx_match = re.search(r"SELECTED_INDEX:\s*(\d+)", full_text, re.IGNORECASE)
    if idx_match:
        val = int(idx_match.group(1))
        if 0 <= val < len(candidates):
            selected_idx = val

    title_match = re.search(r"TITLE:\s*(.+)", full_text)
    if title_match:
        title = title_match.group(1).strip()

    slug_match = re.search(r"SLUG:\s*([a-zA-Z0-9\-_ ]+)", full_text)
    if slug_match:
        slug = slug_match.group(1).strip()

    summary_match = re.search(
        r"SUMMARY_3LINES:\s*([\s\S]*?)---POST_CONTENT_START---", full_text
    )
    if summary_match:
        summary_3lines = summary_match.group(1).strip()

    content_match = re.search(
        r"---POST_CONTENT_START---([\s\S]*?)---POST_CONTENT_END---", full_text
    )
    if content_match:
        post_content = content_match.group(1).strip()
    else:
        # 혹시 구분 태그가 누락된 경우 후반부를 본문으로 채택
        post_content = full_text

    if not title:
        title = candidates[selected_idx]["title"]
    if not slug:
        slug = f"gear-post-{selected_idx}"

    post_data = {
        "title": title,
        "slug": slug,
        "summary": summary_3lines,
        "content": post_content,
    }
    return candidates[selected_idx], post_data


def save_blog_post_file(selected_item: dict, post_data: dict) -> tuple[str, str]:
    """
    _posts/YYYY-MM-DD-slug.md 형식으로 Jekyll 마크다운 파일을 저장하고
    블로그 글의 전체 URL을 반환합니다.
    """
    os.makedirs(POSTS_DIR, exist_ok=True)

    # 한국 시간(KST, UTC+9) 기준 날짜 생성
    kst = datetime.timezone(datetime.timedelta(hours=9))
    now = datetime.datetime.now(kst)
    date_str = now.strftime("%Y-%m-%d")
    datetime_str = now.strftime("%Y-%m-%d %H:%M:%S +0900")

    raw_slug = re.sub(r"[^a-zA-Z0-9\-_]", "-", post_data["slug"].lower())
    clean_slug = re.sub(r"-+", "-", raw_slug).strip("-")
    if not clean_slug:
        clean_slug = f"post-{now.strftime('%H%M%S')}"

    filename = f"{date_str}-{clean_slug}.md"
    file_path = os.path.join(POSTS_DIR, filename)

    escaped_title = post_data["title"].replace('"', '\\"')

    full_markdown = f"""---
layout: post
title: "{escaped_title}"
date: {datetime_str}
categories: [기어, 테크]
tags: [노마드기어, 스마트가젯, 테크트렌드]
---

{post_data['content']}

---

* 📌 **원문 소스**: [{selected_item['source']}]({selected_item['link']})
* 큐레이션 및 발행: [노마드 & 기어]({BLOG_BASE_URL}/)
"""

    with open(file_path, "w", encoding="utf-8") as f:
        f.write(full_markdown)

    # Jekyll minima 기본 permalink: /categories/year/month/day/title.html 또는 /year/month/day/slug.html
    # 기본은 /:categories/:year/:month/:day/:title.html 이거나 /:year/:month/:day/:title.html
    post_url = f"{BLOG_BASE_URL}/{now.strftime('%Y/%m/%d')}/{clean_slug}.html"
    return file_path, post_url


def send_telegram(message: str) -> None:
    """스마트폰 텔레그램으로 전송 (Markdown 파싱 오류 시 일반 텍스트로 자동 재시도)"""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    if len(message) > 4000:
        message = message[:3950] + "\n\n...(길이 제한으로 일부 생략됨)"

    # 1차 시도: Markdown 모드
    payload_md = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
    }
    resp = requests.post(url, json=payload_md, timeout=15)

    if resp.ok:
        return

    # Markdown 문법 충돌 시 일반 텍스트로 재전송
    if resp.status_code == 400:
        print(f"[알림] Markdown 파싱 충돌 감지. 일반 텍스트 모드로 재전송합니다...")
        payload_plain = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
        }
        resp_plain = requests.post(url, json=payload_plain, timeout=15)
        if not resp_plain.ok:
            print(f"[에러] 텔레그램 전송 실패: {resp_plain.status_code} - {resp_plain.text}")
            resp_plain.raise_for_status()
        return

    print(f"[에러] 텔레그램 API 호출 실패: {resp.status_code} - {resp.text}")
    resp.raise_for_status()


def validate_env() -> None:
    """필수 환경변수가 설정되어 있는지 확인합니다."""
    missing = []
    if not GEMINI_API_KEY:
        missing.append("GEMINI_API_KEY")
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not TELEGRAM_CHAT_ID:
        missing.append("TELEGRAM_CHAT_ID")

    if missing:
        print(f"[에러] 필수 환경변수가 누락되었습니다: {', '.join(missing)}")
        sys.exit(1)


def main():
    validate_env()

    print("1. 발송/발행 이력 확인 및 최신 트렌드/기어 후보 아이템 수집 중...")
    sent_links = load_sent_history()
    candidates = get_candidate_items(sent_links)

    if not candidates:
        print("새로 수집된 아이템이 없습니다 (모두 발행 완료되었거나 피드 일시 오류).")
        return

    print(f"   -> 총 {len(candidates)}개의 신규 후보 아이템을 찾았습니다.")
    print("2. Gemini AI로 베스트 아이템 선정 및 블로그 글 작성 중...")
    selected_item, post_data = curate_and_generate_post(candidates)
    print(f"   -> 선정된 아이템: [{selected_item['source']}] {selected_item['title']}")
    print(f"   -> 블로그 제목: {post_data['title']}")

    print("3. GitHub Pages 블로그 포스트 마크다운 파일 생성 중...")
    saved_file, post_url = save_blog_post_file(selected_item, post_data)
    print(f"   -> 파일 생성 완료: {saved_file}")

    print("4. 스마트폰 텔레그램으로 발행 알림 전송 중...")
    summary_text = post_data["summary"] if post_data["summary"] else "상세 내용은 블로그 글을 확인하세요!"
    alert_msg = (
        f"🚀 [노마드 & 기어] 새 글이 블로그에 자동 발행되었습니다!\n\n"
        f"💡 제목: {post_data['title']}\n\n"
        f"📌 핵심 요약:\n{summary_text}\n\n"
        f"🌐 블로그 바로가기:\n{post_url}\n\n"
        f"🔗 원문 출처:\n{selected_item['link']}"
    )
    send_telegram(alert_msg)

    print("5. 중복 방지를 위해 발행 이력 저장 중...")
    save_sent_history(selected_item["link"])
    print(f"완료! 블로그 글({saved_file})이 생성되었고 텔레그램 알림이 발송되었습니다.")


if __name__ == "__main__":
    main()
