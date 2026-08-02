# ================= 最先执行：导入所有依赖 =================
import logging
import time
import re
import json
import numpy as np
import pandas as pd
from openai import OpenAI
from datetime import datetime, timedelta
import warnings
import httpx
import streamlit as st

# 必须第一时间调用
st.set_page_config(page_title="V27.5 四轨猎魔 (精简版)", layout="wide")

# ================= 时区修复 =================
try:
    from zoneinfo import ZoneInfo
    tz_shanghai = ZoneInfo("Asia/Shanghai")
except ImportError:
    import pytz
    tz_shanghai = pytz.timezone("Asia/Shanghai")

try:
    from tickflow import TickFlow
except ImportError:
    TickFlow = None
warnings.filterwarnings("ignore")

# ================= Google Sheets 连接 =================
import gspread
from oauth2client.service_account import ServiceAccountCredentials

gc = None
SHEET_NAME = "Sheet1"
PROMPT_HIST_SHEET = "Prompt_History"

try:
    if "gsheets" in st.secrets:
        creds_dict = dict(st.secrets["gsheets"])
        if "private_key" in creds_dict:
            creds_dict["private_key"] = creds_dict["private_key"].replace("\\n", "\n")
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        gc = gspread.authorize(creds)
        logging.info("✅ Google Sheets 连接成功")
    else:
        st.warning("⚠️ 未找到 [gsheets] 配置")
except Exception as e:
    st.error(f"❌ Google Sheets 连接失败: {e}")
spreadsheet_url = st.secrets.get("SPREADSHEET_URL", "")

# ================= 全局配置 =================
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
TF_API_KEY = st.secrets.get("TF_API_KEY", "")
LLM_API_KEY = st.secrets.get("LLM_API_KEY", "")

CONFIG = {
    "TOP_N_NORMAL": 5,
    "TOP_N_DEMON": 3,
    "TOP_N_DEFENSE": 3,
    "TF_API_KEY": TF_API_KEY,
    "LLM_API_KEY": LLM_API_KEY,
    "LLM_BASE_URL": "https://api.deepseek.com/v1",
    "LLM_MODEL": "deepseek-v4-pro"
}

# ================= 客户端初始化 =================
tf = None
if TickFlow and TF_API_KEY:
    try:
        tf = TickFlow(api_key=TF_API_KEY, base_url="https://api.tickflow.org")
        logging.info("✅ TickFlow 付费版连接成功")
    except Exception as e:
        st.error(f"❌ TickFlow 初始化失败: {e}")

llm_client = None
if LLM_API_KEY:
    try:
        llm_client = OpenAI(
            api_key=LLM_API_KEY,
            base_url=CONFIG["LLM_BASE_URL"],
            timeout=httpx.Timeout(180.0, connect=15.0)
        )
    except Exception as e:
        st.error(f"❌ LLM 初始化失败: {e}")

# ================= Session State 初始化 =================
if "current_active_prompt" not in st.session_state:
    st.session_state.current_active_prompt = "默认 Prompt（未进化）"
if "analysis_report" not in st.session_state:
    st.session_state.analysis_report = None
if "prompt_draft" not in st.session_state:
    st.session_state.prompt_draft = None
if "html_report_data" not in st.session_state:
    st.session_state.html_report_data = None
if "html_report_filename" not in st.session_state:
    st.session_state.html_report_filename = ""
if "base_anti_hallucination_rules" not in st.session_state:
    default_rules = """【核心纪律】
1. 严禁编造任何财务数据、价格或涨跌幅。
2. 所有计算必须展示过程，精确到小数点后两位。
3. 必须结合提供的【历史趋势快照】进行分析，严禁脱离数据空谈。"""
    st.session_state.base_anti_hallucination_rules = default_rules
if "active_prompts" not in st.session_state:
    st.session_state.active_prompts = {
        "normal": "", "demon": "", "defense": "", "watchlist": ""
    }

# ================= 安全日期生成器 =================
def get_safe_trade_dates():
    holidays_2026 = {
        '20260101','20260102','20260103','20260104',
        '20260214','20260215','20260216','20260217','20260218','20260219','20260220','20260221','20260222','20260223','20260228',
        '20260404','20260405','20260406',
        '20260501','20260502','20260503','20260504','20260505','20260509',
        '20260619','20260620','20260621',
        '20260925','20260926','20260927',
        '20261001','20261002','20261003','20261004','20261005','20261006','20261007','20261010'
    }
    now = datetime.now(tz_shanghai)
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
    if not is_data_stable:
        st.info(f"⏳ 防盘中失真：基准日回退至 {t_day}")
    return {
        "today": t_day, "yesterday": t_minus_1, "day_before": t_minus_2,
        "last_week": last_week, "now_str": now.strftime('%Y%m%d_%H%M')
    }

# ================= 财务排雷（仅净资产 < 1 过滤） =================
def financial_blacklist_filter(df):
    if df is None or df.empty or not tf:
        return df
    df = df.reset_index(drop=True)
    try:
        symbols = df['tf_code'].unique().tolist()
        fin = tf.financials.metrics(symbols, latest=True, as_dataframe=True)
        if fin is None or fin.empty:
            return df
        fin['code'] = fin['symbol'].str.split('.').str[0]
        merged = df.merge(fin[['code', 'bps']], on='code', how='left')
        mask = merged['bps'].isna() | (merged['bps'] >= 1.0)
        removed = merged[~mask]
        if not removed.empty:
            st.caption(f"🚮 财务排雷剔除 {len(removed)} 只 (每股净资产<1): {removed['name'].tolist()}")
        return df[mask]
    except Exception as e:
        st.warning(f"财务过滤异常: {e}")
        return df

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
                if amt_today > amt_prev * 1.05: vol_status = "放量"
                elif amt_today < amt_prev * 0.95: vol_status = "缩量"
                else: vol_status = "平量"
                market_summary.append(f"- {name}: 涨幅 {pct:.2f}%, {vol_status}")
                time.sleep(0.1)

        if df is not None and not df.empty:
            up_count = len(df[df['pct_chg'] > 0])
            down_count = len(df[df['pct_chg'] < 0])
            ratio = up_count / max(down_count, 1)
            sentiment = "极度亢奋" if ratio > 3 else ("强势" if ratio > 1.5 else ("均衡" if ratio > 0.8 else ("弱势" if ratio > 0.5 else "极度冰点")))
            zt_main = len(df[(df['board']=='Main') & (df['pct_chg']>=9.8)])
            dt_main = len(df[(df['board']=='Main') & (df['pct_chg']<=-9.8)])
            market_summary.append(f"- 涨跌比{ratio:.2f}, 【{sentiment}】, 涨停{zt_main}家")
            if dt_main > 10:
                market_summary.append(f"⚠️ 跌停{dt_main}家！退潮期")
            elif dt_main > 3:
                market_summary.append(f"⚠️ 跌停{dt_main}家，谨慎")
        return "\n".join(market_summary), ratio
    except Exception as e:
        return f"【大盘数据异常: {e}】", 1.0

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
            # 不再调用 adjust_kline
            latest, prev = df_k.iloc[-1], df_k.iloc[-2]
            close_today = float(latest.get('close', latest.get('last_price')))
            close_prev = float(prev.get('close', prev.get('last_price')))
            pct = (close_today - close_prev) / close_prev * 100 if close_prev > 0 else 0
            high = float(latest.get('high', latest.get('high_price', 0)))
            low = float(latest.get('low', latest.get('low_price', 0)))
            open_price = float(latest.get('open', latest.get('open_price', 0)))
            if open_price == 0: open_price = close_prev
            if high == 0 or high < close_today: high = max(close_today, close_prev * 1.05)
            if low == 0 or low > close_today: low = min(close_today, close_prev * 0.95)
            vol_today = float(latest.get('volume', 0))
            vol_prev = float(prev.get('volume', 0))
            vol_ratio = vol_today / vol_prev if vol_prev > 0 else 1.0
            name = tf_code.split('.')[0]
            turnover = 0.0; amount = 0.0
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
                'open': open_price, 'close': close_today, 'high': high, 'low': low,
                'pre_close': close_prev, 'pct_chg': pct, 'turnover': turnover,
                'amount': amount, 'vol_ratio': vol_ratio,
                'board': 'Main' if tf_code.endswith('.SH') or tf_code.startswith('00') else 'GEM',
                'industry': '自选股', 'chip_warning': ''
            })
            time.sleep(0.1)
        except Exception as e:
            logging.error(f"获取 {tf_code} 失败: {e}")
    return pd.DataFrame(valid_rows)

# ================= 4. 四轨制筛选器 (原有) =================
def filter_normal_stocks(df):
    df = df[~df['name'].str.contains('ST|退', na=False)]
    df = df[df['board'].isin(['Main', 'GEM'])]
    main_mask = (df['board'] == 'Main') & (df['pct_chg'] >= 2.0) & (df['pct_chg'] <= 7.5)
    gem_mask = (df['board'] == 'GEM') & (df['pct_chg'] >= 2.0) & (df['pct_chg'] <= 15.0)
    common_mask = (df['amount'] >= 200000000) & (df['amount'] <= 1500000000) & (df['turnover'] <= 20.0)
    price_mask = df['close'] >= 3.0
    return df[(main_mask | gem_mask) & common_mask & price_mask].sort_values(by='turnover', ascending=True).head(30)

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
                vol_ratio = vol_today / vol_yesterday if vol_yesterday > 0 else 1.0
                if today_close >= recent_3d_high * 0.99 and vol_ratio <= 3.0:
                    verified_codes.append(row['tf_code'])
                time.sleep(0.05)
        except: continue
    return candidates[candidates['tf_code'].isin(verified_codes)].head(CONFIG['TOP_N_DEFENSE'])

# ================= 5. 量比与筹码警告（仅生成警告，不修改量比） =================
def calculate_real_vol_ratio(candidate_df):
    real_vol_ratios = []
    chip_warnings = []
    for _, row in candidate_df.iterrows():
        vol_ratio = 1.0
        warning = ""
        try:
            df_k = tf.klines.get(row['tf_code'], period="1d", count=25, as_dataframe=True)
            if df_k is not None and len(df_k) >= 20:
                today_vol = pd.to_numeric(df_k.iloc[-1]['volume'], errors='coerce')
                past_5d_avg_vol = pd.to_numeric(df_k.iloc[-6:-1]['volume'], errors='coerce').mean()
                vol_ratio = today_vol / past_5d_avg_vol if past_5d_avg_vol > 0 else 1.0

                today_close = pd.to_numeric(df_k.iloc[-1].get('close', df_k.iloc[-1].get('last_price')), errors='coerce')
                ma20 = pd.to_numeric(df_k['close'].tail(20), errors='coerce').mean()
                if today_close < ma20:
                    warning += f"⚠️ 股价跌破MA20({ma20:.2f})；"

                # 筹码断层检测（只生成警告，不改变量比）
                chip_gap_found = False
                chip_gap_price = 0.0
                for i in range(-10, -1):
                    try:
                        d_close = pd.to_numeric(df_k.iloc[i].get('close', df_k.iloc[i].get('last_price')), errors='coerce')
                        d_pre_close = pd.to_numeric(df_k.iloc[i-1].get('close', df_k.iloc[i-1].get('last_price')), errors='coerce')
                        d_high = pd.to_numeric(df_k.iloc[i].get('high', df_k.iloc[i].get('high_price')), errors='coerce')
                        d_vol = pd.to_numeric(df_k.iloc[i]['volume'], errors='coerce')
                        if d_pre_close > 0:
                            d_pct = (d_close - d_pre_close) / d_pre_close * 100
                            if d_pct <= -5.0 and d_vol > past_5d_avg_vol * 1.5:
                                if today_close < d_high * 0.98:
                                    chip_gap_found = True
                                    chip_gap_price = d_high
                                    break
                    except: continue
                if chip_gap_found:
                    warning += f"⚠️ 筹码断层(压力{chip_gap_price:.2f})；"
        except:
            warning += "⚠️ 数据异常；"
        real_vol_ratios.append(vol_ratio)
        chip_warnings.append(warning.strip("；"))

    candidate_df['vol_ratio'] = real_vol_ratios
    candidate_df['chip_warning'] = chip_warnings
    return candidate_df

# ================= 6. 历史趋势快照 =================
def get_history_context(tf_client, tf_code):
    if not tf_client: return "【历史趋势缺失】"
    try:
        df_k = tf_client.klines.get(tf_code, period="1d", count=60, as_dataframe=True)
        if df_k is None or len(df_k) < 20: return "【历史数据不足】"
        df_k['close'] = pd.to_numeric(df_k['close'], errors='coerce')
        df_k['high'] = pd.to_numeric(df_k['high'], errors='coerce')
        df_k['low'] = pd.to_numeric(df_k['low'], errors='coerce')
        curr_close = df_k.iloc[-1]['close']
        high_60d = df_k['high'].max()
        low_60d = df_k['low'].min()
        position_pct = (curr_close - low_60d) / (high_60d - low_60d) * 100 if high_60d > low_60d else 50
        pos_desc = "高位" if position_pct > 80 else ("低位" if position_pct < 20 else "中位")
        ma5 = df_k['close'].rolling(5).mean().iloc[-1]
        ma20 = df_k['close'].rolling(20).mean().iloc[-1]
        ma60 = df_k['close'].mean()
        trend = "多头排列" if ma5 > ma20 > ma60 else ("空头排列" if ma5 < ma20 < ma60 else "均线缠绕")
        avg_vol_20 = df_k['volume'].tail(20).mean()
        curr_vol = df_k.iloc[-1]['volume']
        vol_mult = curr_vol / avg_vol_20 if avg_vol_20 > 0 else 1.0
        return f"""
【60日趋势快照】
- 坐标: {pos_desc} (分位{position_pct:.0f}%)
- 均线: {trend} (MA5:{ma5:.2f}/MA20:{ma20:.2f}/MA60:{ma60:.2f})
- 边界: 压力{high_60d:.2f} | 支撑{low_60d:.2f}
- 量能: 今日是20日均量{vol_mult:.1f}倍
"""
    except: return "【历史数据异常】"

# ================= 7. Prompt 定义（精简、强制三档买点） =================
ANTI_HALLUCINATION_RULES = """
⚠️ 游资铁律（违反=严重亏损）：
1.【禁编数据】所有价格必须基于提供的真实数据计算并展示公式，精确到分。严禁编造历史/题材/财务数据。
2.【散户视角】资金<50万，追求确定性。直接给结论：买/不买？什么价买？什么价割？拒绝模棱两可。
3.【T+1条件策略】基于T日收盘，制定次日计划。必须分3档：高开>2% / 平开±1% / 低开>2%，每档给买点和仓位。
4.【时间止损】买入后30分钟不突破成本价2%→平仓；全天震荡(±1%)→14:45前清仓。
5.【冲高防御】次日冲高>4%后15分钟内回落>2%→止盈一半，剩余止损上移成本价。
"""

STRUCTURED_OUTPUT_SUFFIX = """
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⚠️⚠️⚠️ 【强制输出格式 - 违反即作废】⚠️⚠️⚠️

1. 正文每段3-6句，总字数800-1500字。禁止废话。
2. 严格按照system prompt中的段落标题输出，不得遗漏。
3. 最后评级段必须包含（一字不改）：
   - **评级**：S/A/B/C
   - **仓位**：X成
   - **信心**：X/10
   - **时间止损**：X分钟
   - **一句话**：（20字内）
4. 末尾单独一行JSON：{"rating":"S","buy_price":数字,"stop_price":数字,"position":"X成","confidence":数字,"time_stop":数字,"summary":"20字内"}
5. 在“T+1买点策略”段落中，必须按以下固定格式输出三档买点（不可省略）：
【三档买点】
- 高开2%以上：XX.XX 元
- 平开±1%：XX.XX 元
- 低开2%以上：XX.XX 元
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"""

PROMPT_NORMAL = f"""你是A股15年实战游资，精通"缩量洗盘反包"与"反量化埋伏"。
{ANTI_HALLUCINATION_RULES}
按以下格式输出：
### 1. 盘面语言 (结合历史快照+今日量价，看透主力意图)
### 2. 量化排雷 (流动性/筹码风险)
### 3. T+1买点策略 (分高开/平开/低开三档，展示计算)
### 4. 止损位 (价格止损+时间止损，展示计算)
### 5. 猎手评级
- **评级**：S/A/B/C
- **仓位**：X成
- **信心**：1-10分
- **时间止损**：X分钟不突破则离场
- **一句话**：20字内"""

PROMPT_DEMON = f"""你是A股15年实战游资，精通"龙头首阴反包"与"妖股情绪博弈"。
{ANTI_HALLUCINATION_RULES}
按以下格式输出：
### 1. 妖气指数 (连板高度/市场身位/盘口语言)
### 2. 死亡换手排雷 (筹码断层/流动性)
### 3. T+1买点策略 (高开>3%抢筹/平开半路/低开放弃，展示计算)
### 4. 止损位 (价格止损+时间止损，展示计算)
### 5. 猎手评级
- **评级**：S/A/B/C
- **仓位**：X成
- **信心**：1-10分
- **时间止损**：X分钟不突破则离场
- **一句话**：20字内"""

PROMPT_DEFENSE = f"""你是精通"弱市逆风突破"的A股猎手。大盘萎靡时寻找逆市真金。
{ANTI_HALLUCINATION_RULES}
按以下格式输出：
### 1. 逆风强度 (量价背离/突破有效性)
### 2. 筹码健康度 (量能/套牢盘)
### 3. T+1买点策略 (高开追/平开伏击/低开低吸，展示计算)
### 4. 止损位 (价格止损+时间止损，展示计算)
### 5. 逆风评级
- **评级**：S/A/B/C
- **仓位**：X成
- **信心**：1-10分
- **时间止损**：X分钟不突破则离场
- **一句话**：20字内"""

PROMPT_WATCHLIST = f"""你是冷酷的"账户急救操盘手"。客户自选股全部套牢，只讲残酷真相和操作纪律。
{ANTI_HALLUCINATION_RULES}
按以下格式输出：
### 1. 套牢诊断 (套牢深度/上方压力/趋势阶段)
### 2. 反弹动能 (量价结构/做T空间/破位信号)
### 3. 急救决断 (四选一，严禁模棱两可)： 🩸割肉 | 🛌装死 | 🔄做T | 💰补仓
### 4. 操作锚点 (做T买卖点/补仓位/清仓破位价，展示计算)
### 5. 猎手评级
- **评级**：S/A/B/C
- **仓位**：X成
- **信心**：1-10分
- **时间止损**：X分钟不突破则离场
- **一句话**：20字内"""

st.session_state.active_prompts = {
    "normal": PROMPT_NORMAL, "demon": PROMPT_DEMON,
    "defense": PROMPT_DEFENSE, "watchlist": PROMPT_WATCHLIST
}

def analyze_with_llm(stock_dict, minute_feature_text, market_context, history_context, mode="normal"):
    if not llm_client: return "无AI", "无AI"
    active_prompts = st.session_state.active_prompts
    system_p = active_prompts.get(mode, PROMPT_NORMAL)

    chip_warning = stock_dict.get('chip_warning', '')
    warning_info = f"\n【筹码/趋势警告】: {chip_warning}" if chip_warning else ""

    price_info = f"""
【真实价格锚点】
- 当前价: {stock_dict.get('close', '未知')} 元
- 今日最低: {stock_dict.get('low', '未知')} 元
- 今日最高: {stock_dict.get('high', '未知')} 元
- 昨日收盘: {stock_dict.get('pre_close', '0.0')} 元
"""
    user_prompt = f"""【大盘与情绪】:\n{market_context}
【历史趋势快照】:\n{history_context}
{price_info}
【股票】: {stock_dict.get('name')} ({stock_dict.get('code')}) | {stock_dict.get('board')}
【数据】: 涨幅 {stock_dict.get('pct_chg', 0):.2f}%, 量比 {stock_dict.get('vol_ratio', 0):.2f}, 成交额 {stock_dict.get('amount', 0)/100000000:.1f}亿, 换手 {stock_dict.get('turnover', 0):.2f}%
【分时】: {minute_feature_text}
{warning_info}
⚠️ 【交易计划】：我将于【明日（T+1日）】进行买入操作。请基于上述T日收盘数据，制定明日的集合竞价观察点及盘中条件买入策略。
{STRUCTURED_OUTPUT_SUFFIX}"""
    try:
        response = llm_client.chat.completions.create(
            model=CONFIG["LLM_MODEL"],
            messages=[{"role": "system", "content": system_p}, {"role": "user", "content": user_prompt}],
            max_tokens=32768
        )
        reasoning = getattr(response.choices[0].message, 'reasoning_content', '') or ''
        final = response.choices[0].message.content or ''
        return reasoning, final
    except Exception as e:
        return str(e), f"❌ AI 调用失败: {e}"

def get_minute_features(tf_client, tf_codes):
    features_map = {}
    for tf_code in tf_codes:
        try:
            df_k = tf_client.klines.get(tf_code, period="15m", count=16, as_dataframe=True)
            if df_k is None or df_k.empty: features_map[tf_code] = "【分时缺失】"; continue
            df_k['volume'] = pd.to_numeric(df_k['volume'], errors='coerce')
            total_vol = df_k['volume'].sum()
            tail_vol = df_k['volume'].tail(2).sum()
            tail_ratio = (tail_vol / total_vol * 100) if total_vol > 0 else 0
            features_map[tf_code] = f"尾盘30分量占比: {tail_ratio:.1f}%"
        except: features_map[tf_code] = "【分时异常】"
    return features_map

# ================= 8. 竞价确认清单生成 =================
def generate_auction_checklist(stock_dict, analysis_text):
    code = stock_dict['code']; name = stock_dict['name']
    conditions = []
    yesterday_vol = stock_dict.get('volume', 0)
    min_auction_vol = round(yesterday_vol * 0.03) if yesterday_vol > 0 else 0
    if min_auction_vol > 0:
        conditions.append(f"竞价成交量 ≥ {min_auction_vol}手")
    try:
        high_match = re.search(r'高开.*?(\d+\.\d+)', analysis_text)
        if high_match:
            high_price = float(high_match.group(1))
            conditions.append(f"高开幅度不超过 {high_price:.2f}元 (涨幅{((high_price/stock_dict['close'])-1)*100:.1f}%)")
    except: pass
    conditions.append(f"低开幅度不超过3% (低于{stock_dict['close']*0.97:.2f})，否则放弃买入")
    return {'code': code, 'name': name, 'conditions': conditions, 'active': True}

# ================= 9. 价格提取与评级 =================
def extract_price_from_text(final_text, close_price, price_type="buy"):
    json_match = re.search(r'\{[^{}]*"rating"[^{}]*\}', final_text)
    if json_match:
        try:
            data = json.loads(json_match.group(0))
            if price_type == "buy" and "buy_price" in data:
                val = float(data["buy_price"])
                if close_price * 0.7 <= val <= close_price * 1.3: return val
            if price_type == "stop" and "stop_price" in data:
                val = float(data["stop_price"])
                if close_price * 0.7 <= val <= close_price * 1.3: return val
        except: pass
    patterns = {
        "buy": [r'(?:买点|买入价|介入价)[：:\s]*(\d{1,3}\.\d{1,2})'],
        "stop": [r'(?:止损价|止损位)[：:\s]*(\d{1,3}\.\d{1,2})']
    }.get(price_type, [])
    for p in patterns:
        matches = re.findall(p, final_text)
        for m in matches:
            val = float(m)
            if close_price * 0.7 <= val <= close_price * 1.3: return val
    return 0.0

def extract_rating_from_text(final_text):
    json_match = re.search(r'\{[^{}]*"rating"[^{}]*\}', final_text)
    if json_match:
        try:
            rating = json.loads(json_match.group(0))["rating"].upper()
            if rating in ("S","A","B","C"): return rating
        except: pass
    section_match = re.search(r'评级.*?([SABC])', final_text)
    return section_match.group(1) if section_match else "未评级"

def save_today_predictions(normal_res, demon_res, defense_res, safe_dates):
    if not gc or not spreadsheet_url: return
    try:
        sh = gc.open_by_url(spreadsheet_url)
        worksheet = sh.worksheet(SHEET_NAME)
        existing = worksheet.get_all_values()
        if len(existing) > 1:
            if safe_dates['today'] in [r[0] for r in existing[1:]]:
                st.warning("今日已存在，跳过保存")
                return
    except: pass
    all_results = []
    for res_list, track_name in [(normal_res, "缩量潜伏"), (demon_res, "主板妖股"), (defense_res, "逆风突破")]:
        for item in res_list:
            row = item['row']; final_text = item['final']
            close_price = float(row['close'])
            buy_price = extract_price_from_text(final_text, close_price, "buy")
            stop_price = extract_price_from_text(final_text, close_price, "stop")
            rating = extract_rating_from_text(final_text)
            conditions = generate_auction_checklist(row, final_text)
            conditions_str = " | ".join(conditions['conditions'])
            all_results.append([
                safe_dates['today'], row['name'], row['code'], track_name,
                round(close_price,2), round(buy_price,2), round(stop_price,2), rating,
                final_text, None,None,None, conditions_str, "待验尸"
            ])
    if all_results:
        try:
            worksheet.append_rows(all_results, value_input_option='USER_ENTERED')
            st.success(f"已保存 {len(all_results)} 条")
        except Exception as e:
            st.error(f"保存失败: {e}")

# ================= 10. 智能验尸 =================
def ai_autopsy_record(analysis_text, t1_data_dict, stock_name, stock_code, mode):
    if not llm_client or not analysis_text: return None
    system_prompt = """你是冷静的A股短线交易复盘助手。
请阅读 AI 在 T 日的完整分析（包含买入计划、止损等），结合 T+1 日的实际行情数据，判断：
1. 该交易计划是否具备可执行性？（是否触及买入价？是否一字板/秒板/全天无机会？）
2. 如果可执行，实际盈亏如何？最高盈利多少？是否触及止损？
3. 策略逻辑本身是否正确？有无重大误判？
4. 最终给出一个简短结论（≤40字），并使用以下标签之一：
   - ✅策略有效 （预测符合，可获利）
   - ⚠️执行偏差 （触及买点但未按计划止盈/止损）
   - ❌策略错误 （买点逻辑本身错误，导致亏损）
   - ⛔不可执行 （未触及买点、一字板、流动性枯竭等）
5. 如果不可执行，必须说明具体原因。
请严格按照以下格式输出：
---
复盘结论：[标签] [简短总结]
详细分析：[2-3句核心复盘]
---
"""
    user_prompt = f"""股票：{stock_name}({stock_code}) 策略轨道：{mode}
T日 AI 分析全文：{analysis_text[:2000]}
T+1日实际行情：开盘 {t1_data_dict.get('open')}，最高 {t1_data_dict.get('high')}，最低 {t1_data_dict.get('low')}，收盘 {t1_data_dict.get('close')}，涨跌幅 {t1_data_dict.get('pct_chg','未知')}%，换手 {t1_data_dict.get('turnover','未知')}%
请输出复盘结果。"""
    try:
        response = llm_client.chat.completions.create(
            model=CONFIG["LLM_MODEL"],
            messages=[{"role":"system","content":system_prompt}, {"role":"user","content":user_prompt}],
            max_tokens=500
        )
        return response.choices[0].message.content
    except: return None

def check_buy_feasibility_simple(buy_price, t1_data):
    if buy_price <= 0: return False, "无有效买点"
    high, low, close, turnover, pre_close = t1_data['high'], t1_data['low'], t1_data['close'], t1_data['turnover'], t1_data['pre_close']
    if pre_close > 0:
        limit_up = round(pre_close * 1.1, 2)
        if abs(high - limit_up) < 0.01 and abs(low - limit_up) < 0.01 and abs(close - limit_up) < 0.01 and turnover < 0.5:
            return False, f"一字涨停(换手{turnover:.2f}%)"
    if buy_price < low - 0.01: return False, f"最低价{low:.2f} > 买点{buy_price:.2f}"
    if buy_price > high + 0.01: return False, f"最高价{high:.2f} < 买点{buy_price:.2f}"
    return True, "可成交"

def run_autopsy(safe_dates):
    if not gc or not spreadsheet_url: return
    try:
        sh = gc.open_by_url(spreadsheet_url)
        worksheet = sh.worksheet(SHEET_NAME)
        df_history = pd.DataFrame(worksheet.get_all_records())
        if df_history.empty: return
        if '验尸结果' not in df_history.columns: return
        pending = df_history[df_history['验尸结果'] == '待验尸'].copy()
        if pending.empty: return
        if '日期' in pending.columns:
            today_str = safe_dates['today']
            pending = pending[pending['日期'].astype(str) < today_str]
            if pending.empty:
                st.info("暂无历史记录可验尸"); return
        symbols_to_check = pending['代码'].unique().tolist()
        real_data = get_tickflow_data_for_symbols(tf, symbols_to_check)
        if real_data.empty: return
        updated_rows = worksheet.get_all_values()
        header = updated_rows[0]
        try:
            col_high = header.index('T+1日最高')+1
            col_low = header.index('T+1日最低')+1
            col_close = header.index('T+1日收盘')+1
            col_result = header.index('验尸结果')+1
        except: return
        update_count = 0
        for idx, row in pending.iterrows():
            code = str(row['代码']).strip()
            real_row = real_data[real_data['code'].astype(str).str.strip() == code]
            if real_row.empty: continue
            real = real_row.iloc[0]
            t1_high, t1_low, t1_close = real['high'], real['low'], real['close']
            t1_open = real.get('open', real['close'])
            try:
                ai_buy = float(row['AI建议买点'])
                ai_stop = float(row['AI建议止损'])
            except:
                ai_buy = 0.0; ai_stop = 0.0
            t1_dict = {'open':t1_open,'high':t1_high,'low':t1_low,'close':t1_close,
                       'pre_close':real.get('pre_close',0),'pct_chg':real.get('pct_chg',0),
                       'turnover':real.get('turnover',0),'vol_ratio':real.get('vol_ratio',1)}
            analysis_text = row.get('AI分析全文','')
            stock_name = row.get('名称',''); mode = row.get('策略赛道','')
            ai_result = ai_autopsy_record(analysis_text, t1_dict, stock_name, code, mode)
            if ai_result:
                final_result = f"🤖 AI复盘:\n{ai_result[:400]}"
            else:
                feasible, reason = check_buy_feasibility_simple(ai_buy, t1_dict)
                if not feasible:
                    final_result = f"⛔ 不符合：{reason}"
                elif t1_low <= ai_stop:
                    final_result = f"❌ 止损 (最低{t1_low:.2f})"
                elif t1_high >= ai_buy * 1.05:
                    final_result = f"🏆 止盈 (最高{t1_high:.2f})"
                elif t1_close > ai_buy:
                    final_result = f"✅ 浮盈 (收{t1_close:.2f})"
                else:
                    final_result = f"⚠️ 套牢 (收{t1_close:.2f})"
            sheet_row = idx + 2
            worksheet.update_cell(sheet_row, col_high, round(t1_high,2))
            worksheet.update_cell(sheet_row, col_low, round(t1_low,2))
            worksheet.update_cell(sheet_row, col_close, round(t1_close,2))
            worksheet.update_cell(sheet_row, col_result, final_result)
            update_count += 1
            time.sleep(0.15)
        st.success(f"验尸完成，更新 {update_count} 条")
    except Exception as e:
        st.warning(f"验尸异常: {e}")

# ================= 11. 导师进化（规则上限） =================
def generate_prompt_evolution(failed_cases_text, current_prompt_desc):
    if not llm_client: return "无AI", None
    mentor_system = """你是A股量化策略导师。请分析学生提供的失败案例，诊断AI在分析时的思维错误。
输出格式：
## 📊 错题诊断报告 （300字内）
## 🔧 进化补丁 （新规则列表，不超过5条，总规则数(含铁律)不超过8条；如有必要，合并或删除旧规则）"""
    try:
        resp = llm_client.chat.completions.create(
            model=CONFIG["LLM_MODEL"],
            messages=[{"role":"system","content":mentor_system},
                      {"role":"user","content":f"当前Prompt: {current_prompt_desc}\n失败案例:\n{failed_cases_text}"}],
            max_tokens=32768
        )
        content = resp.choices[0].message.content or ""
        parts = content.split("## 🔧 进化补丁")
        report = parts[0].replace("## 📊 错题诊断报告","").strip()
        patch = parts[1].strip() if len(parts)>1 else ""
        return report, patch
    except Exception as e:
        return f"❌ {e}", None

# ================= 12. HTML 报告导出（略，如需要可加） =================
def clean_display_text(t): return t

# ================= 13. 尾盘狙击（原样保留） =================
def tail_sniper_scan():
    if not tf: return pd.DataFrame()
    st.info("🎯 尾盘扫描...")
    try:
        realtime = tf.quotes.get(universes=["CN_Equity_A"], as_dataframe=True)
        if realtime is None or realtime.empty: return pd.DataFrame()
        realtime = realtime[~realtime['symbol'].str.contains('ST|退', na=False)]
        main_cond = (realtime['board']=='Main') & (realtime['pct_chg']>=2) & (realtime['pct_chg']<=7.5)
        gem_cond = (realtime['board']=='GEM') & (realtime['pct_chg']>=2) & (realtime['pct_chg']<=15)
        realtime = realtime[main_cond | gem_cond]
        realtime = realtime[realtime['amount'] > 1e8]
    except:
        return pd.DataFrame()

    candidates = []
    for _, row in realtime.iterrows():
        symbol = row['symbol']

        # 15分钟量比检查
        try:
            df_15m = tf.klines.get(symbol, period='15m', count=16, as_dataframe=True)
            if df_15m is None or len(df_15m)<2: continue
            vol_last = df_15m.iloc[-1]['volume']
            vol_prev = df_15m.iloc[-2]['volume']
            tail_vol_ratio = vol_last / vol_prev if vol_prev>0 else 0
            if tail_vol_ratio < 1.5: continue
        except:
            continue

        # 分时均价线检查
        try:
            df_1m = tf.klines.get(symbol, period='1m', count=240, as_dataframe=True)
            if df_1m is None or len(df_1m)<10: continue
            avg_price = df_1m['amount'].sum() / df_1m['volume'].sum() if df_1m['volume'].sum()>0 else 0
            if avg_price <= 0 or row['last_price'] < avg_price: continue
        except:
            continue

        # 五档盘口检查（含调试输出）
        try:
            depth = tf.depth.get(symbol)
            if not depth:          # 先判断 depth 是否存在
                continue

            # 🧪 调试输出（测试完请删除以下三行）
            st.write(f"🔍 {symbol} depth 类型: {type(depth)}")
            if hasattr(depth, '__dict__'):
                st.write("depth 属性:", depth.__dict__)

            # 尝试提取买卖盘总量
            bid_vol = 0
            ask_vol = 0
            if hasattr(depth, 'bids') and hasattr(depth, 'asks'):
                bid_vol = sum(b[1] for b in depth.bids)
                ask_vol = sum(a[1] for a in depth.asks)
            elif hasattr(depth, 'bid_volumes') and hasattr(depth, 'ask_volumes'):
                # 有些接口可能叫不同的名字
                bid_vol = sum(depth.bid_volumes)
                ask_vol = sum(depth.ask_volumes)
            else:
                # 若无法识别结构，输出错误并跳过该股
                st.warning(f"⚠️ {symbol} depth 结构未知，跳过")
                continue

            if bid_vol <= ask_vol * 1.2:
                continue
        except Exception as e:
            st.warning(f"五档获取失败 {symbol}: {e}")
            continue

        candidates.append({
            'symbol': symbol,
            'name': row.get('name', ''),
            'price': row['last_price'],
            'pct_chg': row['pct_chg'],
            'vol_ratio': tail_vol_ratio,
            'bid_vol': bid_vol,
            'ask_vol': ask_vol
        })
        time.sleep(0.1)

    return pd.DataFrame(candidates)

# ================= 14. Streamlit 主界面 =================
st.title("👑 四轨制猎手 V27.5 (精简版)")
safe_dates = get_safe_trade_dates()
st.caption(f"📅 基准日: {safe_dates['today']} | 昨: {safe_dates['yesterday']}")
run_autopsy(safe_dates)

with st.sidebar:
    st.header("⚙️ 参数")
    top_n_normal = st.slider("🛡️ 缩量轨 TOP N",1,20,CONFIG["TOP_N_NORMAL"])
    top_n_demon = st.slider("🐉 妖股轨 TOP N",1,10,CONFIG["TOP_N_DEMON"])
    st.divider()
    st.header("👁️ 自选股监控")
    watchlist_input = st.text_area("代码", value="600519,000858,300750", height=150)
    st.divider()
    st.header("🧬 AI策略进化")
    st.caption(f"当前状态: {st.session_state.current_active_prompt}")
    run_evolution = st.button("🔍 分析错题本", use_container_width=True)
    st.divider()
    st.header("🔥 尾盘狙击 (14:45)")
    now = datetime.now(tz_shanghai)
    # 🧪 强制测试（测试完记得改回）
    run_tail = st.button("🎯 运行尾盘狙击（测试模式）", type="primary")
    st.divider()
    run_market_scan = st.button("🚀 全市场四轨扫描", type="primary", use_container_width=True)
    run_watchlist = st.button("👁️ 自选股深度诊断", type="secondary", use_container_width=True)
    st.caption("💡 盘后务必查看「明日竞价确认表」，明早若条件不达标，请放弃买入！")

if run_evolution:
    # 进化逻辑（略，保留原逻辑，使用上面 generate_prompt_evolution）
    pass

if run_market_scan or run_watchlist:
    if not tf or not llm_client: st.error("客户端未初始化"); st.stop()
    CONFIG["TOP_N_NORMAL"] = top_n_normal
    CONFIG["TOP_N_DEMON"] = top_n_demon
    with st.spinner("获取日线数据..."):
        df, market_avg_pct = get_data_tickflow()
        if df is None: st.error("数据获取失败"); st.stop()
    market_context, market_ratio = get_market_context(tf, df)
    st.subheader("🌍 大盘环境")
    st.text(market_context)
    normal_results, demon_results, defense_results, watchlist_results = [], [], [], []

    if run_market_scan:
        normal_df = filter_normal_stocks(df)
        normal_df = financial_blacklist_filter(normal_df)
        if not normal_df.empty:
            normal_df = calculate_real_vol_ratio(normal_df)
            normal_df = normal_df[normal_df['vol_ratio'] <= 1.2].head(CONFIG['TOP_N_NORMAL'])
        demon_df = filter_demon_stocks(df)
        demon_df = financial_blacklist_filter(demon_df)
        if not demon_df.empty:
            demon_df = calculate_real_vol_ratio(demon_df)
            demon_df = demon_df.head(CONFIG['TOP_N_DEMON'])   # 妖股不按量比过滤
        defense_df = pd.DataFrame()
        if market_ratio < 1.0 or market_avg_pct < 0.0:
            defense_df = filter_defense_stocks(df, tf, market_avg_pct)
            defense_df = financial_blacklist_filter(defense_df)
            if not defense_df.empty:
                defense_df = calculate_real_vol_ratio(defense_df)
                defense_df = defense_df[defense_df['vol_ratio'] <= 2.5].head(CONFIG['TOP_N_DEFENSE'])
        all_codes = []
        if not normal_df.empty: all_codes.extend(normal_df['tf_code'].tolist())
        if not demon_df.empty: all_codes.extend(demon_df['tf_code'].tolist())
        if not defense_df.empty: all_codes.extend(defense_df['tf_code'].tolist())
        minute_features = get_minute_features(tf, list(set(all_codes)))
        total_tasks = len(normal_df) + len(demon_df) + len(defense_df)
        if total_tasks == 0: st.warning("无符合条件标的")
        else:
            progress_bar = st.progress(0)
            current = 0
            for _, row in normal_df.iterrows():
                current+=1; progress_bar.progress(current/total_tasks)
                history = get_history_context(tf, row['tf_code'])
                reasoning, final = analyze_with_llm(row.to_dict(), minute_features.get(row['tf_code'],''), market_context, history, "normal")
                normal_results.append({'row':row,'reasoning':reasoning,'final':final}); time.sleep(1)
            for _, row in demon_df.iterrows():
                current+=1; progress_bar.progress(current/total_tasks)
                history = get_history_context(tf, row['tf_code'])
                reasoning, final = analyze_with_llm(row.to_dict(), minute_features.get(row['tf_code'],''), market_context, history, "demon")
                demon_results.append({'row':row,'reasoning':reasoning,'final':final}); time.sleep(1)
            for _, row in defense_df.iterrows():
                current+=1; progress_bar.progress(current/total_tasks)
                history = get_history_context(tf, row['tf_code'])
                reasoning, final = analyze_with_llm(row.to_dict(), minute_features.get(row['tf_code'],''), market_context, history, "defense")
                defense_results.append({'row':row,'reasoning':reasoning,'final':final}); time.sleep(1)
            progress_bar.empty()
            # 竞价确认表
            if normal_results or demon_results or defense_results:
                st.subheader("📋 明日竞价入场确认表")
                all_stocks = normal_results + demon_results + defense_results
                for item in all_stocks:
                    row, final = item['row'], item['final']
                    cond = generate_auction_checklist(row, final)
                    with st.expander(f"🔍 {cond['name']} ({cond['code']}) 竞价条件"):
                        for c in cond['conditions']: st.write(f"- {c}")
                        st.caption("⚠️ 若任一条件不满足，放弃买入")
            save_today_predictions(normal_results, demon_results, defense_results, safe_dates)
        # 展示轨道结果
        st.subheader("🛡️ 轨道一：缩量潜伏池")
        for i, item in enumerate(normal_results,1):
            with st.expander(f"[{i}] {item['row']['name']} 涨幅{item['row']['pct_chg']:.1f}%"):
                st.markdown(item['final'])
        st.subheader("🐉 轨道二：妖股池")
        for i, item in enumerate(demon_results,1):
            with st.expander(f"[{i}] {item['row']['name']} 涨幅{item['row']['pct_chg']:.1f}%"):
                st.markdown(item['final'])
        st.subheader("🔥 轨道三：逆风突破池")
        for i, item in enumerate(defense_results,1):
            with st.expander(f"[{i}] {item['row']['name']} 涨幅{item['row']['pct_chg']:.1f}%"):
                st.markdown(item['final'])

    if run_watchlist:
        symbols = [s.strip() for s in re.split(r'[,\n\s]+', watchlist_input) if s.strip()]
        w_df = get_tickflow_data_for_symbols(tf, symbols)
        w_df = financial_blacklist_filter(w_df)
        if not w_df.empty:
            w_df = calculate_real_vol_ratio(w_df)
            for _, row in w_df.iterrows():
                history = get_history_context(tf, row['tf_code'])
                reasoning, final = analyze_with_llm(row.to_dict(), '', market_context, history, "watchlist")
                watchlist_results.append({'row':row,'reasoning':reasoning,'final':final})
                time.sleep(1)
            st.subheader("🚑 自选股诊断")
            for item in watchlist_results:
                with st.expander(f"{item['row']['name']}"):
                    st.markdown(item['final'])

if run_tail:
    tail_df = tail_sniper_scan()
    if not tail_df.empty:
        st.success(f"发现 {len(tail_df)} 只")
        for _, row in tail_df.iterrows():
            advice = analyze_tail_snipe(row.to_dict())
            st.write(f"**{row['name']}** - {advice}")
    else:
        st.info("无尾盘目标")

st.divider()
st.caption("💡 运行扫描后，下载按钮将出现（此版本简化了报告下载，如需恢复请自行添加）")
