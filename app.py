import logging
import time
import re
import numpy as np
import pandas as pd
from openai import OpenAI
from datetime import datetime, timedelta
from openpyxl.styles import Font, Alignment, PatternFill
from io import BytesIO
import warnings
import httpx
import streamlit as st

try:
    from tickflow import TickFlow
except ImportError:
    TickFlow = None

warnings.filterwarnings("ignore")

# ================= 1. 全局配置 =================
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s', datefmt='%H:%M:%S')

try:
    TF_API_KEY = st.secrets["TF_API_KEY"]
    LLM_API_KEY = st.secrets["LLM_API_KEY"]
except KeyError as e:
    st.error(f"❌ 缺少必要的密钥配置: {e}")
    st.info("请在 Streamlit Cloud 的 Settings -> Secrets 中添加 TF_API_KEY 和 LLM_API_KEY")
    st.stop()

CONFIG = {
    "TOP_N_NORMAL": 5,
    "TOP_N_DEMON": 3,
    "TOP_N_DEFENSE": 3,
    "TF_API_KEY": TF_API_KEY,
    "LLM_API_KEY": LLM_API_KEY,
    "LLM_BASE_URL": "https://api.deepseek.com/v1", 
    "LLM_MODEL": "deepseek-v4-pro" 
}

# ================= 2. 客户端安全初始化 =================
tf = None
if TickFlow:
    try:
        if CONFIG["TF_API_KEY"] == "YOUR_TICKFLOW_API_KEY":
            tf = TickFlow.free()
        else:
            tf = TickFlow(api_key=CONFIG["TF_API_KEY"])
    except Exception as e:
        logging.error(f"TickFlow 客户端初始化失败: {e}")

llm_client = None
if CONFIG["LLM_API_KEY"] != "YOUR_LLM_API_KEY":
    try:
        llm_client = OpenAI(
            api_key=CONFIG["LLM_API_KEY"], 
            base_url=CONFIG["LLM_BASE_URL"],
            timeout=httpx.Timeout(60.0, connect=15.0)
        )
    except Exception as e:
        logging.error(f"LLM 客户端初始化失败: {e}")

# ================= 🛡️ 安全日期生成器 =================
@st.cache_data(ttl=3600, show_spinner=False)
def get_safe_trade_dates():
    now = datetime.now()
    current_str = now.strftime('%Y%m%d')
    is_closed = (now.hour > 15) or (now.hour == 15 and now.minute >= 30)
    dates = []
    for i in range(20):
        d = now - timedelta(days=i)
        if d.weekday() < 5: dates.append(d.strftime('%Y%m%d'))
    if not is_closed and current_str in dates: dates.remove(current_str)
    return {
        "today": dates[0] if dates else current_str, "yesterday": dates[1] if len(dates)>1 else dates[0],
        "day_before": dates[2] if len(dates)>2 else dates[0], "last_week": dates[4] if len(dates)>4 else dates[0],
        "now_str": now.strftime('%Y%m%d_%H%M')
    }

# ================= 3. 数据获取与清洗 =================
def get_data_tickflow():
    if not tf: return None, 0.0
    try:
        logging.info("🚀 获取全市场 A 股日线快照...")
        df = tf.quotes.get(universes=["CN_Equity_A"], as_dataframe=True)
        if df is None or df.empty: return None, 0.0

        df['tf_code'] = df['symbol'].astype(str)
        df['code'] = df['tf_code'].str.split('.').str[0]
        df['name'] = df['ext.name'].astype(str) if 'ext.name' in df.columns else '未知'

        def safe_col(col_name, default=0.0):
            if col_name in df.columns: return pd.to_numeric(df[col_name], errors='coerce').fillna(default).values
            return np.full(len(df), default)

        close_arr = safe_col('last_price', 0.0)
        high_arr = safe_col('high_price', 0.0)
        low_arr = safe_col('low_price', 0.0)
        pre_close_arr = safe_col('pre_close', 0.0)
        pct_arr = safe_col('ext.change_pct', 0.0)
        turnover_arr = safe_col('ext.turnover_rate', 0.0)
        amount_arr = safe_col('amount', 0.0)
        vol_arr = safe_col('volume', 0.0)

        pct_chg = pct_arr * 100 if np.abs(pct_arr).max() < 1.5 else pct_arr
        turnover = turnover_arr * 100 if turnover_arr.max() < 1.5 else turnover_arr
        amount = amount_arr * 10000 if amount_arr.mean() < 100000 else amount_arr

        pre_close_final = pre_close_arr.copy()
        mask_no_pre = pre_close_final == 0
        if mask_no_pre.any():
            safe_pct = pct_chg[mask_no_pre]
            safe_pct = np.where(safe_pct == -100, -99.9, safe_pct)
            pre_close_final[mask_no_pre] = close_arr[mask_no_pre] / (1 + safe_pct / 100)

        limit_up = pre_close_final * 1.10
        limit_down = pre_close_final * 0.90
        high_final = np.where((high_arr == 0) | (high_arr < close_arr), np.maximum(close_arr, (close_arr + limit_up) / 2), high_arr)
        low_final = np.where((low_arr == 0) | (low_arr > close_arr), np.minimum(close_arr, (close_arr + limit_down) / 2), low_arr)

        df['close'] = close_arr
        df['high'] = high_final
        df['low'] = low_final
        df['pre_close'] = pre_close_final
        df['pct_chg'] = pct_chg
        df['turnover'] = turnover
        df['amount'] = amount
        df['volume'] = vol_arr

        def identify_board(code):
            code = str(code)
            if code.startswith(('60', '00')): return 'Main'
            elif code.startswith(('30', '68')): return 'GEM'
            return 'Other'
        df['board'] = df['code'].apply(identify_board)
        market_avg_pct = float(df['pct_chg'].mean())
        return df, market_avg_pct
    except Exception as e:
        logging.error(f"❌ 数据获取异常: {e}")
        return None, 0.0

def get_market_context(tf_client, df):
    if not tf_client: return "【大盘数据缺失】", 1.0
    indices = {"上证指数": "000001.SH", "创业板指": "399006.SZ"}
    market_summary = []
    ratio = 1.0
    try:
        for name, code in indices.items():
            df_k = tf_client.klines.get(code, period="1d", count=5, as_dataframe=True)
            if df_k is not None and len(df_k) >= 2:
                latest, prev = df_k.iloc[-1], df_k.iloc[-2]
                close_today = float(latest.get('close', latest.get('last_price')))
                close_prev = float(prev.get('close', prev.get('last_price')))
                pct = (close_today - close_prev) / close_prev * 100 if close_prev > 0 else 0
                vol_today = float(latest.get('volume', 0))
                vol_prev = float(prev.get('volume', 0))
                vol_status = "放量" if vol_today > vol_prev * 1.1 else ("缩量" if vol_prev * 0.9 > vol_today else "平量")
                market_summary.append(f"- {name}: 涨幅 {pct:.2f}%, {vol_status}")
                time.sleep(0.1)
        if df is not None and not df.empty:
            up_count = len(df[df['pct_chg'] > 0])
            down_count = len(df[df['pct_chg'] < 0])
            ratio = up_count / max(down_count, 1)
            sentiment = "极度亢奋" if ratio > 3 else ("强势" if ratio > 1.5 else ("均衡" if ratio > 0.8 else ("弱势" if ratio > 0.5 else "极度冰点")))
            zt_main = len(df[(df['board']=='Main') & (df['pct_chg']>9.5)])
            dt_main = len(df[(df['board']=='Main') & (df['pct_chg']<-9.5)])
            big_loss = len(df[df['pct_chg'] < -7.0])
            market_summary.append(f"- 全市场情绪: 涨{up_count}/跌{down_count}, 涨跌比{ratio:.2f}, 【{sentiment}】")
            market_summary.append(f"- 赚钱效应: 主板涨停 {zt_main} 家")
            if dt_main > 10: market_summary.append(f"⚠️ 极度恶劣行情: 跌停 {dt_main} 家，大面 {big_loss} 家！【退潮期，空仓保平安】")
            elif dt_main > 3: market_summary.append(f"⚠️ 局部亏钱效应: 跌停 {dt_main} 家。【接力需极度谨慎】")
            else: market_summary.append(f"- 亏钱效应: 跌停 {dt_main} 家 (风险可控)")
        return "\n".join(market_summary), ratio
    except Exception as e:
        return f"【大盘数据获取异常: {e}】", 1.0

def get_tickflow_data_for_symbols(tf_client, symbols_list):
    if not tf_client: return pd.DataFrame()
    parsed_symbols = []
    for s in symbols_list:
        s = str(s).strip()
        if '.' in s: parsed_symbols.append(f"{s.split('.')[1]}.{s.split('.')[0]}")
        else: parsed_symbols.append(f"{s}.SH" if s.startswith('6') else f"{s}.SZ")

    valid_rows = []
    for tf_code in parsed_symbols:
        try:
            df_k = tf_client.klines.get(tf_code, period="1d", count=2, as_dataframe=True)
            if df_k is None or df_k.empty or len(df_k) < 2: continue
            latest, prev = df_k.iloc[-1], df_k.iloc[-2]
            close_today = float(latest.get('close', latest.get('last_price')))
            close_prev = float(prev.get('close', prev.get('last_price'))) 
            pct = (close_today - close_prev) / close_prev * 100 if close_prev > 0 else 0
            high = float(latest.get('high', latest.get('high_price', 0)))
            low = float(latest.get('low', latest.get('low_price', 0)))
            if high == 0 or high < close_today: high = max(close_today, close_prev * 1.05)
            if low == 0 or low > close_today: low = min(close_today, close_prev * 0.95)
            vol_today = float(latest.get('volume', 0))
            vol_prev = float(prev.get('volume', 0))
            vol_ratio = vol_today / vol_prev if vol_prev > 0 else 99.0
            
            name = tf_code.split('.')[0]
            turnover = 0.0
            amount = 0.0
            try:
                info = tf_client.quotes.get(symbols=[tf_code], as_dataframe=True)
                if info is not None and not info.empty:
                    if 'ext.name' in info.columns: name = str(info.iloc[0]['ext.name'])
                    if 'ext.turnover_rate' in info.columns: turnover = float(info.iloc[0].get('ext.turnover_rate', 0))
                    elif 'turnover_rate' in info.columns: turnover = float(info.iloc[0].get('turnover_rate', 0))
                    if 'amount' in info.columns: amount = float(info.iloc[0].get('amount', 0))
                    if 0 < turnover < 1.5: turnover *= 100
                    if 0 < amount < 100000: amount *= 10000
            except: pass

            valid_rows.append({
                'tf_code': tf_code, 'code': tf_code.split('.')[0], 'name': name,
                'close': close_today, 'high': high, 'low': low, 'pre_close': close_prev,
                'pct_chg': pct, 'turnover': turnover, 'amount': amount, 'vol_ratio': vol_ratio,
                'board': 'Main' if tf_code.endswith('.SH') or tf_code.startswith('00') else 'GEM',
                'industry': '自选股'
            })
            time.sleep(0.1)
        except Exception as e:
            logging.error(f"获取 {tf_code} 失败: {e}")
    return pd.DataFrame(valid_rows)

# ================= 4. 四轨制筛选器 =================
def filter_normal_stocks(df):
    df = df[~df['name'].str.contains('ST|退', na=False)]
    df = df[df['board'].isin(['Main', 'GEM'])]
    main_mask = (df['board'] == 'Main') & (df['pct_chg'] >= 2.0) & (df['pct_chg'] <= 7.5)
    gem_mask = (df['board'] == 'GEM') & (df['pct_chg'] >= 2.0) & (df['pct_chg'] <= 15.0)
    common_mask = (df['amount'] >= 150000000) & (df['turnover'] <= 20.0)
    return df[(main_mask | gem_mask) & common_mask].sort_values(by='turnover', ascending=True).head(20)

def filter_demon_stocks(df):
    df = df[~df['name'].str.contains('ST|退', na=False)]
    df = df[df['board'] == 'Main'] 
    price_mask = df['close'] <= 30.0
    turnover_mask = (df['turnover'] >= 10.0) & (df['turnover'] <= 40.0)
    amount_mask = df['amount'] >= 300000000 
    pct_mask = df['pct_chg'] >= 7.0 
    return df[price_mask & turnover_mask & amount_mask & pct_mask].sort_values(by='pct_chg', ascending=False).head(10)

def filter_defense_stocks(df, tf_client, market_avg_pct=0.0):
    df = df[~df['name'].str.contains('ST|退', na=False)]
    df = df[df['board'] == 'Main']
    lower_pct = max(0.5, market_avg_pct + 1.0)
    mask = (df['pct_chg'] >= lower_pct) & (df['pct_pct'] <= 9.5) & \
           (df['amount'] >= 150000000) & (df['turnover'] >= 3.0) & (df['turnover'] <= 15.0) & \
           (df['close'] >= 5.0)
    candidates = df[mask].sort_values(by='pct_chg', ascending=False).head(20)
    if candidates.empty: return pd.DataFrame()
    verified_codes = []
    for _, row in candidates.iterrows():
        try:
            df_k = tf_client.klines.get(row['tf_code'], period="1d", count=5, as_dataframe=True)
            if df_k is not None and len(df_k) >= 3:
                today_close = float(df_k.iloc[-1].get('close', df_k.iloc[-1].get('last_price')))
                recent_3d_high = float(df_k.iloc[-3:]['high'].max()) if 'high' in df_k.columns else 0
                vol_today = float(df_k.iloc[-1].get('volume', 0))
                vol_yesterday = float(df_k.iloc[-2].get('volume', 0))
                vol_ratio = vol_today / vol_yesterday if vol_yesterday > 0 else 99.0
                if today_close >= recent_3d_high * 0.99 and vol_ratio <= 3.0:
                    verified_codes.append(row['tf_code'])
                time.sleep(0.05)
        except: continue
    return candidates[candidates['tf_code'].isin(verified_codes)].head(CONFIG['TOP_N_DEFENSE'])

def calculate_real_vol_ratio(candidate_df):
    real_vol_ratios = []
    for _, row in candidate_df.iterrows():
        try:
            df_k = tf.klines.get(row['tf_code'], period="1d", count=6, as_dataframe=True)
            if df_k is not None and len(df_k) >= 2:
                today_vol = pd.to_numeric(df_k.iloc[-1]['volume'], errors='coerce')
                past_5d_avg_vol = pd.to_numeric(df_k.iloc[:-1]['volume'], errors='coerce').mean()
                vol_ratio = today_vol / past_5d_avg_vol if past_5d_avg_vol > 0 else 99.0
            else: vol_ratio = 99.0
        except: vol_ratio = 99.0
        real_vol_ratios.append(vol_ratio)
        time.sleep(0.05)
    candidate_df['vol_ratio'] = real_vol_ratios
    return candidate_df

# ================= 🚀 5. 新增：历史趋势快照 (解决位置盲区) =================
def get_history_context(tf_client, tf_code):
    """
    获取过去 60 天的关键趋势锚点，让 AI 有“位置感”和“趋势感”
    """
    if not tf_client: return "【历史趋势数据缺失】"
    try:
        df_k = tf_client.klines.get(tf_code, period="1d", count=60, as_dataframe=True)
        if df_k is None or len(df_k) < 20: return "【历史数据不足，无法判断长周期趋势】"
        
        df_k['close'] = pd.to_numeric(df_k['close'], errors='coerce')
        df_k['high'] = pd.to_numeric(df_k['high'], errors='coerce')
        df_k['low'] = pd.to_numeric(df_k['low'], errors='coerce')
        df_k['volume'] = pd.to_numeric(df_k['volume'], errors='coerce')
        
        curr_close = df_k.iloc[-1]['close']
        high_60d = df_k['high'].max()
        low_60d = df_k['low'].min()
        
        # 1. 位置感计算
        position_pct = (curr_close - low_60d) / (high_60d - low_60d) * 100 if high_60d > low_60d else 50
        pos_desc = "高位 (接近60日新高)" if position_pct > 80 else ("低位 (接近60日新低)" if position_pct < 20 else "中位震荡区")
        
        # 2. 趋势感计算 (均线)
        ma5 = df_k['close'].rolling(5).mean().iloc[-1]
        ma20 = df_k['close'].rolling(20).mean().iloc[-1]
        ma60 = df_k['close'].mean()
        trend_desc = "多头排列 (均线向上发散)" if ma5 > ma20 > ma60 else ("空头排列 (均线向下压制)" if ma5 < ma20 < ma60 else "均线缠绕 (方向不明)")
        
        # 3. 筹码压力与支撑
        pressure_desc = f"上方强压力: {high_60d:.2f} (60日最高)"
        support_desc = f"下方强支撑: {low_60d:.2f} (60日最低)"
        
        # 4. 近期量能异动
        avg_vol_20 = df_k['volume'].tail(20).mean()
        curr_vol = df_k.iloc[-1]['volume']
        vol_mult = curr_vol / avg_vol_20 if avg_vol_20 > 0 else 1.0
        
        # 5. 连板/涨停基因检测
        pct_changes = df_k['close'].pct_change() * 100
        limit_ups_60d = (pct_changes > 9.5).sum()
        limit_downs_60d = (pct_changes < -9.5).sum()
        
        res = f"""
【历史趋势快照 (过去60天)】
- 当前坐标: 处于60日区间 {pos_desc} (位置分位: {position_pct:.0f}%)
- 均线形态: {trend_desc} (MA5:{ma5:.2f} | MA20:{ma20:.2f} | MA60:{ma60:.2f})
- 筹码边界: {pressure_desc} | {support_desc}
- 量能对比: 今日成交量是20日均量的 {vol_mult:.1f} 倍
- 股性基因: 60日内涨停 {limit_ups_60d} 次，跌停 {limit_downs_60d} 次
"""
        return res
    except Exception as e:
        return f"【历史数据获取异常: {e}】"

# ================= 6. 纯净版 Prompt (防幻觉 + 历史增强) =================
ANTI_HALLUCINATION_RULES = """
⚠️ 游资实战铁律（违反将导致严重亏损）：
1. 【严禁编造价格】：所有止损、目标、买点，**必须**基于我提供的【当前真实价格】、【今日最高/低】和【昨收】进行精确数学计算（精确到分）。
2. 【强制数学公式】：输出价格时，必须展示计算过程！严禁凭空捏造！
3. 【严禁脑补历史】：绝对不要使用你训练数据中的历史走势！你的趋势判断**必须且只能**基于我提供的【历史趋势快照】与【今日盘面数据】。
4. 【散户视角】：我是资金量不足50万的散户。我要"一击必杀"的确定性和"断臂求生"的致命止损。
5. 【拒绝端水】：直接告诉我买还是不买？什么价格买？什么价格割肉？
6. 【数据缺失处理】：如果【今日最高/低】与【当前价】相同，说明数据缺失！**严禁**得出"价格没变过"的结论！必须基于【昨收】和【涨幅】反推波动区间！
7. 【纯粹量价推演】：严禁提及、猜测或编造该股票的行业、题材、概念！所有分析必须纯粹基于量价结构、筹码博弈、历史趋势与市场情绪。
"""

PROMPT_NORMAL = f"""你是一位在A股摸爬滚打15年的顶尖游资，精通"缩量洗盘后的反包博弈"与"反量化盘中埋伏"。
{ANTI_HALLUCINATION_RULES}
请务必严格按照以下格式输出：
### 1. 盘面语言解读 (结合【历史趋势快照】与今日量价，看透主力意图)
### 2. 流动性与量化排雷
### 3. 反量化买点 (必须包含具体价格计算过程，精确到分)
### 4. 断臂求生止损位 (必须包含具体价格计算过程)
### 5. 猎手评级与仓位建议 (S/A/B/C)"""

PROMPT_DEMON = f"""你是一位A股顶尖的"主板(10%)连板妖股接力"大师。你从不看基本面，只看情绪、筹码和历史股性。
{ANTI_HALLUCINATION_RULES}
请务必严格按照以下格式输出：
### 1. 情绪定性与连板身位 (结合历史涨停基因与今日量价，判断龙头还是杂毛)
### 1. 情绪定性与连板身位 (结合历史涨停基因与今日量价，判断龙头还是杂毛)
### 2. 筹码断层与爆量风险
### 3. 主板接力手法 (必须包含具体打板/半路价格计算过程)
### 4. 断头铡刀止损 (必须包含具体止损价格计算过程)
### 5. 猎手评级与仓位建议 (S/A/B/C)"""

PROMPT_DEFENSE = f"""你是一位精通"弱市逆风突破"的A股实战猎手。当前大盘萎靡/冰点，你的任务是在泥沙俱下中寻找"逆市上涨、筹码稳健、即将突破"的真金标的。
{ANTI_HALLUCINATION_RULES}
请务必严格按照以下格式输出：
### 1. 逆风强度与突破逻辑 (结合历史压力位与今日量价背离，分析突破有效性)
### 2. 筹码结构与量能健康度
### 3. 稳健突破买点 (必须包含具体价格计算过程)
### 4. 证伪止损价 (突破失败必须走，基于关键支撑位给出具体价格计算过程)
### 5. 逆风评级与仓位建议 (S/A/B/C)"""

PROMPT_WATCHLIST = f"""你是一位严苛的自选股审视者。请结合当前大盘环境、历史趋势与今日量价，对这只自选股进行"灵魂拷问"。
{ANTI_HALLUCINATION_RULES}
请务必严格按照以下格式输出：
### 1. 趋势与量价审视 (结合【历史趋势快照】分析当前是多头/空头/震荡，以及趋势健康度)
### 2. 量价背离排雷
### 3. 去留决断 (明确给出：加仓/持有/减仓/清仓)
### 4. 关键价格锚点 (必须基于真实价格与历史高低点计算出具体的支撑/压力位，展示计算过程)"""

def analyze_with_llm(stock_dict, minute_feature_text, market_context, history_context, mode="normal"):
    if not llm_client: return "⚠️ 未配置大模型", "⚠️ 无Key"
    news_context = "【请纯粹基于盘面量价与情绪进行推演】"
    if mode == "demon": system_p = PROMPT_DEMON
    elif mode == "defense": system_p = PROMPT_DEFENSE
    elif mode == "watchlist": system_p = PROMPT_WATCHLIST
    else: system_p = PROMPT_NORMAL

    price_info = f"""
【真实价格锚点 (严禁瞎编，必须基于此计算，展示公式)】
- 当前价: {stock_dict.get('close', '未知')} 元
- 今日最低: {stock_dict.get('low', '未知')} 元
- 今日最高: {stock_dict.get('high', '未知')} 元
- 昨日收盘: {stock_dict.get('pre_close', '0.0')} 元
"""

    user_prompt = f"""【大盘与情绪】:\n{market_context}
【历史趋势快照】:\n{history_context}
【实时新闻】:\n{news_context}\n{price_info}
【股票】: {stock_dict.get('name')} ({stock_dict.get('code')}) | {stock_dict.get('board')}
【数据】: 涨幅 {stock_dict.get('pct_chg', 0):.2f}%, 量比 {stock_dict.get('vol_ratio', 0):.2f}, 成交额 {stock_dict.get('amount', 0)/100000000:.1f}亿, 换手 {stock_dict.get('turnover', 0):.2f}%
【分时】: {minute_feature_text}"""

    try:
        response = llm_client.chat.completions.create(
            model=CONFIG["LLM_MODEL"],
            messages=[{"role": "system", "content": system_p}, {"role": "user", "content": user_prompt}],
            max_tokens=3000 
        )
        reasoning = getattr(response.choices[0].message, 'reasoning_content', '')
        final = response.choices[0].message.content
        return reasoning, final
    except Exception as e:
        return str(e), f"❌ AI 调用失败: {e}"

def get_minute_features(tf_client, tf_codes):
    features_map = {}
    for tf_code in tf_codes:
        try:
            df_k = tf_client.klines.get(tf_code, period="15m", count=16, as_dataframe=True)
            if df_k is None or df_k.empty: features_map[tf_code] = "【分时缺失】"; continue
            total_vol = pd.to_numeric(df_k['volume'], errors='coerce').sum()
            tail_vol = pd.to_numeric(df_k['volume'].tail(2), errors='coerce').sum()
            tail_ratio = (tail_vol / total_vol * 100) if total_vol > 0 else 0
            logic_text = " (尾盘异动抢筹)" if tail_ratio > 25 else (" (尾盘平淡/资金流出)" if tail_ratio < 10 else "")
            features_map[tf_code] = f"尾盘30分量占比: {tail_ratio:.1f}%{logic_text}"
            time.sleep(0.05)
        except:
            features_map[tf_code] = "【分时异常】"
    return features_map

# ================= 7. 表格提取与 Excel 导出 =================
def extract_section(text, keyword):
    pattern = rf"###\s*\d+\.\s*{keyword}.*?\n(.*?)(?=\n###\s*\d+\.|$)"
    match = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
    if match:
        res = match.group(1).strip()
        clean_res = res.replace('\n', ' | ').replace('**', '').replace('*', '')
        return clean_res[:80] + "..." if len(clean_res) > 80 else clean_res
    return "未提及"

def export_to_excel_bytes(normal_results, demon_results, defense_results, watchlist_results):
    all_data = []
    for item in normal_results:
        row, final = item['row'], item['final']
        all_data.append({"轨道": "🛡️ 潜伏池", "股票名称": row['name'], "代码": row['code'], "当前价": row.get('close', ''), "涨幅%": f"{row['pct_chg']:.2f}", "换手%": f"{row['turnover']:.2f}", "量比": f"{row['vol_ratio']:.2f}", "评级": extract_section(final, "猎手评级"), "买点推演": extract_section(final, "反量化买点"), "止损位": extract_section(final, "断臂求生止损位")})
    for item in demon_results:
        row, final = item['row'], item['final']
        all_data.append({"轨道": "🐉 妖股池", "股票名称": row['name'], "代码": row['code'], "当前价": row.get('close', ''), "涨幅%": f"{row['pct_chg']:.2f}", "换手%": f"{row['turnover']:.2f}", "量比": f"{row['vol_ratio']:.2f}", "评级": extract_section(final, "猎手评级"), "买点推演": extract_section(final, "主板接力手法"), "止损位": extract_section(final, "断头铡刀止损")})
    for item in defense_results:
        row, final = item['row'], item['final']
        all_data.append({"轨道": "🔥 逆风池", "股票名称": row['name'], "代码": row['code'], "当前价": row.get('close', ''), "涨幅%": f"{row['pct_chg']:.2f}", "换手%": f"{row['turnover']:.2f}", "量比": f"{row['vol_ratio']:.2f}", "评级": extract_section(final, "逆风评级"), "买点推演": extract_section(final, "稳健突破买点"), "止损位": extract_section(final, "证伪止损价")})
    for item in watchlist_results:
        row, final = item['row'], item['final']
        all_data.append({"轨道": "👁️ 自选股", "股票名称": row['name'], "代码": row['code'], "当前价": row.get('close', ''), "涨幅%": f"{row['pct_chg']:.2f}", "换手%": f"{row['turnover']:.2f}", "量比": f"{row['vol_ratio']:.2f}", "评级": extract_section(final, "去留决断"), "买点推演": extract_section(final, "关键价格锚点"), "止损位": "-"})
    if not all_data: return None
    df = pd.DataFrame(all_data)
    output = BytesIO()
    try:
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name='四轨制复盘')
            worksheet = writer.sheets['四轨制复盘']
            header_font = Font(color="FFFFFF", bold=True)
            header_fill = PatternFill(start_color="000000", end_color="000000", fill_type="solid")
            for cell in worksheet[1]: cell.font = header_font; cell.fill = header_fill; cell.alignment = Alignment(horizontal="center", vertical="center")
            for column in worksheet.columns:
                max_length = max((len(str(cell.value)) if cell.value else 0) for cell in column)
                worksheet.column_dimensions[column[0].column_letter].width = min((max_length + 2) * 1.1, 45)
        output.seek(0)
        return output
    except Exception as e:
        logging.error(f"❌ Excel 导出失败: {e}")
        return None

# ================= 8. Streamlit Web 主界面 =================
st.set_page_config(page_title="V23.0 四轨猎魔策略", layout="wide")
st.title("👑 四轨制猎手 V23.0 (历史趋势增强+纯净量价版)")

safe_dates = get_safe_trade_dates()
st.caption(f"📅 当前基准交易日: {safe_dates['today']} | 上一交易日: {safe_dates['yesterday']}")

with st.sidebar:
    st.header("⚙️ 全市场扫描参数")
    top_n_normal = st.slider("🛡️ 潜伏轨 TOP N", 1, 20, CONFIG["TOP_N_NORMAL"])
    top_n_demon = st.slider("🐉 恶魔轨 TOP N", 1, 10, CONFIG["TOP_N_DEMON"])
    st.divider()
    st.header("👁️ 自选股监控")
    watchlist_input = st.text_area("输入代码 (每行一个或逗号分隔)", value="600519, 000858, 300750", height=150)
    st.divider()
    run_market_scan = st.button("🚀 全市场四轨扫描", type="primary", use_container_width=True)
    run_watchlist = st.button("👁️ 自选股深度诊断", type="secondary", use_container_width=True)

# ================= 9. 执行逻辑隔离 =================
if run_market_scan or run_watchlist:
    if not tf or not llm_client: st.error("❌ 客户端初始化失败，请检查 Secrets 配置"); st.stop()
    CONFIG["TOP_N_NORMAL"] = top_n_normal
    CONFIG["TOP_N_DEMON"] = top_n_demon

    with st.spinner("🚀 正在获取全市场 A 股日线快照..."):
        df, market_avg_pct = get_data_tickflow()
    if df is None: st.error("❌ 大盘数据获取失败"); st.stop()

    market_context, market_ratio = get_market_context(tf, df)
    st.subheader("🌍 今日大盘与情绪环境")
    st.text(market_context)

    if run_market_scan:
        st.info("🛡️ 【轨道一】筛选缩量洗盘猎物...")
        normal_df = filter_normal_stocks(df)
        if not normal_df.empty:
            normal_df = calculate_real_vol_ratio(normal_df)
            normal_df = normal_df[normal_df['vol_ratio'] <= 0.9].head(CONFIG['TOP_N_NORMAL'])

        st.info("🐉 【轨道二】扫描主板妖股...")
        demon_df = filter_demon_stocks(df)
        if not demon_df.empty:
            demon_df = calculate_real_vol_ratio(demon_df)
            demon_df = demon_df.head(CONFIG['TOP_N_DEMON'])

        defense_df = pd.DataFrame()
        if market_ratio < 1.0 or market_avg_pct < 0.0: 
            st.warning("🔥 【轨道三】检测到市场偏弱/冰点，自动激活逆风突破池！")
            defense_df = filter_defense_stocks(df, tf, market_avg_pct)
            if not defense_df.empty: defense_df = calculate_real_vol_ratio(defense_df)

        all_codes = []
        if not normal_df.empty: all_codes.extend(normal_df['tf_code'].tolist())
        if not demon_df.empty: all_codes.extend(demon_df['tf_code'].tolist())
        if not defense_df.empty: all_codes.extend(defense_df['tf_code'].tolist())

        minute_features = get_minute_features(tf, list(set(all_codes)))
        normal_results, demon_results, defense_results = [], [], []

        total_tasks = len(normal_df) + len(demon_df) + len(defense_df)
        if total_tasks == 0: st.warning("今日暂无符合三轨条件的标的")
        else:
            progress_bar = st.progress(0)
            current_task = 0
            if not normal_df.empty:
                for _, row in normal_df.iterrows():
                    current_task += 1; progress_bar.progress(current_task / total_tasks)
                    history_ctx = get_history_context(tf, row['tf_code'])
                    reasoning, final = analyze_with_llm(row.to_dict(), minute_features.get(row['tf_code'], ""), market_context, history_ctx, mode="normal")
                    normal_results.append({'row': row, 'reasoning': reasoning, 'final': final})
                    time.sleep(1)
            if not demon_df.empty:
                for _, row in demon_df.iterrows():
                    current_task += 1; progress_bar.progress(current_task / total_tasks)
                    history_ctx = get_history_context(tf, row['tf_code'])
                    reasoning, final = analyze_with_llm(row.to_dict(), minute_features.get(row['tf_code'], ""), market_context, history_ctx, mode="demon")
                    demon_results.append({'row': row, 'reasoning': reasoning, 'final': final})
                    time.sleep(1)
            if not defense_df.empty:
                for _, row in defense_df.iterrows():
                    current_task += 1; progress_bar.progress(current_task / total_tasks)
                    history_ctx = get_history_context(tf, row['tf_code'])
                    reasoning, final = analyze_with_llm(row.to_dict(), minute_features.get(row['tf_code'], ""), market_context, history_ctx, mode="defense")
                    defense_results.append({'row': row, 'reasoning': reasoning, 'final': final})
                    time.sleep(1)
            progress_bar.empty()

        st.subheader("🛡️ 轨道一：缩量潜伏池")
        if normal_results:
            for idx, item in enumerate(normal_results, 1):
                row, reasoning, final = item['row'], item['reasoning'], item['final']
                with st.expander(f"[{idx}] {row['name']} ({row['code']}) | 涨幅:{row['pct_chg']:.1f}% 换手:{row['turnover']:.1f}%"):
                    if reasoning: st.caption(f"🧠 脑内推演: {reasoning[:500]}...")
                    st.markdown(final)
        else: st.warning("今日暂无符合轨道一条件的标的")

        st.subheader("🐉 轨道二：主板妖股池")
        if demon_results:
            for idx, item in enumerate(demon_results, 1):
                row, reasoning, final = item['row'], item['reasoning'], item['final']
                with st.expander(f"[{idx}] {row['name']} ({row['code']}) | 涨幅:{row['pct_chg']:.1f}% 换手:{row['turnover']:.1f}%"):
                    if reasoning: st.caption(f"🧠 脑内推演: {reasoning[:500]}...")
                    st.markdown(final)
        else: st.warning("今日暂无符合轨道二条件的标的")

        st.subheader("🔥 轨道三：逆风突破池")
        if defense_results:
            for idx, item in enumerate(defense_results, 1):
                row, reasoning, final = item['row'], item['reasoning'], item['final']
                with st.expander(f"[{idx}] {row['name']} ({row['code']}) | 涨幅:{row['pct_chg']:.1f}% 换手:{row['turnover']:.1f}%"):
                    if reasoning: st.caption(f"🧠 脑内推演: {reasoning[:500]}...")
                    st.markdown(final)
        else: st.info("今日大盘情绪强势，逆风池未激活 (或无符合条件标的)")

        st.divider()
        excel_data = export_to_excel_bytes(normal_results, demon_results, defense_results, [])
        if excel_data:
            st.download_button(label="📥 下载全市场四轨复盘 Excel 报告", data=excel_data, file_name=f"四轨制复盘_{safe_dates['now_str']}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    if run_watchlist:
        st.info("👁️ 【自选股】正在获取您的持仓数据...")
        watchlist_symbols = [s.strip() for s in re.split(r'[,\n\s]+', watchlist_input) if s.strip()]
        watchlist_df = get_tickflow_data_for_symbols(tf, watchlist_symbols)
        if not watchlist_df.empty:
            watchlist_df = calculate_real_vol_ratio(watchlist_df)
            watch_codes = watchlist_df['tf_code'].tolist()
            minute_features = get_minute_features(tf, watch_codes)
            watchlist_results = []
            total_tasks = len(watchlist_df)
            progress_bar = st.progress(0)
            for idx, (_, row) in enumerate(watchlist_df.iterrows()):
                progress_bar.progress((idx + 1) / total_tasks)
                history_ctx = get_history_context(tf, row['tf_code'])
                reasoning, final = analyze_with_llm(row.to_dict(), minute_features.get(row['tf_code'], ""), market_context, history_ctx, mode="watchlist")
                watchlist_results.append({'row': row, 'reasoning': reasoning, 'final': final})
                time.sleep(1)
            progress_bar.empty()
            st.subheader("👁️ 自选股深度诊断结果")
            for idx, item in enumerate(watchlist_results, 1):
                row, reasoning, final = item['row'], item['reasoning'], item['final']
                with st.expander(f"[{idx}] {row['name']} ({row['code']}) | 涨幅:{row['pct_chg']:.1f}%"):
                    if reasoning: st.caption(f"🧠 脑内推演: {reasoning[:500]}...")
                    st.markdown(final)
            st.divider()
            excel_data = export_to_excel_bytes([], [], [], watchlist_results)
            if excel_data:
                st.download_button(label="📥 下载自选股诊断 Excel 报告", data=excel_data, file_name=f"自选股诊断_{safe_dates['now_str']}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        else:
            st.warning("⚠️ 未获取到有效自选股数据，请检查代码输入是否正确")
