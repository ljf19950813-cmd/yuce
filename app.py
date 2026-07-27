# ================= 0. 云端数据库初始化 (Google Sheets) =================
try:
    conn = st.connection("gsheets", type="gsheets")
except Exception as e:
    logging.warning(f"Google Sheets 连接失败，回测功能将禁用: {e}")
    conn = None
import logging
import time
import re
import numpy as np
import pandas as pd
from openai import OpenAI
from datetime import datetime, timedelta
from io import BytesIO
import warnings
import httpx
import streamlit as st
import json

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

# ================= 🆕 新增：Session State 初始化 =================
if "current_active_prompt" not in st.session_state:
    st.session_state.current_active_prompt = "当前使用默认四轨制 Prompt（未进化）"
if "analysis_report" not in st.session_state:
    st.session_state.analysis_report = None
if "prompt_draft" not in st.session_state:
    st.session_state.prompt_draft = None
# ================= 🆕 结束 =================


# ================= 🛡️ 安全日期生成器 (次日买入适配版 - 终极修复) =================
def get_safe_trade_dates():
    """
    【次日买入适配版】
    核心逻辑：强制基于系统真实时间推算，抛弃缓存，防止日期错乱。
    自动跳过周末及2026年法定节假日。
    """
    holidays_2026 = {
        '20260101', '20260102', '20260103', '20260104', 
        '20260214', '20260215', '20260216', '20260217', '20260218', '20260219', '20260220', '20260221', '20260222', '20260223', '20260228',
        '20260404', '20260405', '20260406',
        '20260501', '20260502', '20260503', '20260504', '20260505', '20260509',
        '20260619', '20260620', '20260621',
        '20260925', '20260926', '20260927',
        '20261001', '20261002', '20261003', '20261004', '20261005', '20261006', '20261007', '20261010'
    }
    now = datetime.now()
    current_str = now.strftime('%Y%m%d')
    current_time_int = int(now.strftime('%H%M'))
    safe_time_threshold = 1530
    is_data_stable = current_time_int >= safe_time_threshold
    dates = []
    for i in range(20):
        d = now - timedelta(days=i)
        d_str = d.strftime('%Y%m%d')
        if d.weekday() < 5 and d_str not in holidays_2026:
            dates.append(d_str)
    if not is_data_stable and current_str in dates:
        dates.remove(current_str)
    t_day = dates[0] if dates else current_str
    t_minus_1 = dates[1] if len(dates) > 1 else dates[0]
    t_minus_2 = dates[2] if len(dates) > 2 else dates[0]
    last_week = dates[4] if len(dates) > 4 else dates[0]
    logging.info(f"⏱️ 系统当前时间: {now.strftime('%Y-%m-%d %H:%M:%S')} | 数据是否稳定: {is_data_stable}")
    logging.info(f"📅 推算交易日列表: {dates[:5]}")
    return {
        "today": t_day,
        "yesterday": t_minus_1,
        "day_before": t_minus_2,
        "last_week": last_week,
        "now_str": now.strftime('%Y%m%d_%H%M'),
        "t_plus_1_label": "次日(T+1)"
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
        non_zero_pct = pct_arr[pct_arr != 0]
        if len(non_zero_pct) > 0 and np.median(np.abs(non_zero_pct)) < 0.5:
            pct_chg = pct_arr * 100
        else:
            pct_chg = pct_arr.copy()
        non_zero_turnover = turnover_arr[turnover_arr != 0]
        if len(non_zero_turnover) > 0 and np.median(np.abs(non_zero_turnover)) < 0.5:
            turnover = turnover_arr * 100
        else:
            turnover = turnover_arr.copy()
        if np.mean(amount_arr) < 100000: 
            amount = amount_arr * 10000
        else:
            amount = amount_arr.copy()
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
                amt_today = float(latest.get('amount', 0))
                amt_prev = float(prev.get('amount', 0))
                if amt_today == 0:
                    amt_today = float(latest.get('volume', 0))
                    amt_prev = float(prev.get('volume', 0))
                if amt_today > amt_prev * 1.05:
                    vol_status = "放量"
                elif amt_today < amt_prev * 0.95:
                    vol_status = "缩量"
                else:
                    vol_status = "平量"
                market_summary.append(f"- {name}: 涨幅 {pct:.2f}%, {vol_status}")
                time.sleep(0.1)
        if df is not None and not df.empty:
            up_count = len(df[df['pct_chg'] > 0])
            down_count = len(df[df['pct_chg'] < 0])
            ratio = up_count / max(down_count, 1)
            sentiment = "极度亢奋" if ratio > 3 else ("强势" if ratio > 1.5 else ("均衡" if ratio > 0.8 else ("弱势" if ratio > 0.5 else "极度冰点")))
            zt_main = len(df[(df['board']=='Main') & (df['pct_chg']>=9.8)])
            dt_main = len(df[(df['board']=='Main') & (df['pct_chg']<=-9.8)])
            big_loss = len(df[df['pct_chg'] < -7.0])
            market_summary.append(f"- 全市场情绪: 涨{up_count}/跌{down_count}, 涨跌比{ratio:.2f}, 【{sentiment}】")
            market_summary.append(f"- 赚钱效应: 主板涨停 {zt_main} 家")
            if dt_main > 10: 
                market_summary.append(f"⚠️ 极度恶劣行情: 跌停 {dt_main} 家，大面 {big_loss} 家！【退潮期，空仓保平安】")
            elif dt_main > 3: 
                market_summary.append(f"⚠️ 局部亏钱效应: 跌停 {dt_main} 家。【接力需极度谨慎】")
            else: 
                market_summary.append(f"- 亏钱效应: 跌停 {dt_main} 家 (风险可控)")
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
    mask = (df['pct_chg'] >= lower_pct) & (df['pct_chg'] <= 9.5) & \
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

# ================= 🚀 5. 历史趋势快照 =================
def get_history_context(tf_client, tf_code):
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
        position_pct = (curr_close - low_60d) / (high_60d - low_60d) * 100 if high_60d > low_60d else 50
        pos_desc = "高位 (接近60日新高)" if position_pct > 80 else ("低位 (接近60日新低)" if position_pct < 20 else "中位震荡区")
        ma5 = df_k['close'].rolling(5).mean().iloc[-1]
        ma20 = df_k['close'].rolling(20).mean().iloc[-1]
        ma60 = df_k['close'].mean()
        trend_desc = "多头排列 (均线向上发散)" if ma5 > ma20 > ma60 else ("空头排列 (均线向下压制)" if ma5 < ma20 < ma60 else "均线缠绕 (方向不明)")
        pressure_desc = f"上方强压力: {high_60d:.2f} (60日最高)"
        support_desc = f"下方强支撑: {low_60d:.2f} (60日最低)"
        avg_vol_20 = df_k['volume'].tail(20).mean()
        curr_vol = df_k.iloc[-1]['volume']
        vol_mult = curr_vol / avg_vol_20 if avg_vol_20 > 0 else 1.0
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

# ================= 6. 纯净版 Prompt =================
ANTI_HALLUCINATION_RULES = """
⚠️ 游资实战铁律（违反将导致严重亏损）：
1. 【严禁编造价格】：所有止损、目标、买点，**必须**基于我提供的【当前真实价格】、【今日最高/低】和【昨收】进行精确数学计算（精确到分）。
2. 【强制数学公式】：输出价格时，必须展示计算过程！严禁凭空捏造！
3. 【严禁脑补历史】：绝对不要使用你训练数据中的历史走势！你的趋势判断**必须且只能**基于我提供的【历史趋势快照】与【今日盘面数据】。
4. 【散户视角】：我是资金量不足50万的散户。我要"一击必杀"的确定性和"断臂求生"的致命止损。
5. 【拒绝端水】：直接告诉我买还是不买？什么价格买？什么价格割肉？
6. 【数据缺失处理】：如果【今日最高/低】与【当前价】相同，说明数据缺失！**严禁**得出"价格没变过"的结论！必须基于【昨收】和【涨幅】反推波动区间！
7. 【纯粹量价推演】：严禁提及、猜测或编造该股票的行业、题材、概念！所有分析必须纯粹基于量价结构、筹码博弈、历史趋势与市场情绪。
8. 【次日买入视角】：明确告知用户当前是基于【T日收盘】复盘，准备在【T+1日（次日）】买入。你的买点、止损点必须考虑次日集合竞价和早盘情绪。
9. 【条件触发机制】：严禁只给一个固定死价格！必须给出"如果次日高开>2%怎么做"、"如果次日低开或平开怎么做"的条件分支策略。
"""

PROMPT_NORMAL = f"""你是一位在A股摸爬滚打15年的顶尖游资，精通"缩量洗盘后的反包博弈"与"反量化盘中埋伏"。
{ANTI_HALLUCINATION_RULES}

请务必严格按照以下格式输出：
### 1. 盘面语言解读 (结合【历史趋势快照】与今日量价，看透主力意图)
### 2. 流动性与量化排雷
### 3. 次日(T+1)竞价与买点策略 (必须分情况讨论：次日高开/平开/低开的应对买点，展示基于今日收盘价和昨收价的计算过程，精确到分)
### 4. 断臂求生止损位 (基于次日买入后的持仓成本，给出动态止损价计算过程)
### 5. 猎手评级与仓位建议 (S/A/B/C)"""

PROMPT_DEMON = f"""你是一位在A股摸爬滚打15年的顶尖游资，精通"龙头首阴反包"与"妖股接力情绪博弈"。
{ANTI_HALLUCINATION_RULES}

请务必严格按照以下格式输出：
### 1. 妖气指数与龙头信仰 (结合【历史趋势快照】分析连板高度、市场身位及今日盘口语言)
### 2. 死亡换手与流动性排雷 (结合成交额、换手率分析当前筹码断层风险)
### 3. 次日(T+1)竞价与买点策略 (必须分情况讨论：次日高开>3%如何抢筹/平开如何半路/低开如何放弃，展示基于今日收盘价和昨收价的计算过程，精确到分)
### 4. 断臂求生止损位 (基于次日买入后的预估持仓成本，给出动态止损价计算过程)
### 5. 猎手评级与仓位建议 (S/A/B/C)"""

PROMPT_DEFENSE = f"""你是一位精通"弱市逆风突破"的A股实战猎手。当前大盘萎靡/冰点，你的任务是在泥沙俱下中寻找"逆市上涨、筹码稳健、即将突破"的真金标的。
{ANTI_HALLUCINATION_RULES}
请务必严格按照以下格式输出：
### 1. 逆风强度与突破逻辑 (结合【历史趋势快照】与今日量价背离，分析突破有效性)
### 2. 筹码结构与量能健康度
### 3. 次日(T+1)竞价与买点策略 (必须分情况讨论：次日高开如何追/平开如何伏击/低开或急跌如何低吸，展示基于今日收盘价和昨收价的计算过程，精确到分)
### 4. 断臂求生止损位 (基于次日买入后的预估持仓成本，给出动态止损价计算过程，跌破某关键位必须无条件离场)
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
【分时】: {minute_feature_text}

⚠️ 【交易计划】：我将于【明日（T+1日）】进行买入操作。请基于上述T日收盘数据，为我制定明日的集合竞价观察点及盘中条件买入策略。"""
    try:
        response = llm_client.chat.completions.create(
            model=CONFIG["LLM_MODEL"],
            messages=[{"role": "system", "content": system_p}, {"role": "user", "content": user_prompt}],
            max_tokens=4096 
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

# ================= 🚀 7. 增强版 HTML 报告导出模块 =================
def robust_md_to_html(md_text):
    if not md_text: return "<p>【暂无分析内容】</p>"
    html = md_text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    html = re.sub(r'^#{1,4}\s+(.*?)$', r'<h3>\1</h3>', html, flags=re.MULTILINE)
    html = re.sub(r'\*\*(.*?)\*\*', r'<strong>\1</strong>', html)
    html = re.sub(r'\*(.*?)\*', r'<em>\1</em>', html)
    lines = html.split('\n')
    processed_lines = []
    in_list = False
    for line in lines:
        stripped = line.strip()
        is_list_item = re.match(r'^[-*]\s+(.*)', stripped) or re.match(r'^\d+\.\s+(.*)', stripped)
        if is_list_item:
            if not in_list:
                processed_lines.append('<ul>')
                in_list = True
            content = re.sub(r'^[-*]\s+', '', stripped)
            content = re.sub(r'^\d+\.\s+', '', content)
            processed_lines.append(f'<li>{content}</li>')
        else:
            if in_list:
                processed_lines.append('</ul>')
                in_list = False
            if stripped.startswith('<h3>'):
                processed_lines.append(stripped)
            elif stripped == '':
                processed_lines.append('<br>')
            else:
                processed_lines.append(f'<p>{stripped}</p>')
    if in_list:
        processed_lines.append('</ul>')
    return '\n'.join(processed_lines)

def export_to_html_report(normal_results, demon_results, defense_results, watchlist_results, market_context, safe_dates):
    css_style = """
    <style>
        body { font-family: 'Segoe UI', 'Microsoft YaHei', Tahoma, Geneva, Verdana, sans-serif; line-height: 1.6; color: #333; max-width: 1000px; margin: 0 auto; padding: 20px; background: #f9f9f9; }
        .header { text-align: center; border-bottom: 3px solid #2c3e50; padding-bottom: 15px; margin-bottom: 30px; }
        .header h1 { color: #2c3e50; margin: 0; }
        .header p { color: #7f8c8d; margin: 5px 0 0; }
        .market-box { background: #fff; border-left: 5px solid #3498db; padding: 15px; margin-bottom: 30px; box-shadow: 0 2px 5px rgba(0,0,0,0.05); white-space: pre-wrap; font-family: monospace; }
        .track-title { background: #2c3e50; color: #fff; padding: 10px 15px; border-radius: 5px 5px 0 0; margin-top: 40px; font-size: 1.2em; font-weight: bold; page-break-before: always; }
        .stock-card { background: #fff; border: 1px solid #ddd; border-radius: 0 0 5px 5px; padding: 20px; margin-bottom: 20px; box-shadow: 0 2px 5px rgba(0,0,0,0.05); page-break-inside: avoid; }
        .stock-header { display: flex; justify-content: space-between; border-bottom: 1px dashed #ccc; padding-bottom: 10px; margin-bottom: 15px; }
        .stock-name { font-size: 1.3em; font-weight: bold; color: #e74c3c; }
        .stock-code { color: #7f8c8d; font-size: 1.1em; }
        .stock-metrics { display: flex; flex-wrap: wrap; gap: 10px; font-size: 0.9em; color: #555; margin-bottom: 15px; background: #f8f9fa; padding: 8px; border-radius: 4px;}
        .metric-item { padding: 4px 8px; background: #e9ecef; border-radius: 3px; }
        .analysis-content h3 { color: #2980b9; border-bottom: 1px solid #eee; padding-bottom: 5px; margin-top: 20px; }
        .analysis-content ul { padding-left: 20px; margin: 10px 0; }
        .analysis-content li { margin-bottom: 8px; }
        .analysis-content strong { color: #c0392b; }
        .analysis-content p { margin: 8px 0; }
        @media print { 
            body { background: #fff; } 
            .stock-card { break-inside: avoid; page-break-inside: avoid; } 
            .track-title { break-before: page; page-break-before: always; }
        }
    </style>
    """
    html_parts = [f"<!DOCTYPE html><html lang='zh-CN'><head><meta charset='UTF-8'><title>四轨制猎手复盘报告</title>{css_style}</head><body>"]
    html_parts.append(f"<div class='header'><h1>👑 四轨制猎手实战报告</h1><p>生成时间: {safe_dates['now_str']} | 基准日(T日): {safe_dates['today']}</p></div>")
    html_parts.append("<h2>🌍 今日大盘与情绪环境</h2>")
    html_parts.append(f"<div class='market-box'>{market_context}</div>")
    def render_track(track_name, track_emoji, results, mode_type):
        if not results: return ""
        track_html = f"<div class='track-title'>{track_emoji} {track_name}</div>"
        for item in results:
            row, final = item['row'], item['final']
            pct_color = "#e74c3c" if row['pct_chg'] >= 0 else "#27ae60"
            analysis_html = robust_md_to_html(final)
            track_html += f"""
            <div class="stock-card">
                <div class="stock-header">
                    <span class="stock-name">{row['name']}</span>
                    <span class="stock-code">{row['code']} | {row['board']}</span>
                </div>
                <div class="stock-metrics">
                    <span class="metric-item">当前价: {row['close']:.2f}</span>
                    <span class="metric-item" style="color:{pct_color}">涨幅: {row['pct_chg']:.2f}%</span>
                    <span class="metric-item">换手: {row['turnover']:.2f}%</span>
                    <span class="metric-item">量比: {row.get('vol_ratio', 0):.2f}</span>
                    <span class="metric-item">成交额: {row['amount']/100000000:.1f}亿</span>
                </div>
                <div class="analysis-content">{analysis_html}</div>
            </div>
            """
        return track_html
    html_parts.append(render_track("轨道一：缩量潜伏池", "🛡️", normal_results, "normal"))
    html_parts.append(render_track("轨道二：主板妖股池", "🐉", demon_results, "demon"))
    html_parts.append(render_track("轨道三：逆风突破池", "🔥", defense_results, "defense"))
    html_parts.append(render_track("自选股深度诊断", "👁️", watchlist_results, "watchlist"))
    html_parts.append("</body></html>")
    return "\n".join(html_parts).encode('utf-8')

# ================= 💀 核心功能：AI 策略"事后验尸"与数据记录 =================

def save_today_predictions(normal_res, demon_res, defense_res, safe_dates):
    """
    功能：每天收盘后，把 AI 给出的买卖点存入 Google Sheets，留待明天验尸
    🆕 新增：同时保存 AI 的完整预测理由，供后续"错题本"使用
    """
    if not conn: return
    
    all_results = []
    for res_list, track_name in [(normal_res, "缩量潜伏"), (demon_res, "主板妖股"), (defense_res, "逆风突破")]:
        for item in res_list:
            row = item['row']
            final_text = item['final']
            
            buy_match = re.search(r'(?:买点|买入).*?(\d+\.\d{2})', final_text)
            stop_match = re.search(r'(?:止损|割肉).*?(\d+\.\d{2})', final_text)
            
            all_results.append({
                "日期": safe_dates['today'],
                "股票名称": row['name'],
                "代码": row['code'],
                "轨道": track_name,
                "T日收盘": round(row['close'], 2),
                "AI建议买点": float(buy_match.group(1)) if buy_match else 0.0,
                "AI建议止损": float(stop_match.group(1)) if stop_match else 0.0,
                "AI预测理由": final_text,  # 🆕 新增：保存完整分析文本
                "T+1日最高": None,
                "T+1日最低": None,
                "T+1日收盘": None,
                "验尸结果": "待验尸"
            })
            
    if all_results:
        df_to_save = pd.DataFrame(all_results)
        try:
            conn.update(worksheet="Sheet1", data=df_to_save, append=True)
            st.success(f"✅ 已将今日 {len(all_results)} 条 AI 策略存入云端数据库，明日自动验尸！")
        except Exception as e:
            st.error(f"❌ 存入 Google Sheets 失败: {e}")

def run_autopsy(safe_dates):
    """
    功能：每次运行程序时，自动检查云端表格里"待验尸"的记录，
    获取它们今天的真实走势，计算胜率并更新表格。
    """
    if not conn: return
    
    try:
        df_history = conn.query(worksheet="Sheet1")
        if df_history is None or df_history.empty: return
        
        pending_rows = df_history[df_history['验尸结果'] == '待验尸'].copy()
        if pending_rows.empty: return
        
        st.info(f"🔍 检测到 {len(pending_rows)} 条历史 AI 策略，正在进行事后验尸...")
        
        symbols_to_check = pending_rows['代码'].unique().tolist()
        today_real_data = get_tickflow_data_for_symbols(tf, symbols_to_check)
        
        updated_indices = []
        for idx, row in pending_rows.iterrows():
            code = row['代码']
            real_row = today_real_data[today_real_data['code'] == code]
            
            if not real_row.empty:
                real = real_row.iloc[0]
                t1_high = real['high']
                t1_low = real['low']
                t1_close = real['close']
                
                ai_buy = row['AI建议买点']
                ai_stop = row['AI建议止损']
                
                result = "数据不足"
                if ai_buy > 0 and ai_stop > 0:
                    if t1_low <= ai_stop:
                        result = f"❌ 爆头止损 (最低{t1_low:.2f}破止损{ai_stop:.2f})"
                    elif t1_high >= ai_buy * 1.05:
                        result = f"🏆 大肉止盈 (最高{t1_high:.2f})"
                    elif t1_close > ai_buy:
                        result = f"✅ 浮盈收盘 (收{t1_close:.2f})"
                    else:
                        result = f"⚠️ 阴跌套牢 (收{t1_close:.2f})"
                
                df_history.at[idx, 'T+1日最高'] = round(t1_high, 2)
                df_history.at[idx, 'T+1日最低'] = round(t1_low, 2)
                df_history.at[idx, 'T+1日收盘'] = round(t1_close, 2)
                df_history.at[idx, '验尸结果'] = result
                updated_indices.append(idx)
                
        if updated_indices:
            conn.update(worksheet="Sheet1", data=df_history)
            
            completed = df_history[df_history['验尸结果'] != '待验尸']
            win_rate = len(completed[completed['验尸结果'].str.contains('大肉|浮盈', na=False)]) / max(len(completed), 1) * 100
            st.success(f"💀 验尸完毕！AI 历史总胜率: **{win_rate:.1f}%** (共 {len(completed)} 笔)")
            
    except Exception as e:
        st.warning(f"验尸过程出现异常 (不影响今日复盘): {e}")

# ================= 🆕 新增：导师 AI 进化引擎 =================

def generate_prompt_evolution(failed_cases_text, current_prompt_desc):
    """
    🆕 导师 AI 核心：分析失败案例，诊断 Prompt 缺陷，生成进化版 Prompt
    """
    if not llm_client:
        return "⚠️ 未配置大模型，无法进行进化分析", None
    
    mentor_system_prompt = """你是一位A股量化策略的"导师级AI"。你的学生是一个使用AI进行短线交易的散户。
你的任务是：
1. 仔细阅读学生提供的【失败交易案例】（包含AI当时的预测理由和最终的验尸结果）。
2. 像经验丰富的老交易员一样，诊断出AI在分析时犯了什么思维错误（如：忽视大盘环境、止损设置不合理、对量价信号误判等）。
3. 基于诊断结果，生成一份【诊断报告】和一段【进化后的ANTI_HALLUCINATION_RULES补丁】。

你的输出格式必须严格遵循：

## 📊 错题诊断报告

（在这里用中文写出你的分析，300字以内。指出核心问题是什么。）

## 🔧 进化补丁

（在这里输出一段新的规则文本，将被追加到原有的 ANTI_HALLUCINATION_RULES 中。要求：
- 用编号列表格式
- 每条规则必须具体、可执行
- 针对诊断出的具体问题
- 不超过5条新规则）"""

    user_prompt = f"""## 当前系统状态
当前 Prompt 状态: {current_prompt_desc}

## 近期失败案例（错题本）
{failed_cases_text}

请诊断这些失败案例的共性问题，并生成进化补丁。"""

    try:
        response = llm_client.chat.completions.create(
            model=CONFIG["LLM_MODEL"],
            messages=[
                {"role": "system", "content": mentor_system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            max_tokens=3000
        )
        full_response = response.choices[0].message.content
        
        # 解析返回内容，拆分出诊断报告和进化补丁
        report = full_response
        new_patch = ""
        
        if "## 🔧 进化补丁" in full_response:
            parts = full_response.split("## 🔧 进化补丁")
            report = parts[0].replace("## 📊 错题诊断报告", "").strip()
            new_patch = parts[1].strip()
        elif "进化补丁" in full_response:
            parts = re.split(r'#+\s*进化补丁', full_response)
            if len(parts) > 1:
                report = parts[0].strip()
                new_patch = parts[1].strip()
        
        return report, new_patch
        
    except Exception as e:
        return f"❌ 导师 AI 调用失败: {e}", None


# ================= 8. Streamlit Web 主界面 =================
st.set_page_config(page_title="V25.0 四轨猎魔策略 (AI进化版)", layout="wide")
st.title("👑 四轨制猎手 V25.0 (AI 自我进化版)")

safe_dates = get_safe_trade_dates()
st.caption(f"📅 当前基准交易日: {safe_dates['today']} | 上一交易日: {safe_dates['yesterday']}")

# 🌟 每次打开应用，第一件事：自动验尸昨天的记录！
run_autopsy(safe_dates)

with st.sidebar:
    st.header("⚙️ 全市场扫描参数")
    top_n_normal = st.slider("🛡️ 缩量轨 TOP N", 1, 20, CONFIG["TOP_N_NORMAL"])
    top_n_demon = st.slider("🐉 妖股轨 TOP N", 1, 10, CONFIG["TOP_N_DEMON"])
    st.divider()
    st.header("👁️ 自选股监控")
    watchlist_input = st.text_area("输入代码 (每行一个或逗号分隔)", value="600519, 000858, 300750", height=150)
    st.divider()
    
    # 🆕 新增：Prompt 进化控制面板
    st.header("🧬 AI 策略进化中心")
    st.caption(f"当前状态: {st.session_state.current_active_prompt}")
    
    run_prompt_evolution = st.button("🔍 分析错题本并生成优化方案", use_container_width=True)
    
    st.divider()
    run_market_scan = st.button("🚀 全市场四轨扫描", type="primary", use_container_width=True)
    run_watchlist = st.button("👁️ 自选股深度诊断", type="secondary", use_container_width=True)


# ================= 🆕 新增：Prompt 进化执行逻辑 =================
if run_prompt_evolution:
    if not llm_client:
        st.error("❌ 未配置 LLM 客户端，无法进行策略进化")
    else:
        with st.spinner("正在从 Sheet1 提取错题本，导师 AI 正在批改作业..."):
            try:
                df_history = conn.query(worksheet="Sheet1")
                
                if df_history is None or df_history.empty:
                    st.warning("⚠️ 表格里还没有历史数据，请先运行几次四轨扫描。")
                else:
                    # 筛选失败案例：包含 '爆头' 或 '套牢' 的记录
                    failed_df = df_history[
                        df_history['验尸结果'].str.contains('爆头|套牢|数据不足', na=False)
                    ]
                    
                    if failed_df.empty:
                        st.success("🎉 太棒了！近期 AI 预测全部盈利，暂无需进化。")
                    else:
                        # 拼接失败案例文本（最多取最近 8 个）
                        failed_text = ""
                        reason_col = 'AI预测理由' if 'AI预测理由' in failed_df.columns else '轨道'
                        
                        for _, row in failed_df.tail(8).iterrows():
                            failed_text += f"【案例】日期:{row['日期']} | 股票:{row['股票名称']}({row['代码']}) | 轨道:{row['轨道']}\n"
                            reason_text = str(row[reason_col])[:500] if reason_col in row else "无记录"
                            failed_text += f"AI当时的预测理由: {reason_text}...\n"
                            failed_text += f"最终验尸结果: {row['验尸结果']}\n\n"
                        
                        st.info(f"📝 提取了 {len(failed_df.tail(8))} 个失败案例，正在调用导师 AI...")
                        
                        # 调用导师 AI
                        report, new_patch = generate_prompt_evolution(
                            failed_text, 
                            st.session_state.current_active_prompt
                        )
                        
                        st.session_state.analysis_report = report
                        st.session_state.prompt_draft = new_patch
                        st.rerun()
                        
            except Exception as e:
                st.error(f"❌ 读取错题本失败: {e}")

# 🆕 新增：展示进化分析结果与确认按钮
if st.session_state.analysis_report and st.session_state.prompt_draft:
    st.header("🧬 AI 策略进化工作台")
    
    with st.container():
        st.markdown("### 📊 导师诊断报告")
        st.markdown(st.session_state.analysis_report)
        
        st.markdown("### 🔧 进化补丁预览")
        st.code(st.session_state.prompt_draft, language="text")
        
        col1, col2 = st.columns(2)
        with col1:
            if st.button("✅ 确认应用进化补丁", type="primary"):
                # 将补丁追加到全局 ANTI_HALLUCINATION_RULES
                global ANTI_HALLUCINATION_RULES
                ANTI_HALLUCINATION_RULES = ANTI_HALLUCINATION_RULES + "\n\n## 进化补丁 (来自错题分析)\n" + st.session_state.prompt_draft
                
                # 重新生成所有 Prompt（使补丁生效）
                global PROMPT_NORMAL, PROMPT_DEMON, PROMPT_DEFENSE, PROMPT_WATCHLIST
                PROMPT_NORMAL = f"""你是一位在A股摸爬滚打15年的顶尖游资，精通"缩量洗盘后的反包博弈"与"反量化盘中埋伏"。
{ANTI_HALLUCINATION_RULES}

请务必严格按照以下格式输出：
### 1. 盘面语言解读 (结合【历史趋势快照】与今日量价，看透主力意图)
### 2. 流动性与量化排雷
### 3. 次日(T+1)竞价与买点策略 (必须分情况讨论：次日高开/平开/低开的应对买点，展示基于今日收盘价和昨收价的计算过程，精确到分)
### 4. 断臂求生止损位 (基于次日买入后的持仓成本，给出动态止损价计算过程)
### 5. 猎手评级与仓位建议 (S/A/B/C)"""
                
                PROMPT_DEMON = f"""你是一位在A股摸爬滚打15年的顶尖游资，精通"龙头首阴反包"与"妖股接力情绪博弈"。
{ANTI_HALLUCINATION_RULES}

请务必严格按照以下格式输出：
### 1. 妖气指数与龙头信仰 (结合【历史趋势快照】分析连板高度、市场身位及今日盘口语言)
### 2. 死亡换手与流动性排雷 (结合成交额、换手率分析当前筹码断层风险)
### 3. 次日(T+1)竞价与买点策略 (必须分情况讨论：次日高开>3%如何抢筹/平开如何半路/低开如何放弃，展示基于今日收盘价和昨收价的计算过程，精确到分)
### 4. 断臂求生止损位 (基于次日买入后的预估持仓成本，给出动态止损价计算过程)
### 5. 猎手评级与仓位建议 (S/A/B/C)"""
                
                PROMPT_DEFENSE = f"""你是一位精通"弱市逆风突破"的A股实战猎手。当前大盘萎靡/冰点，你的任务是在泥沙俱下中寻找"逆市上涨、筹码稳健、即将突破"的真金标的。
{ANTI_HALLUCINATION_RULES}
请务必严格按照以下格式输出：
### 1. 逆风强度与突破逻辑 (结合【历史趋势快照】与今日量价背离，分析突破有效性)
### 2. 筹码结构与量能健康度
### 3. 次日(T+1)竞价与买点策略 (必须分情况讨论：次日高开如何追/平开如何伏击/低开或急跌如何低吸，展示基于今日收盘价和昨收价的计算过程，精确到分)
### 4. 断臂求生止损位 (基于次日买入后的预估持仓成本，给出动态止损价计算过程，跌破某关键位必须无条件离场)
### 5. 逆风评级与仓位建议 (S/A/B/C)"""
                
                PROMPT_WATCHLIST = f"""你是一位严苛的自选股审视者。请结合当前大盘环境、历史趋势与今日量价，对这只自选股进行"灵魂拷问"。
{ANTI_HALLUCINATION_RULES}
请务必严格按照以下格式输出：
### 1. 趋势与量价审视 (结合【历史趋势快照】分析当前是多头/空头/震荡，以及趋势健康度)
### 2. 量价背离排雷
### 3. 去留决断 (明确给出：加仓/持有/减仓/清仓)
### 4. 关键价格锚点 (必须基于真实价格与历史高低点计算出具体的支撑/压力位，展示计算过程)"""
                
                # 更新状态
                st.session_state.current_active_prompt = f"已进化 (补丁应用时间: {datetime.now().strftime('%m-%d %H:%M')})"
                
                # 写入 Prompt_History 表
                try:
                    try:
                        hist_df = conn.query(worksheet="Prompt_History")
                        ver_num = f"v1.{len(hist_df)}" if hist_df is not None and not hist_df.empty else "v1.0"
                    except:
                        ver_num = "v1.0"
                        
                    new_row = pd.DataFrame([{
                        "Timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
                        "Version": ver_num,
                        "Prompt_Content": st.session_state.prompt_draft,
                        "Analysis_Report": st.session_state.analysis_report
                    }])
                    
                    conn.update(worksheet="Prompt_History", data=new_row, append=True)
                    st.success(f"🎉 进化成功！{ver_num} 补丁已全局生效，下次扫描将使用新规则。")
                except Exception as e:
                    st.warning(f"⚠️ 补丁已在本次会话生效，但写入云端历史表失败 (请确保建了 Prompt_History 标签页): {e}")
                
                st.session_state.analysis_report = None
                st.session_state.prompt_draft = None
                st.rerun()
                
        with col2:
            if st.button("❌ 放弃本次进化"):
                st.session_state.analysis_report = None
                st.session_state.prompt_draft = None
                st.rerun()


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

    normal_results, demon_results, defense_results, watchlist_results = [], [], [], []

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
        
        total_tasks = len(normal_df) + len(demon_df) + len(defense_df)
        if total_tasks == 0: 
            st.warning("今日暂无符合三轨条件的标的")
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

            try:
                save_today_predictions(normal_results, demon_results, defense_results, safe_dates)
            except Exception as e:
                logging.error(f"保存今日预测到 Google Sheets 失败: {e}")
                st.warning(f"⚠️ 今日预测结果未能成功写入云端表格，但不影响本次查看: {e}")

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
        html_data = export_to_html_report(normal_results, demon_results, defense_results, [], market_context, safe_dates)
        if html_data:
            st.download_button(label="📥 下载全市场四轨复盘 HTML 报告", data=html_data, file_name=f"四轨制复盘_{safe_dates['now_str']}.html", mime="text/html")

    if run_watchlist:
        st.info("👁️ 【自选股】正在获取您的持仓数据...")
        watchlist_symbols = [s.strip() for s in re.split(r'[,\n\s]+', watchlist_input) if s.strip()]
        watchlist_df = get_tickflow_data_for_symbols(tf, watchlist_symbols)
        if not watchlist_df.empty:
            watchlist_df = calculate_real_vol_ratio(watchlist_df)
            watch_codes = watchlist_df['tf_code'].tolist()
            minute_features = get_minute_features(tf, watch_codes)
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
            html_data = export_to_html_report([], [], [], watchlist_results, market_context, safe_dates)
            if html_data:
                st.download_button(label="📥 下载自选股诊断 HTML 报告", data=html_data, file_name=f"自选股诊断_{safe_dates['now_str']}.html", mime="text/html")
        else:
            st.warning("⚠️ 未获取到有效自选股数据，请检查代码输入是否正确")
