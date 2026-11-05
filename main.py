import html
import os
import re
import sys
import feedparser
import requests
from google import genai

# 1. 환경 설정 (비밀 키)
TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
TELEGRAM_CHAT_ID = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
GEMINI_API_KEY = (os.getenv("GEMINI_API_KEY") or "").strip()

HISTORY_FILE = "sent_history.txt"
MAX_HISTORY = 300

# '노마드 & 기어' 컨셉에 맞춘 글로벌 테크 / 기어 / 디자인 RSS 피드 목록
RSS_FEEDS = [
    {"name": "Yanko Design (혁신 기어/디자인)", "url": "https://www.yankodesign.com/feed/"},
    {"name": "The Gadgeteer (노마드 가젯/기어 리뷰)", "url": "https://the-gadgeteer.com/feed/"},
    {"name": "Product Hunt (신규 테크/아이디어)", "url": "https://www.producthunt.com/feed"},
]


def clean_html(raw_html: str, max_len: int = 400) -> str:
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
    """이미 발송한 아이템 링크 목록을 불러옵니다."""
    if not os.path.exists(HISTORY_FILE):
        return set()
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        return {
            line.strip()
            for line in f
            if line.strip() and not line.strip().startswith("#")
        }


def save_sent_history(new_link: str) -> None:
    """새로 발송한 아이템 링크를 기록 파일에 추가합니다."""
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

    # 최근 MAX_HISTORY 개수만 유지
    existing = existing[-MAX_HISTORY:]

    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        f.write("# 발송 완료된 아이템 링크 기록 (중복 방지용)\n")
        for link in existing:
            f.write(f"{link}\n")


def get_candidate_items(sent_links: set[str], per_feed_limit: int = 6) -> list[dict]:
    """여러 RSS 피드에서 아직 발송하지 않은 최신 후보 아이템들을 수집합니다."""
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


def curate_and_generate_draft(candidates: list[dict]) -> tuple[dict, str]:
    """
    Gemini AI (gemini-3.8-flash) 및 Google Search 연동을 사용하여:
    1) 후보 중 '노마드 & 기어' 컨셉에 가장 잘 어울리는 아이템 1개를 선별하고
    2) 구글 검색으로 상세 스펙/특징을 보충하여 매력적인 블로그 초안을 작성합니다.
    """
    client = genai.Client(api_key=GEMINI_API_KEY)

    candidates_text = "\n\n".join(
        [
            f"[{idx}] 출처: {item['source']}\n"
            f"- 제품명/제목: {item['title']}\n"
            f"- 링크: {item['link']}\n"
            f"- 요약: {item['summary']}"
            for idx, item in enumerate(candidates)
        ]
    )

    prompt = f"""
당신은 테크 및 라이프스타일 트렌드 큐레이터 '노마드 & 기어'의 전문 에디터입니다.

아래 [후보 아이템 목록] 중에서 한국 2030 독자(디지털 노마드, 데스크테리어족, 스마트 기어/가젯 마니아, 생산성 도구에 관심 많은 직장인/크리에이터)의 호기심을 가장 강하게 자극할 수 있는 베스트 아이템 **단 1개**를 골라주세요.
(너무 개발자 전용인 백엔드/인프라 도구는 피하고, 실생활/업무/라이프스타일/하드웨어 기어/매력적인 신제품 위주로 선정하세요.)

선정한 아이템에 대해 Google 검색을 활용하여 실제 제품의 상세 기능, 디자인 특징, 차별점 등을 정확히 파악한 뒤(없는 사실을 지어내지 말 것), 매력적인 블로그/뉴스레터 초안을 작성하세요.

[후보 아이템 목록]
{candidates_text}

[출력 형식 규칙 - 반드시 지킬 것]
첫 번째 줄에는 반드시 선택한 아이템의 번호만 아래 형식으로 출력하세요:
SELECTED_INDEX: 숫자

두 번째 줄부터 블로그 초안을 작성하세요:
1. 제목: 클릭하고 싶게 매력적인 한 줄 제목 (예: "펀딩 대박? ~한 스마트 기어 등장")
2. 3줄 핵심 요약 (어떤 제품인지, 가장 혁신적인 기능, 추천 대상)
3. 본문: 제품의 핵심 매력 포인트와 활용 시나리오를 친절하고 감각적인 말투(~해요, ~해보세요)로 생생하게 소개
4. 에디터의 한마디: 노마드 & 기어 관점에서의 짧은 총평
"""

    interaction = client.interactions.create(
        model="gemini-3.8-flash",
        input=prompt,
        tools=[{"type": "google_search"}],
    )

    full_text = (interaction.output_text or "").strip()
    if not full_text:
        raise RuntimeError("Gemini로부터 응답 텍스트를 받지 못했습니다.")

    selected_idx = 0
    draft_lines = []
    for i, line in enumerate(full_text.splitlines()):
        match = re.match(r"^\s*SELECTED_INDEX\s*:\s*(\d+)\s*$", line, re.IGNORECASE)
        if match and i < 3:
            idx_val = int(match.group(1))
            if 0 <= idx_val < len(candidates):
                selected_idx = idx_val
            continue
        draft_lines.append(line)

    draft = "\n".join(draft_lines).strip()
    return candidates[selected_idx], draft


def send_telegram(message: str) -> None:
    """스마트폰 텔레그램으로 전송 (Markdown 파싱 오류 시 일반 텍스트로 자동 재시도)"""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    # 텔레그램 메시지 최대 길이(4096자) 제한 안전 처리
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

    # Markdown 문법 충돌(400 Bad Request) 시 일반 텍스트로 Fallback 재전송
    if resp.status_code == 400:
        print(
            f"[알림] Markdown 파싱 충돌 감지 ({resp.text}). 일반 텍스트 모드로 재전송합니다..."
        )
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

    print("1. 발송 이력 확인 및 최신 트렌드/기어 후보 아이템 수집 중...")
    sent_links = load_sent_history()
    candidates = get_candidate_items(sent_links)

    if not candidates:
        print("새로 수집된 아이템이 없습니다 (모두 발송 완료되었거나 피드 오류).")
        return

    print(f"   -> 총 {len(candidates)}개의 신규 후보 아이템을 찾았습니다.")
    print("2. Gemini AI (gemini-3.8-flash + Google 검색)로 베스트 아이템 선정 및 초안 작성 중...")
    selected_item, draft = curate_and_generate_draft(candidates)
    print(f"   -> 선정된 아이템: [{selected_item['source']}] {selected_item['title']}")

    print("3. 스마트폰 텔레그램으로 알림 전송 중...")
    alert_msg = (
        f"🔔 [노마드 & 기어] 오늘의 큐레이션 초안 도착!\n"
        f"📌 출처: {selected_item['source']}\n"
        f"📦 아이템: {selected_item['title']}\n\n"
        f"{draft}\n\n"
        f"🔗 원문 링크: {selected_item['link']}"
    )
    send_telegram(alert_msg)

    print("4. 중복 방지를 위해 발송 이력 저장 중...")
    save_sent_history(selected_item["link"])
    print("완료! 스마트폰을 확인하세요.")


if __name__ == "__main__":
    main()
