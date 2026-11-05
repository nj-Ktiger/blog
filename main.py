import os
import requests
import feedparser
from google import genai

# 1. 환경 설정 (비밀 키)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

def get_trending_item():
    """해외 크라우드펀딩 / 신규 테크 RSS에서 최신 아이템 수집"""
    rss_url = "https://www.producthunt.com/feed" # 또는 킥스타터 트렌딩 피드
    feed = feedparser.parse(rss_url)
    if not feed.entries:
        return None
    
    top_item = feed.entries[0]
    return {
        "title": top_item.title,
        "link": top_item.link,
        "summary": top_item.summary
    }

def generate_blog_draft(item_info):
    """Gemini AI를 사용해 매력적인 한국어 블로그/뉴스레터 형식으로 요약"""
    client = genai.Client(api_key=GEMINI_API_KEY)
    
    prompt = f"""
    당신은 테크 및 라이프스타일 트렌드 큐레이터 '노마드 & 기어'의 전문 에디터입니다.
    아래 해외 신제품/아이디어 아이템 정보를 읽고, 한국 2030 독자들의 호기심을 자극하는 매력적인 블로그/뉴스레터 초안을 작성하세요.

    [아이템 원문 정보]
    - 제품명: {item_info['title']}
    - 링크: {item_info['link']}
    - 내용: {item_info['summary']}

    [작성 규칙]
    1. 제목: 클릭하고 싶게 매력적인 한 줄 제목 (예: "펀딩 대박? ~한 아이템 등장")
    2. 3줄 핵심 요약 (어떤 제품인지, 가장 혁신적인 기능, 추천 대상)
    3. 본문: 친절하고 감각적인 말투 (~해요, ~해보세요)
    4. 원문 링크를 포함할 것
    """
    
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt
    )
    return response.text

def send_telegram(message):
    """내 스마트폰 텔레그램으로 전송"""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    requests.post(url, json=payload, timeout=10)

def main():
    print("1. 최신 트렌드 아이템 수집 중...")
    item = get_trending_item()
    if not item:
        print("수집된 아이템이 없습니다.")
        return

    print("2. Gemini AI로 매력적인 글 초안 생성 중...")
    draft = generate_blog_draft(item)

    print("3. 스마트폰 텔레그램으로 알림 전송 중...")
    alert_msg = f"🔔 **[노마드 & 기어] 오늘의 초안 도착!**\n\n{draft}\n\n🔗 원문: {item['link']}"
    send_telegram(alert_msg)
    print("완료! 스마트폰을 확인하세요.")

if __name__ == "__main__":
    main()
