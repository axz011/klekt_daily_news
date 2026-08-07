"""
RSS-based daily news generator + SMTP sender.
- 从多个主流媒体 RSS 抓取不同类目的新闻（无需 API key）
- 输出最多 20 条，按新闻重要性排序并通过 SMTP 发邮件
- 重要性用简单启发式评分计算（来源权重、标题关键词、摘要长度、发布时间）
环境变量：
  SMTP_HOST, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD, EMAIL_FROM, EMAIL_TO
  TRANSLATE_API_URL, TRANSLATE_API_KEY (可选，用于翻译标题)
"""

import os
import smtplib
from email.message import EmailMessage
from datetime import datetime
from dateutil import parser
import pytz
import feedparser
import requests
import time
import re

BJT = pytz.timezone("Asia/Shanghai")

# 分类与 RSS 列表（可以按需增删）
FEEDS = {
    "政治": [
        "https://feeds.reuters.com/Reuters/worldNews",
        "https://www.bbc.co.uk/zhongwen/simp/index.xml",  # BBC 中文（部分中文）
        "https://www.aljazeera.com/xml/rss/all.xml",
        "https://rss.nytimes.com/services/xml/rss/nyt/World.xml"
    ],
    "经济": [
        "https://feeds.reuters.com/reuters/businessNews",
        "https://www.ft.com/?format=rss",
        "https://rss.cnn.com/rss/money_news_international.rss"
    ],
    "AI/人工智能": [
        "https://techcrunch.com/feed/",
        "https://www.technologyreview.com/feed/",
        "https://www.wired.com/feed/category/gear/latest/rss"
    ],
    "机器人": [
        "https://spectrum.ieee.org/rss/fulltext/robotics",
        "https://roboticsbusinessreview.com/feed/"
    ],
    "脑机接口": [
        "https://www.nature.com/subjects/brain-computer-interface.rss",
        "https://www.sciencedaily.com/rss/mind_brain.xml"
    ],
    "基础科学": [
        "https://www.sciencedaily.com/rss/top/science.xml",
        "https://www.nature.com/nature/articles?type=research&format=rss"
    ],
    "心理学/社会学": [
        "https://www.sciencedaily.com/rss/mind_behavior.xml",
        "https://www.psychologytoday.com/us/rss"
    ],
    "信息工程": [
        "https://www.usenix.org/feed",
        "https://www.infoq.com/feed/"
    ]
}

# SMTP & env
SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
EMAIL_FROM = os.getenv("EMAIL_FROM")
EMAIL_TO = os.getenv("EMAIL_TO")
TRANSLATE_API_URL = os.getenv("TRANSLATE_API_URL")
TRANSLATE_API_KEY = os.getenv("TRANSLATE_API_KEY")

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
    return "(EN only) " + text


def parse_time(entry):
    # feedparser 已解析的 published_parsed 可用；否则使用 published 字段解析
    try:
        if hasattr(entry, "published_parsed") and entry.published_parsed:
            dt = datetime(*entry.published_parsed[:6], tzinfo=pytz.UTC)
            return dt
        if "published" in entry:
            dt = parser.parse(entry.published)
            if dt.tzinfo is None:
                dt = pytz.UTC.localize(dt)
            return dt
    except Exception:
        pass
    return datetime.now(pytz.UTC)


def fetch_from_feed(url):
    try:
        d = feedparser.parse(url)
        if d.bozo:
            # malformed feed occasionally; still try entries
            pass
        return d.entries or []
    except Exception:
        return []


def compute_importance(item):
    """对新闻条目做一个简单的启发式评分，用于排序（越高越重要）。
    规则示例：
      - 来源权重（知名媒体加分）
      - 标题关键词（breaking/独家/突发 等）
      - 摘要长度（较长的摘要+1）
      - 发布时间（24小时内略加分）
    该函数可按需调整权重。
    """
    score = 0.0
    title = (item.get('title_en') or '').lower()
    source = (item.get('source') or '').lower()
    desc = item.get('description') or ''

    # 来源权重（示例列表）
    high_sources = [
        'reuters', 'nytimes', 'ft', 'financial times', 'bbc', 'aljazeera', 'cnn',
        'techcrunch', 'wired', 'nature', 'sciencedaily'
    ]
    for s in high_sources:
        if s in source:
            score += 5
            break

    # 标题关键词
    keywords = ['breaking', 'breaking news', 'exclusive', 'urgent', 'alert', '重大', '突发', '独家', '警报', '危机']
    for k in keywords:
        if k in title:
            score += 4
            break

    # 摘要长度指示信息量
    if len(desc) > 200:
        score += 1

    # 最近 24 小时略加分
    try:
        pub = item.get('published')
        if pub and (datetime.now(pytz.UTC) - pub).total_seconds() < 86400:
            score += 1
    except Exception:
        pass

    return score


def collect_top_items(limit=20):
    items = []
    seen = set()
    # iterate categories round-robin to balance categories
    for category, feeds in FEEDS.items():
        for feed in feeds:
            entries = fetch_from_feed(feed)
            for e in entries:
                link = e.get("link") or e.get("id")
                if not link or link in seen:
                    continue
                seen.add(link)
                title = e.get("title", "").strip()
                summary = (e.get("summary") or e.get("description") or "")[:800]
                published = parse_time(e)
                source = e.get("source", {}).get("title") if e.get("source") else e.get("author") or ""
                items.append({
                    "category": category,
                    "title_en": title,
                    "description": summary,
                    "url": link,
                    "published": published,
                    "source": source
                })
                if len(items) >= limit:
                    break
            if len(items) >= limit:
                break
            time.sleep(0.2)  # polite delay
        if len(items) >= limit:
            break

    # compute importance scores and sort by importance desc, then by published desc
    for it in items:
        it['importance'] = compute_importance(it)

    items.sort(key=lambda x: (x.get('importance', 0), x.get('published')), reverse=True)
    return items[:limit]


def format_bjt(dt):
    if not dt:
        return ""
    try:
        return dt.astimezone(BJT).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(dt)


def build_email_body(items):
    lines = []
    lines.append("日报：全球重要新闻（RSS 源，自动生成）\n")
    for i, it in enumerate(items, 1):
        title_en = it["title_en"]
        title_zh = translate(title_en)
        lines.append(f"{i}. 类目：{it['category']}")
        lines.append(f"   发布时间（北京时间）：{format_bjt(it.get('published'))}")
        # 显示重要性评分以便可解释排序
        lines.append(f"   重要性评分：{it.get('importance', 0):.1f}")
        lines.append(f"   标题（中/英）：{title_zh} / {title_en}")
        lines.append(f"   内容摘要：{it.get('description','')}")
        lines.append(f"   信息源：{it.get('source') or 'RSS'}")
        lines.append(f"   原文链接：{it.get('url')}\n")
    return "\n".join(lines)


def send_email(subject, body_plain):
    if not (SMTP_HOST and SMTP_USERNAME and SMTP_PASSWORD and EMAIL_FROM and EMAIL_TO):
        print("SMTP 配置不完整，邮件不会发送。")
        print("邮件内容预览:\n")
        print(body_plain[:2000])
        return
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg.set_content(body_plain)

    if SMTP_PORT == 465:
        server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=60)
    else:
        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=60)
        server.starttls()
    server.login(SMTP_USERNAME, SMTP_PASSWORD)
    server.send_message(msg)
    server.quit()


def main():
    items = collect_top_items(limit=20)
    if not items:
        print("未获取到新闻：RSS 源可能暂时不可用或网络问题。")
    body = build_email_body(items)
    subject = f"每日要闻 — {datetime.now(BJT).strftime('%Y-%m-%d')}"
    send_email(subject, body)
    print("处理完成（已发送或打印邮件内容）。")


if __name__ == "__main__":
    main()
