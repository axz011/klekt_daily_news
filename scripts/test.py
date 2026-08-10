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
from datetime import datetime, timedelta
from dateutil import parser
import pytz
import feedparser
import requests
import time
import re
import logging
from typing import List, Dict, Optional, Tuple

# 设置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# 语言检测库（如果没有，使用备用方案）
try:
    from langdetect import detect, DetectorFactory
    DetectorFactory.seed = 0  # 确保结果可重现
    LANG_DETECT_AVAILABLE = True
except ImportError:
    LANG_DETECT_AVAILABLE = False
    logger.warning("langdetect 未安装，将使用简单规则判断语言")

# 翻译库
try:
    from deep_translator import GoogleTranslator
    _deep_translator_available = True
except ImportError:
    _deep_translator_available = False
    logger.warning("deep_translator 未安装，将使用翻译 API（如已配置）")

# 繁简转换库
try:
    from opencc import OpenCC
    _opencc = OpenCC('t2s')
    logger.info("OpenCC 繁简转换已启用")
except ImportError:
    _opencc = None
    logger.warning("OpenCC 未安装，将使用备用转换方案")

try:
    import zhconv
    logger.info("zhconv 繁简转换已启用")
except ImportError:
    zhconv = None

BJT = pytz.timezone("Asia/Shanghai")

# 分类与 RSS 列表
FEEDS = {
    "经济": [
        "https://feeds.reuters.com/reuters/businessNews",
        "https://rss.cnn.com/rss/money_news_international.rss",
        "https://feeds.bbci.co.uk/news/business/rss.xml",
        "https://www.ft.com/?format=rss",
        "https://www.economist.com/finance-and-economics/rss.xml",
    ],
    "科技": [
        "https://feeds.feedburner.com/TechCrunch",
        "https://www.wired.com/feed/rss",
        "https://rss.nytimes.com/services/xml/rss/nyt/Technology.xml",
    ],
    "健康": [
        "https://www.medicalnewstoday.com/feed",
        "https://www.sciencedaily.com/rss/health_medicine.xml",
    ]
}

# 环境变量
SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = os.getenv("SMTP_PORT", "587")
SMTP_USERNAME = os.getenv("SMTP_USERNAME")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
EMAIL_FROM = os.getenv("EMAIL_FROM")
EMAIL_TO = os.getenv("EMAIL_TO")
TRANSLATE_API_URL = os.getenv("TRANSLATE_API_URL")
TRANSLATE_API_KEY = os.getenv("TRANSLATE_API_KEY")


def validate_config() -> bool:
    """验证必要的配置是否完整"""
    required_vars = {
        "SMTP_HOST": SMTP_HOST,
        "SMTP_USERNAME": SMTP_USERNAME,
        "SMTP_PASSWORD": SMTP_PASSWORD,
        "EMAIL_FROM": EMAIL_FROM,
        "EMAIL_TO": EMAIL_TO,
    }
    missing = [k for k, v in required_vars.items() if not v]
    if missing:
        logger.error(f"缺少必要的环境变量: {', '.join(missing)}")
        return False
    
    try:
        int(SMTP_PORT)
    except ValueError:
        logger.error(f"SMTP_PORT 必须是数字，当前值: {SMTP_PORT}")
        return False
    
    # 检查翻译配置
    if not TRANSLATE_API_URL or not TRANSLATE_API_KEY:
        if not _deep_translator_available:
            logger.warning("未配置翻译 API 且 deep_translator 未安装，将保留原文")
    
    return True


def to_simplified(text: str) -> str:
    """将文本转换为简体中文"""
    if not text:
        return text
    
    try:
        if _opencc:
            return _opencc.convert(text)
    except Exception as e:
        logger.debug(f"OpenCC 转换失败: {e}")
    
    try:
        if zhconv:
            return zhconv.convert(text, 'zh-cn')
    except Exception as e:
        logger.debug(f"zhconv 转换失败: {e}")
    
    # 备用：简单移除一些常见繁体字
    try:
        import subprocess
        proc = subprocess.run(
            ['opencc', '-c', 't2s.json'],
            input=text.encode('utf-8'),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5
        )
        if proc.returncode == 0:
            return proc.stdout.decode('utf-8')
    except Exception as e:
        logger.debug(f"系统 opencc 转换失败: {e}")
    
    return text


def detect_language(text: str) -> str:
    """检测文本语言，返回 'zh', 'en', 或 'other'"""
    if not text:
        return 'unknown'
    
    # 优先使用 langdetect
    if LANG_DETECT_AVAILABLE:
        try:
            lang = detect(text)
            if lang in ['zh-cn', 'zh-tw', 'zh']:
                return 'zh'
            elif lang == 'en':
                return 'en'
            else:
                return 'other'
        except Exception as e:
            logger.debug(f"语言检测失败: {e}")
    
    # 备用方案：检查是否包含中日韩文字
    if re.search('[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]', text):
        return 'zh'
    elif re.search('[a-zA-Z]', text) and len(re.findall('[a-zA-Z]', text)) / len(text) > 0.5:
        return 'en'
    else:
        return 'other'


def translate_text(text: str, target_lang: str = 'zh') -> str:
    """翻译文本到目标语言"""
    if not text:
        return text
    
    # 检测语言
    src_lang = detect_language(text)
    
    # 如果已经是目标语言，直接返回（但如果是繁简转换需求，单独处理）
    if src_lang == target_lang:
        if target_lang == 'zh':
            return to_simplified(text)
        return text
    
    # 如果目标语言是中文，确保转换为简体
    if target_lang == 'zh':
        # 使用翻译 API
        if TRANSLATE_API_URL and TRANSLATE_API_KEY:
            try:
                response = requests.post(
                    TRANSLATE_API_URL,
                    json={"q": text, "target": "zh"},
                    headers={"Authorization": f"Bearer {TRANSLATE_API_KEY}"},
                    timeout=15
                )
                if response.status_code == 200:
                    data = response.json()
                    translated = data.get("translatedText") or data.get("translation") or next(iter(data.values()))
                    if translated:
                        return to_simplified(translated)
            except Exception as e:
                logger.debug(f"翻译 API 请求失败: {e}")
        
        # 备用：使用 deep_translator
        if _deep_translator_available:
            try:
                translated = GoogleTranslator(source='auto', target='zh-CN').translate(text)
                if translated:
                    return to_simplified(translated)
            except Exception as e:
                logger.debug(f"deep_translator 翻译失败: {e}")
    
    # 如果目标语言是英文，翻译为英文
    elif target_lang == 'en':
        if TRANSLATE_API_URL and TRANSLATE_API_KEY:
            try:
                response = requests.post(
                    TRANSLATE_API_URL,
                    json={"q": text, "target": "en"},
                    headers={"Authorization": f"Bearer {TRANSLATE_API_KEY}"},
                    timeout=15
                )
                if response.status_code == 200:
                    data = response.json()
                    translated = data.get("translatedText") or data.get("translation") or next(iter(data.values()))
                    if translated:
                        return translated
            except Exception as e:
                logger.debug(f"翻译 API 请求失败: {e}")
        
        if _deep_translator_available:
            try:
                translated = GoogleTranslator(source='auto', target='en').translate(text)
                if translated:
                    return translated
            except Exception as e:
                logger.debug(f"deep_translator 翻译失败: {e}")
    
    # 如果翻译失败或不可用，返回原文（但如果是中文则转换为简体）
    if detect_language(text) == 'zh':
        return to_simplified(text)
    return text


def parse_time(entry) -> datetime:
    """解析 RSS 条目的发布时间"""
    try:
        if hasattr(entry, "published_parsed") and entry.published_parsed:
            dt = datetime(*entry.published_parsed[:6], tzinfo=pytz.UTC)
            return dt
        
        if "published" in entry:
            dt = parser.parse(entry.published)
            if dt.tzinfo is None:
                dt = pytz.UTC.localize(dt)
            return dt
        
        if "updated" in entry:
            dt = parser.parse(entry.updated)
            if dt.tzinfo is None:
                dt = pytz.UTC.localize(dt)
            return dt
    except Exception as e:
        logger.debug(f"解析时间失败: {e}")
    
    return datetime.now(pytz.UTC)


def fetch_from_feed(url: str, max_items: int = 10) -> List[Dict]:
    """从 RSS 源获取条目"""
    try:
        logger.info(f"正在抓取 RSS: {url}")
        d = feedparser.parse(url)
        
        if d.bozo:
            logger.warning(f"RSS 解析警告 ({url}): {d.bozo_exception}")
        
        entries = []
        for entry in d.entries[:max_items]:
            link = entry.get("link") or entry.get("id")
            if not link:
                continue
            
            title = entry.get("title", "").strip()
            summary = (entry.get("summary") or entry.get("description") or "")[:800]
            published = parse_time(entry)
            
            # 获取来源
            source = ""
            if hasattr(entry, "source") and entry.source:
                source = entry.source.get("title", "")
            elif "author" in entry:
                source = entry.author
            elif "publisher" in entry:
                source = entry.publisher
            
            entries.append({
                "title": title,
                "summary": summary,
                "url": link,
                "published": published,
                "source": source or "Unknown",
            })
        
        logger.info(f"从 {url} 获取到 {len(entries)} 条新闻")
        return entries
        
    except Exception as e:
        logger.error(f"抓取 RSS 失败 ({url}): {e}")
        return []


def compute_importance(item: Dict) -> float:
    """计算新闻重要性评分"""
    score = 0.0
    
    title = item.get('title_en', '').lower()
    source = item.get('source', '').lower()
    summary = item.get('summary', '')
    
    # 来源权重
    high_sources = [
        'reuters', 'ft', 'financial times', 'bbc', 'cnn',
        'bloomberg', 'wsj', 'wall street journal', 'ap', 'associated press'
    ]
    for s in high_sources:
        if s in source:
            score += 5
            break
    
    # 标题关键词
    keywords = [
        'breaking', 'exclusive', 'urgent', 'alert', 
        '重大', '突发', '独家', '警报', '危机',
        'emergency', 'crash', 'plunge', 'surge'
    ]
    for k in keywords:
        if k in title:
            score += 4
            break
    
    # 摘要长度
    if len(summary) > 200:
        score += 1
    elif len(summary) > 100:
        score += 0.5
    
    # 时间新鲜度（24小时内）
    try:
        pub = item.get('published')
        if pub and isinstance(pub, datetime):
            age = (datetime.now(pytz.UTC) - pub).total_seconds()
            if age < 3600:  # 1小时内
                score += 3
            elif age < 21600:  # 6小时内
                score += 2
            elif age < 86400:  # 24小时内
                score += 1
    except Exception as e:
        logger.debug(f"计算时间分数失败: {e}")
    
    return score


def collect_top_items(limit: int = 20, max_per_feed: int = 6) -> List[Dict]:
    """收集并排序新闻条目"""
    all_items = []
    seen_urls = set()
    
    for category, feeds in FEEDS.items():
        logger.info(f"处理分类: {category}")
        
        for feed_url in feeds:
            entries = fetch_from_feed(feed_url, max_per_feed)
            
            for entry in entries:
                url = entry['url']
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                
                # 处理标题
                title_raw = entry['title']
                title_lang = detect_language(title_raw)
                
                if title_lang == 'zh':
                    title_zh = to_simplified(title_raw)
                    title_en = translate_text(title_raw, 'en')
                else:
                    title_en = title_raw
                    title_zh = translate_text(title_raw, 'zh')
                
                # 处理摘要
                summary_raw = entry['summary']
                summary_lang = detect_language(summary_raw)
                
                if summary_lang == 'zh':
                    summary_zh = to_simplified(summary_raw)
                    summary_en = translate_text(summary_raw, 'en')
                else:
                    summary_en = summary_raw
                    summary_zh = translate_text(summary_raw, 'zh')
                
                all_items.append({
                    "category": category,
                    "title_en": title_en,
                    "title_zh": title_zh,
                    "summary_zh": summary_zh,
                    "summary_en": summary_en,
                    "url": url,
                    "published": entry['published'],
                    "source": to_simplified(entry['source']),
                })
                
                if len(all_items) >= limit:
                    break
            
            if len(all_items) >= limit:
                break
        
        if len(all_items) >= limit:
            break
    
    # 计算重要性分数
    for item in all_items:
        item['importance'] = compute_importance(item)
    
    # 排序：按重要性降序，同重要性按时间降序
    all_items.sort(
        key=lambda x: (x['importance'], x['published']),
        reverse=True
    )
    
    return all_items[:limit]


def format_bjt(dt: Optional[datetime]) -> str:
    """格式化为北京时间"""
    if not dt:
        return "时间未知"
    try:
        return dt.astimezone(BJT).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(dt)


def build_email_body(items: List[Dict]) -> str:
    """构建邮件正文"""
    lines = [
        "📰 每日全球新闻摘要",
        f"📅 {datetime.now(BJT).strftime('%Y年%m月%d日 %A')}",
        "=" * 50,
        ""
    ]
    
    for i, item in enumerate(items, 1):
        lines.extend([
            f"【{i}】{item['category']}",
            f"🕐 {format_bjt(item['published'])}",
            f"⭐ 重要性: {item['importance']:.1f}/10.0",
            f"📌 标题 (中文): {item['title_zh']}",
            f"📌 标题 (英文): {item['title_en']}",
            "",
            f"📝 摘要 (中文):",
            f"{item['summary_zh'][:300]}..." if len(item['summary_zh']) > 300 else f"{item['summary_zh']}",
            "",
            f"📝 摘要 (英文):",
            f"{item['summary_en'][:300]}..." if len(item['summary_en']) > 300 else f"{item['summary_en']}",
            "",
            f"🏷️ 来源: {item['source']}",
            f"🔗 链接: {item['url']}",
            "-" * 40,
            ""
        ])
    
    lines.append(f"📊 共 {len(items)} 条新闻")
    lines.append(f"🔄 更新时间: {datetime.now(BJT).strftime('%Y-%m-%d %H:%M:%S')} (北京时间)")
    lines.append("\n🤖 本日报由 AI 自动生成，仅供参考")
    
    return "\n".join(lines)


def send_email(subject: str, body: str) -> bool:
    """发送邮件"""
    if not validate_config():
        logger.warning("配置不完整，仅打印邮件内容预览")
        print("\n" + "=" * 60)
        print("邮件内容预览:")
        print("=" * 60)
        print(body[:2000] + ("..." if len(body) > 2000 else ""))
        print("=" * 60)
        return False
    
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = EMAIL_FROM
        msg["To"] = EMAIL_TO
        msg.set_content(body)
        
        # 根据端口选择连接方式
        port = int(SMTP_PORT)
        if port == 465:
            server = smtplib.SMTP_SSL(SMTP_HOST, port, timeout=60)
        else:
            server = smtplib.SMTP(SMTP_HOST, port, timeout=60)
            server.starttls()
        
        server.login(SMTP_USERNAME, SMTP_PASSWORD)
        server.send_message(msg)
        server.quit()
        
        logger.info(f"邮件发送成功: {EMAIL_TO}")
        return True
        
    except Exception as e:
        logger.error(f"邮件发送失败: {e}")
        return False


def main():
    """主函数"""
    logger.info("开始生成日报...")
    
    # 检查配置
    if not validate_config():
        logger.warning("配置不完整，将只进行测试，不发送邮件")
    
    # 收集新闻
    try:
        items = collect_top_items(limit=20)
        logger.info(f"成功收集 {len(items)} 条新闻")
    except Exception as e:
        logger.error(f"收集新闻失败: {e}")
        return
    
    if not items:
        logger.error("未获取到任何新闻，请检查 RSS 源")
        return
    
    # 构建邮件
    body = build_email_body(items)
    subject = f"每日经济要闻 — {datetime.now(BJT).strftime('%Y-%m-%d')}"
    
    # 发送邮件
    success = send_email(subject, body)
    
    if success:
        logger.info("处理完成")
    else:
        logger.warning("处理完成但邮件未发送")


if __name__ == "__main__":
    main()
