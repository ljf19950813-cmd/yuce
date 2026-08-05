import requests
from bs4 import BeautifulSoup
import os
import json
import random
import time
from datetime import datetime
from openai import OpenAI

# ============ 配置区（请根据您的实际信息填写） ============
LLM_API_KEY = os.environ.get("LLM_API_KEY")
if not LLM_API_KEY:
    raise ValueError("未找到环境变量 LLM_API_KEY")
LLM_BASE_URL = "https://api.deepseek.com/v1"
LLM_MODEL = "deepseek-chat"                    # 使用便宜模型即可

SPREADSHEET_URL = "https://docs.google.com/spreadsheets/d/1r-YLpb-QJVBegJsntbOs_Bgn8Dyt1LnIypEgor20D6k/edit?pli=1&gid=1959313853#gid=1959313853"       # 您的 Google Sheets 电子表格 URL
SHEET_NAME = "Hot_Topics"                      # 存储热点的工作表名称

SERVICE_ACCOUNT_JSON = os.environ.get("SERVICE_ACCOUNT_JSON")
if not SERVICE_ACCOUNT_JSON:
    raise ValueError("未找到环境变量 SERVICE_ACCOUNT_JSON")
SERVICE_ACCOUNT_INFO = json.loads(SERVICE_ACCOUNT_JSON)

# ============ 多新闻源定义 ============
NEWS_SOURCES = [
    {
        "name": "东方财富-要闻",
        "url": "https://finance.eastmoney.com/a/czqyw.html",
        "selector": ".title a",
        "base_url": "https://finance.eastmoney.com"
    },
    {
        "name": "同花顺-7x24快讯",
        "url": "https://news.10jqka.com.cn/",
        "selector": ".list-con a",
        "base_url": ""
    },
    {
        "name": "新浪财经-滚动新闻",
        "url": "https://finance.sina.com.cn/stock/rollnews.shtml",
        "selector": ".list_009 li a",
        "base_url": ""
    },
    {
        "name": "财联社-电报",
        "url": "https://www.cls.cn/telegraph",
        "selector": ".telegraph-content-box .title",
        "base_url": "https://www.cls.cn"
    },
    {
        "name": "网易财经-要闻",
        "url": "https://money.163.com/",
        "selector": ".top_news_list a",
        "base_url": ""
    },
    # {
    #     "name": "雪球-热门话题",
    #     "url": "https://xueqiu.com/hots/topic",
    #     "selector": ".topic-item .title a",
    #     "base_url": "https://xueqiu.com"
    # },
]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.3 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
]

# ============ 函数定义 ============

def fetch_news():
    """抓取所有新闻源的标题，去重后返回列表"""
    all_titles = []
    for src in NEWS_SOURCES:
        try:
            headers = {"User-Agent": random.choice(USER_AGENTS)}
            resp = requests.get(src["url"], headers=headers, timeout=10)
            soup = BeautifulSoup(resp.content, 'html.parser')
            links = soup.select(src["selector"])
            for link in links[:20]:
                title = link.get_text().strip()
                if title and len(title) > 5:   # 过滤太短的标题
                    all_titles.append(title)
        except Exception as e:
            print(f"抓取 {src['name']} 失败: {e}")
    # 去重
    return list(set(all_titles))

def extract_topics(titles, llm_client):
    """使用 AI 从标题中提炼 3-5 个热点主题及关键词"""
    if not titles: return []
    prompt = f"""以下是今日或近期财经新闻标题列表。请你提炼出其中最核心的3-5个炒作热点主题，每个主题给出：
1. 主题名称（如 "低空经济"）
2. 核心关键词（可用于匹配股票名称或板块，如 ["无人机","飞行汽车","空管系统"]）
3. 热度评分（1-10，10最热）

输出格式（纯JSON，不要其他文字）：
[{{"topic":"主题1","keywords":["词1","词2"],"score":9}}, ...]

新闻标题：
{chr(10).join(titles[:50])}"""

    try:
        resp = llm_client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role":"user","content":prompt}],
            max_tokens=500,
            temperature=0.2
        )
        content = resp.choices[0].message.content
        # 尝试提取 JSON 部分
        start = content.find('[')
        end = content.rfind(']')
        if start != -1 and end != -1:
            return json.loads(content[start:end+1])
        else:
            return []
    except Exception as e:
        print(f"AI 提取主题失败: {e}")
        return []

def save_to_sheets(topics):
    """将热点主题写入 Google Sheets 的 Hot_Topics 工作表"""
    import gspread
    from oauth2client.service_account import ServiceAccountCredentials
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(SERVICE_ACCOUNT_INFO, scope)
    gc = gspread.authorize(creds)
    sh = gc.open_by_url(SPREADSHEET_URL)

    try:
        ws = sh.worksheet(SHEET_NAME)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=SHEET_NAME, rows=100, cols=5)
        ws.append_row(["更新时间", "主题", "关键词", "热度评分", "是否确认"])

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    rows = []
    for t in topics:
        rows.append([now_str, t['topic'], ','.join(t['keywords']), t['score'], '否'])
    ws.append_rows(rows)
    print(f"成功更新 {len(topics)} 个热点主题到 Google Sheets")

# ============ 主流程 ============
if __name__ == "__main__":
    llm_client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL)
    titles = fetch_news()
    if titles:
        topics = extract_topics(titles, llm_client)
        if topics:
            save_to_sheets(topics)
        else:
            print("未提取到热点主题")
    else:
        print("未抓取到新闻标题")
