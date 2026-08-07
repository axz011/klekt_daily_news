import os
import smtplib
from email.message import EmailMessage
import requests
from datetime import datetime
from dateutil import parser
import pytz
import time

# Configuration from env
SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
EMAIL_FROM = os.getenv("EMAIL_FROM")
EMAIL_TO = os.getenv("EMAIL_TO")
NEWSAPI_KEY = os.getenv("NEWSAPI_KEY")  # optional
TRANSLATE_API_URL = os.getenv("TRANSLATE_API_URL")  # optional
TRANSLATE_API_KEY = os.getenv("TRANSLATE_API_KEY")  # optional

BJT = pytz.timezone("Asia/Shanghai")

QUERIES = [
    ("政治", "politics"),
    ("经济", "economy OR markets OR inflation OR gdp"),
    ("AI/人工智能", "artificial intelligence OR AI"),
    ("机器人", "robotics OR robot"),
    ("脑机接口", "brain computer interface OR BCI OR brain-machine"),
    ("基础科学", "physics OR chemistry OR biology OR discovery"),
    ("心理学/社会学", "psychology OR sociology"),
    ("信息工程", "information engineering OR computer science OR networking")
]

def translate(text):
    if TRANSLATE_API_URL and TRANSLATE_API_KEY:
        try:
            resp = requests.post(TRANSLATE_API_URL, json={"q": text, "target": "zh"}, headers={"Authorization": f"Bearer {TRANSLATE_API_KEY}"}, timeout=15)
            if resp.status_code == 200:
                j = resp.json()
                if isinstance(j, dict):
                    return j.get("translatedText") or j.get("translation") or next(iter(j.values()))
                return str(j)
        except Exception:
            pass
    # fallback: return English with a marker
    return "(EN only) " + text

def fetch_news_from_newsapi(q, page=1, page_size=20):
    if not NEWSAPI_KEY:
        return []
    url = "https://newsapi.org/v2/everything"
    params = {
        "q": q,
        "language": "en",
        "sortBy": "publishedAt",
        "pageSize": page_size,
        "page": page,
        "apiKey": NEWSAPI_KEY
    }
    try:
        r = requests.get(url, params=params, timeout=15)
        if r.status_code == 200:
            return r.json().get("articles", [])
    except Exception:
        return []
    return []

def collect_top_articles(limit=20):
    seen = set()
    results = []
    for cat, q in QUERIES:
        articles = fetch_news_from_newsapi(q, page_size=5)
        for a in articles:
            url = a.get("url")
            if not url or url in seen:
                continue
            seen.add(url)
            results.append({
                "category": cat,
                "source": a.get("source", {}).get("name") or "",
                "title_en": a.get("title") or "",
                "description": a.get("description") or a.get("content") or "",
                "url": url,
                "publishedAt": a.get("publishedAt")
            })
            if len(results) >= limit:
                return results
        time.sleep(0.5)
    return results[:limit]

def format_bjt(iso_ts):
    if not iso_ts:
        return ""
    try:
        dt = parser.isoparse(iso_ts)
        if dt.tzinfo is None:
            dt = pytz.utc.localize(dt)
        return dt.astimezone(BJT).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return iso_ts

def build_email_body(items):
    lines = []
    lines.append("日报：全球重要新闻（自动生成）\n")
    for i, it in enumerate(items, 1):
        title_en = it["title_en"]
        title_zh = translate(title_en)
        lines.append(f"{i}. 类目：{it['category']}")
        lines.append(f"   发布时间（北京时间）：{format_bjt(it.get('publishedAt'))}")
        lines.append(f"   标题（中/英）：{title_zh} / {title_en}")
        lines.append(f"   内容摘要：{it.get('description','')}")
        lines.append(f"   信息源：{it.get('source')}")
        lines.append(f"   原文链接：{it.get('url')}\n")
    return "\n".join(lines)

def send_email(subject, body_plain):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg.set_content(body_plain)

    # send via SMTP
    if SMTP_PORT == 465:
        server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=60)
    else:
        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=60)
        server.starttls()
    server.login(SMTP_USERNAME, SMTP_PASSWORD)
    server.send_message(msg)
    server.quit()

def main():
    items = collect_top_articles(limit=20)
    if not items:
        print("未获取到新闻，请检查 NEWSAPI_KEY 是否配置或 API 可用性；仍将发送空邮件以便日志。")
    body = build_email_body(items)
    subject = f"每日要闻 — {datetime.now(BJT).strftime('%Y-%m-%d')}"
    send_email(subject, body)
    print("邮件已发送（或已尝试发送）。")

if __name__ == "__main__":
    main()
