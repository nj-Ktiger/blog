import argparse
import datetime
import html
import json
import os
import re
import sys
import time
import feedparser
import requests
from bs4 import BeautifulSoup
from google import genai
from google.genai.errors import APIError

# 1. 환경 설정 (비밀 키)
TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
TELEGRAM_CHAT_ID = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
GEMINI_API_KEY = (os.getenv("GEMINI_API_KEY") or "").strip()

POSTS_DIR = "_posts"
HISTORY_FILE = "sent_history.txt"
PENDING_FILE = "pending_candidates.json"
MAX_HISTORY = 300
BLOG_BASE_URL = "https://nj-ktiger.github.io/blog"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
}

# 원문 기사 전문과 고화질 제품 이미지가 제공되는 글로벌 기어/가젯 전문 피드
RSS_FEEDS = [
    {"name": "Yanko Design", "url": "https://www.yankodesign.com/feed/"},
    {"name": "The Gadgeteer", "url": "https://the-gadgeteer.com/feed/"},
]


def clean_html(raw_html: str) -> str:
    """HTML 태그와 불필요한 공백을 제거합니다."""
    if not raw_html:
        return ""
    text = re.sub(r"<[^>]+>", " ", raw_html)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def load_sent_history() -> set[str]:
    """이미 발행한 아이템 링크 목록을 불러옵니다."""
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


def is_listicle_title(title: str) -> bool:
    """'5 Best...', '10 Great...' 같은 단순 모음집 기사는 제외하고 단일 제품 리뷰/소개를 우선합니다."""
    pattern = r"^\s*(?:the\s+)?\d+\s+(?:best|great|cool|top|awesome)\b"
    return bool(re.search(pattern, title, re.IGNORECASE))


def scrape_article_details(url: str, rss_entry) -> tuple[str, str]:
    """
    원문 URL에 직접 접속하여:
    1) 실제 기사 본문 전체 텍스트 (할루시네이션 방지용 팩트 데이터)
    2) 대표 제품 이미지 URL (og:image)
    을 추출합니다.
    """
    paragraphs = []
    image_url = ""

    # 1. RSS 자체에 content:encoded 전문이 있는 경우 확보
    rss_content_list = getattr(rss_entry, "content", None)
    if rss_content_list and isinstance(rss_content_list, list):
        raw_val = rss_content_list[0].get("value", "")
        if raw_val:
            soup_rss = BeautifulSoup(raw_val, "html.parser")
            for p in soup_rss.find_all("p"):
                txt = p.get_text(" ", strip=True)
                if len(txt) > 40 and "Yanko Design" not in txt:
                    paragraphs.append(txt)

    # 2. 실제 웹페이지 접속하여 og:image 및 본문 추출
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.content, "html.parser")

        og_img = soup.find("meta", property="og:image")
        if og_img and og_img.get("content"):
            image_url = og_img["content"].strip()

        if not paragraphs:
            article_tag = (
                soup.find("div", class_=re.compile(r"entry-content|post-content|article-content"))
                or soup.find("article")
                or soup.find("main")
            )
            target = article_tag if article_tag else soup.body
            if target:
                for p in target.find_all("p"):
                    txt = p.get_text(" ", strip=True)
                    if len(txt) > 45:
                        paragraphs.append(txt)
    except Exception as e:
        print(f"[경고] 원문 페이지 상세 스크래핑 실패 ({url}): {e}")

    # 3. 만약 본문이 부족하면 RSS summary라도 추가
    if not paragraphs:
        raw_summary = getattr(rss_entry, "summary", "") or getattr(rss_entry, "description", "")
        cleaned = clean_html(raw_summary)
        if cleaned:
            paragraphs.append(cleaned)

    full_text = "\n\n".join(paragraphs)
    # 토큰 절약 및 핵심 집중을 위해 최대 4500자까지 유지
    if len(full_text) > 4500:
        full_text = full_text[:4500]

    return full_text, image_url


def collect_top_3_candidates(sent_links: set[str]) -> list[dict]:
    """
    각 피드에서 실제 본문이 충실한 단일 기어/가젯 아이템을 수집하여
    최종 후보 3개를 엄선합니다.
    """
    per_feed_items = []

    for feed_info in RSS_FEEDS:
        source_name = feed_info["name"]
        url = feed_info["url"]
        feed_items = []
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)
        except Exception as e:
            print(f"[경고] {source_name} 피드 수집 실패: {e}")
            continue

        for entry in feed.entries:
            link = getattr(entry, "link", "").strip()
            title = getattr(entry, "title", "").strip()
            if not link or not title or link in sent_links:
                continue

            if is_listicle_title(title):
                continue

            full_text, image_url = scrape_article_details(link, entry)
            # 본문이 최소 300자 이상 확인된 실제 기사만 후보로 채택 (할루시네이션 원천 차단)
            if len(full_text) < 300:
                continue

            feed_items.append(
                {
                    "source": source_name,
                    "title": title,
                    "link": link,
                    "image_url": image_url,
                    "full_text": full_text,
                }
            )
            if len(feed_items) >= 2:
                break

        per_feed_items.append(feed_items)

    # 피드별로 골고루 섞어서 3개 선택
    selected = []
    for i in range(2):
        for feed_list in per_feed_items:
            if i < len(feed_list) and len(selected) < 3:
                selected.append(feed_list[i])

    return selected


def generate_with_gemini(client: genai.Client, prompt: str) -> str:
    """
    Gemini 모델 호출 (gemini-3.5-flash-lite -> gemini-3.1-flash-lite -> gemini-3.8-flash)
    """
    models_to_try = ["gemini-3.1-flash-lite", "gemini-3.5-flash-lite", "gemini-3.8-flash"]

    last_error = None
    for model_name in models_to_try:
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
            )
            if response.text and response.text.strip():
                return response.text.strip()
        except APIError as e:
            print(f"[알림] {model_name} 호출 중 에러: {e}. 다음 모델 시도...")
            last_error = e
        except Exception as e:
            print(f"[경고] {model_name} 호출 실패: {e}")
            last_error = e

    raise RuntimeError(f"Gemini API 호출에 모두 실패했습니다: {last_error}")


def enrich_candidates_with_korean_summary(candidates: list[dict]) -> list[dict]:
    """
    각 후보 아이템의 실제 원문(full_text)만을 기반으로
    한국어 제목과 2줄 핵심 요약을 각각 정확하게 생성합니다.
    (개별 처리 또는 명확한 1:1 매핑으로 엉뚱한 내용 섞임 방지)
    """
    client = genai.Client(api_key=GEMINI_API_KEY)

    for idx, item in enumerate(candidates, start=1):
        prompt = f"""
당신은 테크/기어 전문 에디터입니다.
아래 제공된 [원문 기사]의 실제 내용만을 바탕으로 한국 독자를 위한 매력적인 한국어 제목과 핵심 요약 2줄을 작성하세요.
절대 원문에 없는 내용이나 다른 제품을 지어내지 마세요.

[원문 제목]
{item['title']}

[원문 기사 발췌]
{item['full_text'][:2000]}

[출력 형식 - 아래 형식으로만 출력하세요]
KO_TITLE: [한국어로 번역/다듬은 매력적인 제품 제목]
KO_SUMMARY: [어떤 제품이고 어떤 핵심 특징/스펙이 있는지 2문장으로 명확히 요약]
"""
        res_text = generate_with_gemini(client, prompt)
        ko_title = item["title"]
        ko_summary = item["full_text"][:150] + "..."

        t_match = re.search(r"KO_TITLE:\s*(.+)", res_text)
        if t_match:
            ko_title = t_match.group(1).strip()

        s_match = re.search(r"KO_SUMMARY:\s*([\s\S]+)", res_text)
        if s_match:
            ko_summary = re.sub(r"\s+", " ", s_match.group(1)).strip()

        item["index"] = idx
        item["ko_title"] = ko_title
        item["ko_summary"] = ko_summary

    return candidates


def generate_blog_post_from_candidate(item: dict) -> dict:
    """
    사용자가 선택한 단 1개의 후보 아이템의 실제 원문(full_text)만을 전달하여
    할루시네이션 없이 100% 팩트 기반의 블로그 포스트를 작성합니다.
    """
    client = genai.Client(api_key=GEMINI_API_KEY)

    prompt = f"""
당신은 테크 및 라이프스타일 트렌드 큐레이터 '노마드 & 기어'의 전문 블로거입니다.
아래 [원문 기사 전문]에 소개된 단 하나의 제품에 대해서만 한국어 블로그 글을 작성하세요.

[절대 준수 규칙 - 할루시네이션 금지]
1. 반드시 아래 [원문 기사 전문]에 등장하는 실제 제품({item['title']})과 그 제품의 실제 기능, 디자인, 스펙, 가격/특징만 서술하세요.
2. 원문에 언급되지 않은 다른 브랜드나 가상의 스펙을 절대 상상해서 지어내지 마세요.
3. 친절하고 감각적인 한국어 해요체(~해요, ~입니다, ~해보세요)로 자연스럽게 작성하세요.

[원문 정보]
- 출처: {item['source']}
- 원문 제목: {item['title']}
- 한국어 가제: {item.get('ko_title', item['title'])}
- 원문 링크: {item['link']}

[원문 기사 전문]
{item['full_text']}

[출력 형식 규칙 - 아래 구분자를 정확히 지켜서 출력하세요]
TITLE: [클릭하고 싶게 매력적인 한국어 블로그 제목 (제품명 포함)]
SLUG: [URL에 들어갈 영문 소문자 슬러그 (예: casio-jam-keyboard, nextool-w20-pro)]
---POST_CONTENT_START---
## 📌 3줄 핵심 요약
- **어떤 제품인가요?**: (원문 기반 핵심 정체성)
- **핵심 특징 & 스펙**: (원문에 나온 주요 기능과 스펙)
- **추천 대상**: (이 기어가 특히 유용할 사용자층)

## 💡 제품 소개 및 디자인 특징
(원문에 묘사된 제품의 등장 배경, 외관 디자인, 크기/소재 등 실제 특징을 생생하게 소개)

## ✨ 주요 기능 및 매력 포인트
(원문에 설명된 구체적인 작동 방식과 장점을 소제목 2~3개로 나누어 자세히 설명)

## 🎯 아쉬운 점 또는 체크 포인트 & 추천 대상
(원문에 언급된 한계점이나 가격, 확인해야 할 사항과 추천 대상을 솔직하게 정리)

## ☕ 노마드 & 기어 에디터의 한마디
(이 제품이 주는 라이프스타일/기어로서의 가치 총평)
---POST_CONTENT_END---
"""

    full_text = generate_with_gemini(client, prompt)

    title = item.get("ko_title") or item["title"]
    slug = ""
    post_content = ""

    title_match = re.search(r"TITLE:\s*(.+)", full_text)
    if title_match:
        title = title_match.group(1).strip().strip('"').strip("'")

    slug_match = re.search(r"SLUG:\s*([a-zA-Z0-9\-_ ]+)", full_text)
    if slug_match:
        slug = slug_match.group(1).strip()

    content_match = re.search(
        r"---POST_CONTENT_START---([\s\S]*?)---POST_CONTENT_END---", full_text
    )
    if content_match:
        post_content = content_match.group(1).strip()
    else:
        post_content = re.sub(r"^(TITLE|SLUG):.*$", "", full_text, flags=re.MULTILINE).strip()

    if not slug:
        slug = re.sub(r"[^a-zA-Z0-9]+", "-", item["title"].lower()).strip("-")[:45]

    return {
        "title": title,
        "slug": slug,
        "content": post_content,
    }


def save_blog_post_file(item: dict, post_data: dict) -> tuple[str, str]:
    """
    _posts/YYYY-MM-DD-slug.md 파일에 대표 이미지(Chirpy image 규격)와 원문 링크를 정확히 결합하여 저장합니다.
    """
    os.makedirs(POSTS_DIR, exist_ok=True)

    kst = datetime.timezone(datetime.timedelta(hours=9))
    now = datetime.datetime.now(kst)
    date_str = now.strftime("%Y-%m-%d")
    datetime_str = now.strftime("%Y-%m-%d %H:%M:%S +0900")

    raw_slug = re.sub(r"[^a-zA-Z0-9\-_]", "-", post_data["slug"].lower())
    clean_slug = re.sub(r"-+", "-", raw_slug).strip("-")[:50].strip("-")
    if not clean_slug:
        clean_slug = f"gear-{now.strftime('%H%M%S')}"

    filename = f"{date_str}-{clean_slug}.md"
    file_path = os.path.join(POSTS_DIR, filename)

    escaped_title = post_data["title"].replace('"', '\\"')

    image_yaml = ""
    if item.get("image_url"):
        image_yaml = f"""image:
  path: {item['image_url']}
  alt: "{escaped_title}"
"""

    full_markdown = f"""---
title: "{escaped_title}"
date: {datetime_str}
categories: [기어, 테크]
tags: [노마드기어, 스마트가젯, {item['source'].replace(' ', '')}]
{image_yaml}---

{post_data['content']}

---

### 🔗 원문 및 출처 안내
* **출처 매체**: {item['source']}
* **원문 제목**: {item['title']}
* **원문 기사 보러가기**: [{item['link']}]({item['link']})
"""

    with open(file_path, "w", encoding="utf-8") as f:
        f.write(full_markdown)

    # Chirpy의 permalink 기본값: /posts/:title/
    post_url = f"{BLOG_BASE_URL}/posts/{clean_slug}/"
    return file_path, post_url


def send_telegram_message(text: str, reply_markup: dict | None = None) -> dict:
    """텔레그램 메시지를 전송합니다 (인라인 버튼 지원)."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    if len(text) > 4000:
        text = text[:3950] + "\n...(생략)"

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    resp = requests.post(url, json=payload, timeout=15)
    if not resp.ok:
        print(f"[에러] 텔레그램 전송 실패: {resp.status_code} - {resp.text}")
        resp.raise_for_status()
    return resp.json()


def get_latest_update_id() -> int:
    """현재 텔레그램 큐의 마지막 update_id를 확인합니다."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    try:
        resp = requests.get(url, params={"timeout": 1}, timeout=10)
        if resp.ok:
            results = resp.json().get("result", [])
            if results:
                return results[-1]["update_id"]
    except Exception:
        pass
    return 0


def check_user_selection(min_update_id: int, created_at_ts: float) -> tuple[int | None, int | None]:
    """
    텔레그램 getUpdates를 확인하여 사용자가 선택한 후보 번호(1, 2, 3)가 있는지 반환합니다.
    인라인 버튼 클릭(callback_query)과 일반 채팅 답장('1', '2', '3')을 모두 지원합니다.
    반환값: (선택된 번호 1~3 또는 None, 처리된 update_id)
    """
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {"offset": min_update_id + 1, "timeout": 2}
    try:
        resp = requests.get(url, params=params, timeout=10)
        if not resp.ok:
            return None, None
        updates = resp.json().get("result", [])
    except Exception as e:
        print(f"[경고] 텔레그램 응답 확인 중 오류: {e}")
        return None, None

    for upd in updates:
        upd_id = upd["update_id"]

        # 1) 인라인 버튼 클릭 확인 (callback_query)
        cb = upd.get("callback_query")
        if cb:
            from_id = str(cb.get("from", {}).get("id", ""))
            data = cb.get("data", "")
            if from_id == TELEGRAM_CHAT_ID and data.startswith("publish:"):
                choice_str = data.split(":")[1]
                if choice_str in ("1", "2", "3"):
                    # 버튼 클릭 응답 토스트 전송
                    ack_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery"
                    requests.post(
                        ack_url,
                        json={
                            "callback_query_id": cb["id"],
                            "text": f"✅ {choice_str}번 후보를 선택하셨습니다! 블로그 글을 작성합니다.",
                        },
                        timeout=5,
                    )
                    # 기존 업데이트 큐 소비 처리
                    requests.get(url, params={"offset": upd_id + 1}, timeout=5)
                    return int(choice_str), upd_id

        # 2) 일반 텍스트 메시지 답장 확인 ('1', '2', '3', '1번', '2번', '3번')
        msg = upd.get("message")
        if msg:
            chat_id = str(msg.get("chat", {}).get("id", ""))
            msg_date = msg.get("date", 0)
            text = (msg.get("text") or "").strip()
            if chat_id == TELEGRAM_CHAT_ID and msg_date >= created_at_ts - 10:
                m = re.match(r"^([123])\s*(?:번|번\s*발행|번\s*선택)?$", text)
                if m:
                    choice = int(m.group(1))
                    requests.get(url, params={"offset": upd_id + 1}, timeout=5)
                    return choice, upd_id

    return None, None


def execute_publish(choice: int, pending_data: dict) -> bool:
    """선택된 후보 번호(1~3)의 글을 생성하여 블로그에 저장하고 텔레그램으로 완료 알림을 보냅니다."""
    candidates = pending_data.get("candidates", [])
    idx = choice - 1
    if not (0 <= idx < len(candidates)):
        print(f"[에러] 잘못된 후보 번호입니다: {choice}")
        return False

    selected_item = candidates[idx]
    print(f"👉 [{choice}번 후보 선택됨] {selected_item['title']}")

    send_telegram_message(
        f"⏳ [{choice}번 후보 승인 완료]\n"
        f"📦 '{selected_item.get('ko_title', selected_item['title'])}'\n\n"
        f"원문 기사 전문을 바탕으로 블로그 글을 작성하여 발행 중입니다... (약 20~30초 소요)"
    )

    post_data = generate_blog_post_from_candidate(selected_item)
    saved_file, post_url = save_blog_post_file(selected_item, post_data)

    # 이력 및 상태 업데이트
    save_sent_history(selected_item["link"])
    pending_data["status"] = "published"
    pending_data["selected_choice"] = choice
    pending_data["published_at"] = time.time()
    pending_data["published_file"] = saved_file
    pending_data["published_url"] = post_url

    with open(PENDING_FILE, "w", encoding="utf-8") as f:
        json.dump(pending_data, f, ensure_ascii=False, indent=2)

    send_telegram_message(
        f"🎉 [노마드 & 기어] 블로그 발행 완료!\n\n"
        f"💡 제목: {post_data['title']}\n"
        f"📌 출처: {selected_item['source']}\n\n"
        f"🌐 내 블로그 글 바로가기 (GitHub Pages 반영까지 약 1분 소요):\n{post_url}\n\n"
        f"🔗 실제 원문 링크:\n{selected_item['link']}"
    )
    print(f"✅ 발행 완료: {saved_file} -> {post_url}")
    return True


def mode_propose(wait_seconds: int = 120) -> None:
    """
    1단계: 후보 3개를 엄선하여 텔레그램으로 인라인 버튼과 함께 전송하고,
    사용자가 즉시 선택하면 바로 발행까지 처리합니다.
    """
    print("1. 이미 발행된 이력 확인 및 신규 기어/가젯 원문 수집 중...")
    sent_links = load_sent_history()
    candidates = collect_top_3_candidates(sent_links)

    if not candidates:
        print("새로 수집된 아이템이 없습니다.")
        return

    print(f"2. 엄선된 {len(candidates)}개 후보의 원문 팩트 기반 한국어 요약 생성 중...")
    candidates = enrich_candidates_with_korean_summary(candidates)

    last_upd_id = get_latest_update_id()
    now_ts = time.time()

    pending_data = {
        "status": "pending",
        "created_at": now_ts,
        "last_update_id": last_upd_id,
        "candidates": candidates,
    }
    with open(PENDING_FILE, "w", encoding="utf-8") as f:
        json.dump(pending_data, f, ensure_ascii=False, indent=2)

    # 텔레그램 메시지 구성
    lines = [
        "🔔 [노마드 & 기어] 오늘의 블로그 후보 3선 도착!",
        "아래 3개 후보 중 블로그에 발행할 아이템을 골라 버튼을 누르거나 숫자(1, 2, 3)를 답장해 주세요.\n",
    ]
    emojis = ["1️⃣", "2️⃣", "3️⃣"]
    buttons = []

    for i, item in enumerate(candidates):
        num = i + 1
        emoji = emojis[i] if i < len(emojis) else f"[{num}]"
        lines.append(
            f"{emoji} [{item['source']}] {item['ko_title']}\n"
            f"• 원제: {item['title']}\n"
            f"• 핵심: {item['ko_summary']}\n"
            f"• 원문 확인: {item['link']}\n"
        )
        buttons.append(
            [{"text": f"{emoji} {num}번 아이템으로 블로그 발행하기", "callback_data": f"publish:{num}"}]
        )

    lines.append("💡 아래 버튼을 터치하거나 채팅창에 1, 2, 3을 입력하시면 해당 원문으로 블로그 글이 자동 발행됩니다!")
    msg_text = "\n".join(lines)

    print("3. 스마트폰 텔레그램으로 후보 3선 및 선택 버튼 전송 중...")
    send_telegram_message(msg_text, reply_markup={"inline_keyboard": buttons})

    if wait_seconds <= 0:
        print("후보 전송 완료! (대기 모드 종료)")
        return

    print(f"4. 사용자의 즉시 선택 여부를 {wait_seconds}초 동안 대기합니다...")
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        choice, _ = check_user_selection(last_upd_id, now_ts)
        if choice is not None:
            execute_publish(choice, pending_data)
            return
        time.sleep(3)

    print("즉시 선택 대기 시간이 종료되었습니다. 이후 선택 시 백그라운드 스케줄러가 확인하여 발행합니다.")


def mode_check() -> None:
    """
    2단계: 대기 중인 후보(pending_candidates.json)가 있을 때
    텔레그램에서 사용자의 선택(버튼 클릭 또는 1/2/3 답장)이 왔는지 확인하고 발행합니다.
    """
    if not os.path.exists(PENDING_FILE):
        print("대기 중인 후보 파일이 없습니다.")
        return

    with open(PENDING_FILE, "r", encoding="utf-8") as f:
        pending_data = json.load(f)

    if pending_data.get("status") != "pending":
        print(f"현재 승인 대기 중인 상태가 아닙니다 (status={pending_data.get('status')}).")
        return

    last_upd_id = pending_data.get("last_update_id", 0)
    created_at = pending_data.get("created_at", 0)

    choice, _ = check_user_selection(last_upd_id, created_at)
    if choice is not None:
        execute_publish(choice, pending_data)
    else:
        print("아직 사용자의 후보 선택 응답이 도착하지 않았습니다.")


def validate_env() -> None:
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
    parser = argparse.ArgumentParser(description="Nomad & Gear Blog Bot")
    parser.add_argument(
        "--mode",
        choices=["propose", "check"],
        default="propose",
        help="propose: 후보 3개 발송 및 대기 / check: 선택 응답 확인 후 발행",
    )
    parser.add_argument(
        "--wait",
        type=int,
        default=120,
        help="propose 모드에서 전송 직후 사용자 응답을 기다릴 시간(초)",
    )
    args = parser.parse_args()

    if args.mode == "propose":
        mode_propose(wait_seconds=args.wait)
    elif args.mode == "check":
        mode_check()


if __name__ == "__main__":
    main()
