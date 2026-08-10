"""
经济新闻：    
"""
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
import subprocess

# deep-translator for backup translation
try:
    from deep_translator import GoogleTranslator
    _deep_translator_available = True
except Exception:
    _deep_translator_available = False

BJT = pytz.timezone("Asia/Shanghai")

# 分类与 RSS 列表（可以按需增删）
FEEDS = {
    "economics/经济": [      
        "https://feeds.reuters.com/reuters/businessNews",
        "https://www.ft.com/?format=rss",
        "https://rss.cnn.com/rss/money_news_international.rss"
        "https://www.bbc.co.uk/zhongwen/simp/index.xml", # BBC 中文（部分中文）
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

# optional chinese conversion libs
try:
    from opencc import OpenCC
    _opencc = OpenCC('t2s')
except Exception:
    _opencc = None

try:
    import zhconv
except Exception:
    zhconv = None


def to_simplified(text):
    """把文本转换为简体中文（尽量）。优先使用 opencc，其次尝试 zhconv，最后尝试调用系统 opencc 命令行（若存在）。失败则原样返回。"""
    if not text:
        return text
    try:
        if _opencc:
            return _opencc.convert(text)
    except Exception:
        pass
    try:
        if zhconv:
            return zhconv.convert(text, 'zh-cn')
    except Exception:
        pass
    # fallback to system opencc CLI if available
    try:
        proc = subprocess.run(['opencc', '-c', 't2s.json'], input=text.encode('utf-8'), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5)
        if proc.returncode == 0:
            return proc.stdout.decode('utf-8')
    except Exception:
        pass
    return text


def contains_cjk(text):
    return bool(re.search('[\u4e00-\u9fff]', text or ''))


def translate(text):
    # 如果标题已经包含中文，就不再调用翻译接口，直接使用原文（后续会统一转换为简体）
    if contains_cjk(text):
        return text
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
    # 回退到 deep-translator
    if _deep_translator_available:
        try:
            return GoogleTranslator(source='auto', target='zh-CN').translate(text)
        except Exception:
            pass
    # 没有可用翻译时返回提示信息
    return "没有对应的译文"


def translate_to_en(text):
    # 将中文翻译为英文
    if not contains_cjk(text):
        return text  # 如果已经是英文，直接返回
    if TRANSLATE_API_URL and TRANSLATE_API_KEY:
        try:
            resp = requests.post(TRANSLATE_API_URL, json={"q": text, "target": "en"}, headers={"Authorization": f"Bearer {TRANSLATE_API_KEY}"}, timeout=15)
            if resp.status_code == 200:
                j = resp.json()
                if isinstance(j, dict):
                    return j.get("translatedText") or j.get("translation") or next(iter(j.values()))
                return str(j)
        except Exception:
            pass
    # 回退到 deep-translator
    if _deep_translator_available:
        try:
            return GoogleTranslator(source='auto', target='en').translate(text)
        except Exception:
            pass
    # 没有可用翻译时返回提示信息
    return "No translation available"


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

                # 保留原始标题用于显示原文（title_en 保持为抓取到的原始标题）
                title_raw = title
                title_en = title_raw

                # 计算中文标题：如果原始标题已含中文，直接使用；否则尝试翻译（若配置了翻译接口）
                title_zh = translate(title_raw)
                # 去除历史遗留前缀
                if isinstance(title_zh, str) and title_zh.startswith("(EN only)"):
                    title_zh = title_zh.replace("(EN only)", "").strip()
                title_zh = to_simplified(title_zh)

                # 处理中英文摘要
                summary_raw = summary
                if contains_cjk(summary_raw):
                    # 摘要是中文，生成英文版本
                    summary_zh = to_simplified(summary_raw)
                    summary_en = translate_to_en(summary_raw)
                else:
                    # 摘要是英文，生成中文版本
                    summary_en = summary_raw
                    summary_zh = to_simplified(translate(summary_raw))

                # 把来源也转换为简体，便于邮件展示
                source_s = to_simplified(source)

                items.append({
                    "category": category,
                    "title_en": title_en,
                    "title_zh": title_zh,
                    "summary_zh": summary_zh,
                    "summary_en": summary_en,
                    "url": link,
                    "published": published,
                    "source": source_s
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


def fetch_page_title(url):
    """尝试抓取文章页面并提取英文标题（og:title 或 HTML <title>），用于当 feed 标题为中文但页面有英文标题时回退使用。"""
    try:
        headers = {"User-Agent": "klekt-daily-news/1.0 (+https://github.com)"}
        resp = requests.get(url, headers=headers, timeout=5)
        if resp.status_code != 200:
            return None
        html = resp.text
        # 尝试 og:title
        m = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', html, flags=re.I)
        if not m:
            m = re.search(r'<meta[^>]+name=["\']twitter:title["\'][^>]+content=["\']([^"\']+)["\']', html, flags=re.I)
        if m:
            title = m.group(1).strip()
            return title
        # fallback to <title>
        m = re.search(r'<title[^>]*>(.*?)</title>', html, flags=re.I|re.S)
        if m:
            title = re.sub(r'\s+', ' ', m.group(1)).strip()
            return title
    except Exception:
        return None
    return None


def build_email_body(items):
    lines = []
    lines.append("日报：全球重要新闻（RSS 源，自动生成）\n")
    for i, it in enumerate(items, 1):
        title_en = it.get("title_en") or ''
        title_zh = it.get("title_zh") or ''
        url = it.get('url') or ''

        # 如果 title_en 看起来是中文（feed 给的是中文），尝试抓取页面标题作为英文回退
        if contains_cjk(title_en):
            fetched = fetch_page_title(url)
            if fetched and not contains_cjk(fetched):
                title_en_display = fetched
            else:
                # 如果抓取不到英文，就保留原始标题作为回退（但仍将内容简体化）
                title_en_display = title_en
        else:
            title_en_display = title_en

        lines.append(f"{i}. 类目：{to_simplified(it['category'])}")
        lines.append(f"   发布时间（北京时间）：{format_bjt(it.get('published'))}")
        # 显示重要性评分以便可解释排序
        lines.append(f"   重要性评分：{it.get('importance', 0):.1f}")
        # 标题：中文（简体） / 英文原文（或抓取到的页面标题）
        lines.append(f"   标题（中/英）：{(title_zh or '—')} / {(title_en_display or '—')}")
        # 内容摘要：中文 / 英文
        lines.append(f"   内容摘要（中）：{it.get('summary_zh','')}")
        lines.append(f"   内容摘要（英）：{it.get('summary_en','')}")
        lines.append(f"   信息源：{it.get('source') or 'RSS'}")
        lines.append(f"   原文链接：{url}\n")
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
    items = collect_top_items(limit=10)
    if not items:
        print("未获取到新闻：RSS 源可能暂时不可用或网络问题。")
    body = build_email_body(items)
    subject = f"每日经济要闻 — {datetime.now(BJT).strftime('%Y-%m-%d')}"
    send_email(subject, body)
    print("处理完成（已发送或打印邮件内容）。")


if __name__ == "__main__":
    main()
