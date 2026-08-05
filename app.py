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
    "TOP_N_DEMON": 5,
    "TOP_N_DEFENSE": 5,
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

def filter_recent_surge(df, days=5, max_pct=30):
    """
    剔除近 days 个交易日累计涨幅超过 max_pct% 的股票。
    需要 df 包含 'tf_code' 列，利用日K线计算。
    """
    if df is None or df.empty:
        return df
    keep = []
    for _, row in df.iterrows():
        try:
            k = tf.klines.get(row['tf_code'], period='1d', count=days+1, as_dataframe=True)
            if k is not None and len(k) >= days+1:
                # 计算近days日累计涨幅（不含当日）
                start_close = float(k.iloc[-(days+1)]['close'])
                end_close = float(k.iloc[-2]['close'])   # 昨日收盘
                if start_close > 0:
                    pct = (end_close - start_close) / start_close * 100
                    if pct > max_pct:
                        continue  # 剔除
            keep.append(row)
        except:
            keep.append(row)   # 数据异常时保留，避免误杀
    result = pd.DataFrame(keep)
    if len(result) < len(df):
        st.caption(f"🚫 近{days}日涨幅>{max_pct}%剔除 {len(df)-len(result)} 只")
    return result
    
# ================= 3. 数据获取与清洗 =================
def get_data_tickflow():
    if not tf: return None, 0.0
    try:
        logging.info("🚀 获取全市场 A 股日线快照...")
        df = tf.quotes.get(universes=["CN_Equity_A"], as_dataframe=True)
        if df is None or df.empty: return None, 0.0

        df['tf_code'] = df['symbol'].astype(str)
        df['code'] = df['tf_code'].str.split('.').str[0].str.zfill(6)
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

⚠️ 在输出第5段之前，你必须先精简输出一段「✅ 逻辑自检」，核对买点/止损/仓位/信心是否存在矛盾。格式：
✅ 逻辑自检
- 买点：XX.XX元，止损：YY.YY元
- 仓位：X成，信心：X/10
- 无矛盾 / 存在矛盾（需说明）

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

⚠️ 在输出第5段之前，你必须先精简输出一段「✅ 逻辑自检」，核对买点/止损/仓位/信心是否存在矛盾。格式：
✅ 逻辑自检
- 买点：XX.XX元，止损：YY.YY元
- 仓位：X成，信心：X/10
- 无矛盾 / 存在矛盾（需说明）

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

⚠️ 在输出第5段之前，你必须先精简输出一段「✅ 逻辑自检」，核对买点/止损/仓位/信心是否存在矛盾。格式：
✅ 逻辑自检
- 买点：XX.XX元，止损：YY.YY元
- 仓位：X成，信心：X/10
- 无矛盾 / 存在矛盾（需说明）

### 5. 逆风评级
- **评级**：S/A/B/C
- **仓位**：X成
- **信心**：1-10分
- **时间止损**：X分钟不突破则离场
- **一句话**：20字内"""

PROMPT_WATCHLIST = f"""你是冷酷的"账户急救操盘手"。客户自选股全部已买入并且被套，只讲残酷真相和操作纪律。
{ANTI_HALLUCINATION_RULES}
按以下格式输出：
### 1. 套牢诊断 (套牢深度/上方压力/趋势阶段)
### 2. 反弹动能 (量价结构/做T空间/破位信号)
### 3. 急救决断 (四选一，严禁模棱两可)： 🩸割肉 | 🛌装死 | 🔄做T | 💰补仓
### 4. 操作锚点 (做T买卖点/补仓位/清仓破位价，展示计算)

⚠️ 在输出第5段之前，你必须先精简输出一段「✅ 逻辑自检」，核对买点/止损/仓位/信心是否存在矛盾。格式：
✅ 逻辑自检
- 买点：XX.XX元，止损：YY.YY元
- 仓位：X成，信心：X/10
- 无矛盾 / 存在矛盾（需说明）

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
def generate_auction_checklist(stock_dict, analysis_text, track=""):
    code = stock_dict['code']
    name = stock_dict['name']
    close_price = stock_dict['close']
    conditions = []

    # 1. 竞价量能建议（软条件）
    yesterday_vol = stock_dict.get('volume', 0)
    min_auction_vol = round(yesterday_vol * 0.015) if yesterday_vol > 0 else 0
    if min_auction_vol > 0:
        conditions.append(f"⚠️ 建议竞价成交量 ≥ {min_auction_vol}手（低于此值谨慎追高）")

    # 2. 高开价格参考（从三档买点中提取）
    try:
        block_match = re.search(r'【三档买点】\s*\n(.*?)(?=\n\s*\n|\Z)', analysis_text, re.DOTALL)
        if block_match:
            block = block_match.group(1)
            high_line = re.search(r'高开.*?[：:]\s*(\d+\.\d+)', block)
            if high_line:
                high_price = float(high_line.group(1))
                if close_price * 0.98 <= high_price <= close_price * 1.15:
                    pct = (high_price / close_price - 1) * 100
                    conditions.append(
                        f"高开买入价 {high_price:.2f} 元（对应涨幅 ≥{pct:.1f}%），"
                        f"若开盘价超过此价位 2% 则放弃追高"
                    )
    except:
        pass

    # 3. 低开限制（根据策略动态调整）
    if '妖股' in track:
        low_limit_pct = 5.0
        desc = "妖股允许大幅低开，但需竞价量能配合"
    elif '逆风' in track:
        low_limit_pct = 3.0
        desc = "逆风环境低开3%以上放弃"
    else:  # 缩量潜伏及其他
        low_limit_pct = 2.5
        desc = "缩量潜伏低开2.5%以上放弃"

    low_limit = round(close_price * (1 - low_limit_pct/100), 2)
    conditions.append(f"❌ 低开幅度超过{low_limit_pct}% (低于{low_limit}元) → 放弃买入（{desc}）")

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

def validate_prediction(final_text, close_price):
    """硬校验：买点、止损是否在合理范围内"""
    buy = extract_price_from_text(final_text, close_price, "buy")
    stop = extract_price_from_text(final_text, close_price, "stop")
    if buy <= 0 or stop <= 0:
        return False
    if not (close_price * 0.85 <= buy <= close_price * 1.15):
        return False
    if not (close_price * 0.85 <= stop <= close_price * 1.15):
        return False
    if stop >= buy:  # 止损必须低于买点
        return False
    return True
    
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
            if not validate_prediction(final_text, close_price):
                st.caption(f"⚠️ {row['name']}({row['code']}) 买点/止损不合理，已跳过保存")
                continue

            all_results.append([
                safe_dates['today'],
                row['name'],
                "'" + str(row['code']),          # ← 加单引号
                track_name,
                round(close_price,2), round(buy_price,2), round(stop_price,2), rating,
                final_text, None,None,None, "待验尸", conditions_str
            ])
    if all_results:
        try:
            worksheet.append_rows(all_results, value_input_option='USER_ENTERED')
            st.success(f"已保存 {len(all_results)} 条")
        except Exception as e:
            st.error(f"保存失败: {e}")
            st.session_state.sheet1_refresh = True

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
        df_history = pd.DataFrame(st.session_state.get("sheet1_data", []))
        if df_history.empty: return
        # 检查必需列
        required_cols = ['验尸结果', '日期', '代码', 'AI建议买点']
        for col in required_cols:
            if col not in df_history.columns:
                st.warning(f"表格缺少列: {col}")
                return

        # 只验尸日期 ≤ T-2 的记录（确保至少过了两个交易日）
        pending = df_history[df_history['验尸结果'] == '待验尸'].copy()
        if pending.empty: return

        if '日期' in pending.columns:
            # 获取T-2日期
            t_minus_2 = safe_dates['day_before']  # 已经是T-2
            pending = pending[pending['日期'].astype(str) <= t_minus_2]
            if pending.empty:
                st.info("暂无T+2日可验尸的记录")
                return

        st.info(f"🔍 检测到 {len(pending)} 条可验尸记录（T+2日），开始模拟卖出...")

        # 需要获取T+1和T+2两天的行情
        symbols_to_check = pending['代码'].unique().tolist()
        if not tf: return

        # 获取T+1日行情（原有函数）
        t1_data = get_tickflow_data_for_symbols(tf, symbols_to_check)
        # 获取T+2日行情（新函数，下面定义）
        t2_data = get_tickflow_data_for_symbols_offset(tf, symbols_to_check, offset_days=2)

        if t1_data.empty and t2_data.empty:
            st.warning("无法获取T+1/T+2行情数据")
            return

        # 读取表头获取列号
        updated_rows = worksheet.get_all_values()
        header = updated_rows[0]
        try:
            col_high = header.index('T+1日最高') + 1
            col_low = header.index('T+1日最低') + 1
            col_close = header.index('T+1日收盘') + 1
            # 新增列（确保表格中已添加这些列）
            col_t2_open = header.index('T+2日开盘') + 1 if 'T+2日开盘' in header else None
            col_t2_avg = header.index('T+2日均价') + 1 if 'T+2日均价' in header else None
            col_sell_price = header.index('模拟卖出价') + 1 if '模拟卖出价' in header else None
            col_result = header.index('验尸结果') + 1
        except ValueError as e:
            st.warning(f"表头缺失关键列，请更新Sheet1表头。缺失: {e}")
            return

        update_count = 0
        for idx, row in pending.iterrows():
            code = str(row['代码']).strip().replace("'", "").replace(" ", "")
            # 获取T+1行情
            t1_row = t1_data[t1_data['code'].astype(str).str.strip() == code]
            if t1_row.empty: continue
            t1 = t1_row.iloc[0]
            t1_high, t1_low, t1_close = t1['high'], t1['low'], t1['close']

            # 获取T+2行情
            t2_row = t2_data[t2_data['code'].astype(str).str.strip() == code]
            if t2_row.empty: continue
            t2 = t2_row.iloc[0]
            t2_open = t2['open']
            t2_high = t2['high']
            t2_low = t2['low']
            t2_close = t2['close']
            # 计算T+2日均价（可用amount/volume或近似均价）
            t2_avg = (t2_high + t2_low + t2_close) / 3
            sell_price = t2_avg

            # 获取买入价
            try:
                ai_buy = float(row['AI建议买点'])
            except:
                ai_buy = 0.0
            if ai_buy <= 0: continue

            # 检查T+1日是否可成交（简单判断）
            if ai_buy < t1_low - 0.01 or ai_buy > t1_high + 0.01:
                final_result = "⛔ 不可执行：T+1日未触及买点"
            else:
                # 计算盈亏
                pct = (sell_price - ai_buy) / ai_buy * 100
                # 构造T+1和T+2数据字典给AI
                t1_dict = {'open': t1['open'], 'high': t1_high, 'low': t1_low, 'close': t1_close,
                           'pct_chg': t1.get('pct_chg', 0), 'turnover': t1.get('turnover', 0),
                           'vol_ratio': t1.get('vol_ratio', 1)}
                t2_dict = {'open': t2_open, 'high': t2_high, 'low': t2_low, 'close': t2_close,
                           'avg': t2_avg, 'sell_price': sell_price, 'pct': pct}
                analysis_text = row.get('AI分析全文', '')
                stock_name = row.get('名称', '')
                mode = row.get('策略赛道', '')

                ai_result = ai_autopsy_record_v2(analysis_text, t1_dict, t2_dict, stock_name, code, mode)
                if ai_result:
                    final_result = f"🤖 T+2验尸:\n{ai_result[:400]}"
                else:
                    # 规则兜底
                    if pct > 5:
                        final_result = f"🏆 大肉 +{pct:.1f}% (卖{sell_price:.2f})"
                    elif pct > 0:
                        final_result = f"✅ 盈利 +{pct:.1f}%"
                    elif pct > -3:
                        final_result = f"⚠️ 小亏 {pct:.1f}%"
                    else:
                        final_result = f"❌ 亏损 {pct:.1f}%"

            # 写回表格
            sheet_row = idx + 2
            worksheet.update_cell(sheet_row, col_high, round(t1_high, 2))
            worksheet.update_cell(sheet_row, col_low, round(t1_low, 2))
            worksheet.update_cell(sheet_row, col_close, round(t1_close, 2))
            if col_t2_open:
                worksheet.update_cell(sheet_row, col_t2_open, round(t2_open, 2))
            if col_t2_avg:
                worksheet.update_cell(sheet_row, col_t2_avg, round(t2_avg, 2))
            if col_sell_price:
                worksheet.update_cell(sheet_row, col_sell_price, round(sell_price, 2))
            worksheet.update_cell(sheet_row, col_result, final_result)
            update_count += 1
            time.sleep(0.15)

        if update_count > 0:
            try:
                # 获取最近20条验尸结果（非“待验尸”）
                df_latest = pd.DataFrame(worksheet.get_all_records())
                completed = df_latest[df_latest['验尸结果'] != '待验尸'].tail(20)
                if len(completed) >= 5:
                    win_count = len(completed[completed['验尸结果'].str.contains('🏆|盈利|✅策略有效', na=False)])
                    win_rate = round(win_count / len(completed) * 100, 1)
                    # 写入 Prompt_History
                    prompt_ws = sh.worksheet(PROMPT_HIST_SHEET) if PROMPT_HIST_SHEET in [ws.title for ws in sh.worksheets()] else sh.add_worksheet(title=PROMPT_HIST_SHEET, rows=100, cols=6)
                    prompt_data = prompt_ws.get_all_values()
                    if len(prompt_data) > 1:
                        last_row = len(prompt_data)
                        prompt_ws.update_cell(last_row, 5, win_rate)   # 胜率列
                        prompt_ws.update_cell(last_row, 6, len(completed))  # 交易笔数
            except Exception as e:
                logging.warning(f"更新版本胜率失败: {e}")
            st.success(f"💀 T+2验尸完成，更新 {update_count} 条")
        st.session_state.sheet1_refresh = True  # 下次需要重新读取
        else:
            st.warning("没有记录被更新，请检查T+2数据是否充足")
    except Exception as e:
        st.warning(f"验尸异常: {e}")

def get_tickflow_data_for_symbols_offset(tf_client, symbols_list, offset_days=2):
    """
    获取相对于最新交易日的偏移日K线。
    offset_days=1 表示T+1，2表示T+2。
    """
    if not tf_client: return pd.DataFrame()
    # 复用原有函数，但查询多天前数据。这里简单用 count 参数拿到多根K线再取倒数第二根？
    # 更稳健的方式：获取最近 offset_days+1 根日K，取倒数第 offset_days 根。
    # 直接在原有 get_tickflow_data_for_symbols 基础上修改，或者写一个新函数。
    # 为简洁，这里用原有函数获取最近3根K线，然后取特定偏移。
    parsed = []
    for s in symbols_list:
        s = str(s).strip()
        if '.' in s: parsed.append(f"{s.split('.')[1]}.{s.split('.')[0]}")
        else: parsed.append(f"{s}.SH" if s.startswith('6') else f"{s}.SZ")
    rows = []
    for tf_code in parsed:
        try:
            k = tf_client.klines.get(tf_code, period='1d', count=offset_days+2, as_dataframe=True)
            if k is None or len(k) < offset_days+1: continue
            # 取倒数第 offset_days 根（从0开始）
            target = k.iloc[-offset_days-1] if offset_days > 0 else k.iloc[-1]
            close = float(target.get('close', target.get('last_price')))
            open_p = float(target.get('open', target.get('open_price', close)))
            high = float(target.get('high', target.get('high_price', close)))
            low = float(target.get('low', target.get('low_price', close)))
            if close > 1000:
                close /= 100.0
                open_p /= 100.0
                high /= 100.0
                low /= 100.0
            amount = float(target.get('amount', 0))
            vol = float(target.get('volume', 0))
            rows.append({
                'code': tf_code.split('.')[0].zfill(6),
                'open': open_p, 'high': high, 'low': low, 'close': close,
                'amount': amount, 'volume': vol
            })
            time.sleep(0.05)
        except: continue
    return pd.DataFrame(rows)

# 修改AI验尸函数，加入T+2信息
def ai_autopsy_record_v2(analysis_text, t1_dict, t2_dict, stock_name, stock_code, mode):
    if not llm_client or not analysis_text: return None
    system_prompt = """你是A股超短线复盘教练。现在进行T+2日验尸，请阅读AI的T日分析、T+1日实际行情和T+2日模拟卖出情况，判断：
1. 交易计划是否具备可执行性？（触及买点？T+1日是否一字板？）
2. 如果执行，T+2日模拟卖出价（开盘价）盈利多少？
3. 策略逻辑是否正确？有无重大误判？
4. 给出结论标签：✅策略有效 / ⚠️执行偏差 / ❌策略错误 / ⛔不可执行
5. 输出格式：
---
复盘结论：[标签] [简短总结]
详细分析：[2-3句核心复盘]
---
"""
    user_prompt = f"""股票：{stock_name}({stock_code}) 策略：{mode}
T日分析：{analysis_text[:1500]}
T+1日行情：开{t1_dict['open']} 高{t1_dict['high']} 低{t1_dict['low']} 收{t1_dict['close']} 涨幅{t1_dict.get('pct_chg','?')}%
T+2日模拟卖出：开{t2_dict['open']} 均价{t2_dict.get('avg','?')} 卖出价{t2_dict.get('sell_price','?')} 盈亏{t2_dict.get('pct','?')}%
请输出复盘结论。"""
    try:
        resp = llm_client.chat.completions.create(
            model=CONFIG["LLM_MODEL"],
            messages=[{"role":"system","content":system_prompt}, {"role":"user","content":user_prompt}],
            max_tokens=1000
        )
        return resp.choices[0].message.content
    except: return None

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

# ================= 12. HTML 报告导出 =================
def clean_display_text(final_text):
    if not final_text: return final_text
    cleaned = re.sub(r'\n?\{[^{}]*"rating"[^{}]*\}\s*$', '', final_text)
    cleaned = re.sub(r'━{5,}.*?━{5,}', '', cleaned, flags=re.DOTALL)
    return cleaned.strip()
    
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
    if in_list: processed_lines.append('</ul>')
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
    html_parts.append(f"<div class='header'><h1>👑 四轨制猎手实战报告 (V27.5)</h1><p>生成时间: {safe_dates['now_str']} | 基准日(T日): {safe_dates['today']}</p></div>")
    html_parts.append("<h2>🌍 今日大盘与情绪环境</h2>")
    html_parts.append(f"<div class='market-box'>{market_context}</div>")
    
    def render_track(track_name, track_emoji, results, mode_type):
        if not results: return ""
        track_html = f"<div class='track-title'>{track_emoji} {track_name}</div>"
        for item in results:
            row, final = item['row'], item['final']
            pct_color = "#e74c3c" if row['pct_chg'] >= 0 else "#27ae60"
            analysis_html = robust_md_to_html(clean_display_text(final))
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

# ================= 13. 尾盘狙击（原样保留） =================
def tail_sniper_scan():
    if not tf: return pd.DataFrame()
    st.info("🎯 尾盘扫描...")

    # 1. 获取实时行情，快速过滤
    try:
        realtime = tf.quotes.get(universes=["CN_Equity_A"], as_dataframe=True)
        if realtime is None or realtime.empty:
            st.warning("未获取到实时行情")
            return pd.DataFrame()

        # 创建涨幅列（兼容字段名）
        if 'ext.change_pct' in realtime.columns:
            realtime['pct_chg'] = realtime['ext.change_pct'].copy()
            if realtime['pct_chg'].abs().max() < 1:
                realtime['pct_chg'] = realtime['pct_chg'] * 100
        else:
            st.error("缺少涨幅字段")
            return pd.DataFrame()

        # 创建 board 列
        if 'board' not in realtime.columns:
            realtime['board'] = realtime['symbol'].apply(
                lambda x: 'Main' if x.split('.')[0].startswith(('60','00')) 
                else ('GEM' if x.split('.')[0].startswith(('30','68')) else 'Other')
            )

        # 排除 ST
        realtime = realtime[~realtime['symbol'].str.contains('ST|退', na=False)]

        # 涨幅条件
        main_cond = (realtime['board']=='Main') & (realtime['pct_chg']>=2) & (realtime['pct_chg']<=7.5)
        gem_cond  = (realtime['board']=='GEM') & (realtime['pct_chg']>=2) & (realtime['pct_chg']<=15)
        realtime = realtime[main_cond | gem_cond]
        realtime = realtime[realtime['amount'] > 1e8]

        # 只保留成交额最大的前 50 只
        realtime = realtime.sort_values('amount', ascending=False).head(50)
    except Exception as e:
        st.error(f"实时行情过滤失败: {e}")
        return pd.DataFrame()

    if realtime.empty:
        st.info("无满足涨幅和成交额条件的股票")
        return pd.DataFrame()

    # 2. 逐一检查
    candidates = []
    total = len(realtime)
    progress_bar = st.progress(0)

    for idx, (_, row) in enumerate(realtime.iterrows()):
        symbol = row['symbol']

        # 15分钟量比
        try:
            df_15m = tf.klines.get(symbol, period='15m', count=16, as_dataframe=True)
            if df_15m is None or len(df_15m) < 2:
                continue
            vol_last = df_15m.iloc[-1]['volume']
            vol_prev = df_15m.iloc[-2]['volume']
            tail_vol_ratio = vol_last / vol_prev if vol_prev > 0 else 0
            if tail_vol_ratio < 1.5:
                continue
        except:
            continue


        # 分时均价线（实盘严格版）
        try:
            df_1m = tf.klines.get(symbol, period='1m', count=240, as_dataframe=True)
            if df_1m is None or len(df_1m) < 10:
                continue
            avg_price = df_1m['amount'].sum() / df_1m['volume'].sum() if df_1m['volume'].sum() > 0 else 0
            if avg_price <= 0 or row['last_price'] < avg_price:
                continue
        except:
            continue

        # 五档盘口
        try:
            depth = tf.depth.get(symbol)
            if not depth or not isinstance(depth, dict):
                continue
            bid_vol = sum(depth.get('bid_volumes', []))
            ask_vol = sum(depth.get('ask_volumes', []))
            if bid_vol <= ask_vol * 1.2:
                continue
        except:
            continue

        candidates.append({
            'symbol': symbol,
            'name': row.get('ext.name', row.get('name', '')),
            'price': row['last_price'],
            'pct_chg': row['pct_chg'],
            'vol_ratio': tail_vol_ratio,
            'bid_vol': bid_vol,
            'ask_vol': ask_vol
        })
        time.sleep(0.05)

        progress_bar.progress((idx + 1) / total)

    progress_bar.empty()

    return pd.DataFrame(candidates)

def analyze_tail_snipe(stock_dict):
    if not llm_client:
        return "无 AI 客户端"
    prompt = f"""尾盘狙击目标：
{stock_dict['name']} ({stock_dict['symbol']})
现价：{stock_dict['price']} 元，涨幅：{stock_dict['pct_chg']}%
尾盘放量比：{stock_dict['vol_ratio']:.2f}
买盘总量：{stock_dict['bid_vol']}手，卖盘总量：{stock_dict['ask_vol']}手
请立即给出操作建议（买入/观望/卖出），目标价和止损价（精确到分），50字内。"""
    for attempt in range(2):   # 尝试两次
        try:
            resp = llm_client.chat.completions.create(
                model=CONFIG["LLM_MODEL"],
                messages=[{"role": "user", "content": prompt}],
                max_tokens=200,
                timeout=15   # 设置超时避免长时间等待
            )
            content = resp.choices[0].message.content
            if content and content.strip():
                return content.strip()
        except:
            time.sleep(1)   # 等1秒再重试
    return "AI 暂时无建议，请人工判断"

def save_tail_snipe_results(results_list, safe_date):
    """将尾盘狙击结果写入独立的 Tail_Snipe 工作表"""
    if not gc or not spreadsheet_url or not results_list:
        return
    try:
        sh = gc.open_by_url(spreadsheet_url)
        try:
            ws = sh.worksheet("Tail_Snipe")
        except gspread.exceptions.WorksheetNotFound:
            ws = sh.add_worksheet(title="Tail_Snipe", rows=100, cols=8)
            ws.append_row(["日期", "名称", "代码", "现价", "AI建议买点", "AI建议止损", "AI分析全文", "竞价条件"])
        
        ws.append_rows(results_list, value_input_option='USER_ENTERED')
        st.success(f"✅ 已将 {len(results_list)} 条尾盘狙击结果存入 Tail_Snipe！")
    except Exception as e:
        st.error(f"❌ 尾盘保存失败: {e}")

# ================= 14. Streamlit 主界面 =================
st.title("👑 四轨制猎手 V27.5 (精简版)")
safe_dates = get_safe_trade_dates()
st.caption(f"📅 基准日: {safe_dates['today']} | 昨: {safe_dates['yesterday']}")
# 全局 Sheet1 缓存，避免重复读取
if gc and spreadsheet_url:
    try:
        if "sheet1_data" not in st.session_state or st.session_state.get("sheet1_refresh"):
            sh = gc.open_by_url(spreadsheet_url)
            ws = sh.worksheet(SHEET_NAME)
            st.session_state.sheet1_data = ws.get_all_records()
            st.session_state.sheet1_refresh = False
    except Exception as e:
        if "sheet1_data" not in st.session_state:
            st.session_state.sheet1_data = []
        st.warning(f"Sheet1 缓存加载失败: {e}")
run_autopsy(safe_dates)

# ================= 持仓管理函数（必须放在主界面之前） =================
def load_portfolio():
    """从 Portfolio 工作表读取持有中的股票（增强兼容性）"""
    if not gc or not spreadsheet_url: return pd.DataFrame()
    try:
        sh = gc.open_by_url(spreadsheet_url)
        try:
            ws = sh.worksheet("Portfolio")
        except gspread.exceptions.WorksheetNotFound:
            ws = sh.add_worksheet(title="Portfolio", rows=100, cols=10)
            ws.append_row(["日期", "代码", "名称", "策略赛道", "买入价", "持仓数量", "当前状态", "卖出价", "卖出日期", "AI审查结果"])
            return pd.DataFrame()

        data = ws.get_all_records()
        if not data:
            return pd.DataFrame()

        df = pd.DataFrame(data)

        # 自动识别状态列
        status_col = None
        if '当前状态' in df.columns:
            status_col = '当前状态'
        else:
            for col in df.columns:
                if '状态' in str(col):
                    status_col = col
                    break

        if status_col is None:
            # 没有状态列，返回全部数据（或空）
            st.warning("Portfolio 表缺少「当前状态」列，无法正确筛选持仓")
            return pd.DataFrame()

        # 筛选持有中的记录（忽略前后空格）
        mask = df[status_col].astype(str).str.strip() == '持有中'
        return df[mask].copy()
    except Exception as e:
        logging.error(f"加载持仓失败: {e}")
        st.warning(f"加载持仓异常: {e}")
        return pd.DataFrame()

def save_new_buy(stock, track, buy_price, quantity, date):
    """新增一条持有记录，quantity 为买入股数（整数）"""
    if not gc: return
    try:
        sh = gc.open_by_url(spreadsheet_url)
        ws = sh.worksheet("Portfolio")
        # 兼容中英文列名
        code = stock.get('code') or stock.get('代码', '')
        name = stock.get('name') or stock.get('名称', '')
        ws.append_row([
            date,
            code,
            name,
            track,
            buy_price,
            int(quantity),        # 确保整数
            "持有中",
            None, None, None
        ], value_input_option='USER_ENTERED')
    except Exception as e:
        st.error(f"保存买入失败: {e}")

def record_sell_and_review(stock_record, sell_price, date):
    """标记卖出，调用AI审查交易是否符合策略"""
    if not gc: return
    try:
        sh = gc.open_by_url(spreadsheet_url)
        ws = sh.worksheet("Portfolio")
        # 强制标准化代码为6位字符串（无论原始是数字还是字符串）
        raw_code = stock_record.get('代码') or stock_record.get('code', '')
        code = str(raw_code).replace("'", "").strip().zfill(6)
        cell = ws.find(code)   # 现在 code 肯定是字符串
        if cell:
            row = cell.row
            ws.update_cell(row, 8, sell_price)        # 卖出价
            ws.update_cell(row, 9, date)               # 卖出日期
            ws.update_cell(row, 7, "已卖出")
            # AI审查
            review = ai_trade_review(stock_record, sell_price)
            ws.update_cell(row, 10, review)
            st.info(f"卖出已记录，AI审查：{review}")
    except Exception as e:
        st.error(f"记录卖出失败: {e}")

def ai_trade_review(stock_record, sell_price):
    """让AI判断卖出是否符合超短线纪律"""
    if not llm_client: return "无AI"
    prompt = f"""你是超短线交易教练。请审查以下操作是否符合纪律：
股票：{stock_record['名称']}({stock_record['代码']})
策略：{stock_record['策略赛道']}
买入价：{stock_record['买入价']}，卖出价：{sell_price}
盈利：{(sell_price - float(stock_record['买入价']))/float(stock_record['买入价'])*100:.1f}%
请判断：是否触发止盈/止损条件？执行是否有偏差？给出改进建议（40字内）。"""
    try:
        resp = llm_client.chat.completions.create(
            model=CONFIG["LLM_MODEL"],
            messages=[{"role": "user", "content": prompt}],
            max_tokens=150
        )
        return resp.choices[0].message.content
    except: return "审查失败"

def analyze_holding(stock_record, tf_client):
    """对单只持仓股进行深度跟踪：复盘 + 后续操作建议 + 动态价格指导"""
    if not llm_client: return "AI未就绪"
    code = str(stock_record.get('代码') or stock_record.get('code', '')).replace("'", "").strip().zfill(6)
    name = stock_record.get('名称') or stock_record.get('name', '')
    try:
        buy_price = float(stock_record['买入价'])
    except:
        return "买入价数据错误"
    track = stock_record.get('策略赛道', '')

    # 纪律参数
    if '妖股' in track:
        stop_loss_pct, profit_target, time_stop = -5.0, 10.0, 15
    elif '逆风' in track:
        stop_loss_pct, profit_target, time_stop = -2.0, 5.0, 30
    else:
        stop_loss_pct, profit_target, time_stop = -3.0, 8.0, 30

    # 获取行情
    try:
        exchange = 'SH' if code.startswith('6') else 'SZ'
        tf_code = f"{code}.{exchange}"
        df_k = tf_client.klines.get(tf_code, period='1d', count=20, as_dataframe=True)
        if df_k is None or len(df_k) < 5:
            return f"行情数据不足 (仅{len(df_k) if df_k is not None else 0}根K线)"
        latest = df_k.iloc[-1]
        current_price = float(latest.get('close', latest.get('last_price')))
        if current_price <= 0:
            return "当前价异常"
        pct_chg = (current_price - buy_price) / buy_price * 100

        prev_close = float(df_k.iloc[-2].get('close', df_k.iloc[-2].get('last_price', current_price)))
        today_pct = (current_price - prev_close) / prev_close * 100 if prev_close > 0 else 0

        vol = float(latest.get('volume', 0))
        avg_vol_5 = df_k['volume'].tail(5).mean()
        vol_ratio = vol / avg_vol_5 if avg_vol_5 > 0 else 1.0
        ma5 = df_k['close'].tail(5).mean()
        ma20 = df_k['close'].tail(20).mean() if len(df_k) >= 20 else df_k['close'].mean()
        ma_status = "多头" if ma5 > ma20 else "空头"
    except Exception as e:
        return f"行情获取失败: {e}"

    prompt = f"""你是超短线交易教练，请复盘以下持仓并提供操作指导。

【持仓信息】
股票：{name}({code})
策略风格：{track}
买入价：{buy_price}，当前价：{current_price}，盈亏：{pct_chg:.1f}%
今日涨跌：{today_pct:.2f}%，量比：{vol_ratio:.2f}，均线：{ma_status}
该策略纪律：止损 {stop_loss_pct}%，止盈目标 {profit_target}%，时间止损 {time_stop} 分钟。

请按以下格式输出（严格三行，每行 ≤40字）：
复盘：[当初买入逻辑是否仍在？当前走势是否符合预期？可加入均线/量能判断]
操作：[持有/减仓/卖出/加仓]，并附 10 字内理由
价位：[若持有，给出新的目标价和止损价；若卖出，给出清仓价]"""

    for attempt in range(3):   # 最多尝试三次
        try:
            resp = llm_client.chat.completions.create(
                model=CONFIG["LLM_MODEL"],
                messages=[{"role": "user", "content": prompt}],
                max_tokens=1000,
                timeout=30   # 增加超时时间
            )
            content = resp.choices[0].message.content
            if content and content.strip():
                return content.strip()
            # 如果返回空内容，记录并重试
            logging.warning(f"持仓跟踪 {name}({code}) 第{attempt+1}次返回空内容，准备重试...")
            time.sleep(2)   # 等2秒再试
        except Exception as e:
            logging.error(f"持仓跟踪 {name}({code}) 第{attempt+1}次调用失败: {e}")
            if attempt < 2:
                time.sleep(3)
            else:
                return f"AI调用失败: {e}"

    # 三次都返回空
    return f"AI连续3次返回空内容，请检查模型状态或稍后重试。"
        
def auto_rollback_if_needed():
    if not gc: return
    try:
        sh = gc.open_by_url(spreadsheet_url)
        prompt_ws = sh.worksheet(PROMPT_HIST_SHEET)
        data = prompt_ws.get_all_values()
        if len(data) < 2: return
        last_row = data[-1]
        # 最近一笔的胜率和交易笔数
        win_rate = float(last_row[4]) if len(last_row) > 4 and last_row[4] else 100
        trades = int(last_row[5]) if len(last_row) > 5 and last_row[5] else 0
        if trades >= 10 and win_rate < 30:
            # 存在上一版本则回滚
            if len(data) >= 3:
                prev_row = data[-2]
                prev_prompt = prev_row[2] if len(prev_row) > 2 else None
                if prev_prompt:
                    st.session_state.active_prompts["normal"] = prev_prompt + " (已自动回滚)"
                    st.session_state.current_active_prompt = f"已回滚至 {prev_row[1]}"
                    st.warning(f"⚠️ 当前策略胜率过低 ({win_rate}%)，已自动回滚至上一版本。")
    except Exception as e:
        logging.warning(f"自动回滚检查失败: {e}")

auto_rollback_if_needed()

# ================= 侧边栏 =================
with st.sidebar:
    st.header("⚙️ 参数")
    top_n_normal = st.slider("🛡️ 缩量轨 TOP N", 1, 20, CONFIG["TOP_N_NORMAL"])
    top_n_demon = st.slider("🐉 妖股轨 TOP N", 1, 10, CONFIG["TOP_N_DEMON"])
    st.divider()
    st.header("👁️ 自选股监控")
    watchlist_input = st.text_area("代码", value="600519,000858,300750", height=150)
    st.divider()
    st.header("🧬 AI策略进化")
    # 显示当前版本胜率
    try:
        sh = gc.open_by_url(spreadsheet_url)
        prompt_ws = sh.worksheet(PROMPT_HIST_SHEET)
        data = prompt_ws.get_all_values()
        if len(data) > 1:
            last_row = data[-1]
            version = last_row[1] if len(last_row) > 1 else "?"
            winrate = last_row[4] if len(last_row) > 4 else "?"
            trades = last_row[5] if len(last_row) > 5 else "?"
            st.caption(f"版本 {version} | 近{trades}笔胜率 {winrate}%")
    except:
        pass
    st.caption(f"当前状态: {st.session_state.current_active_prompt}")
    run_evolution = st.button("🔍 分析错题本", use_container_width=True)
    st.divider()
    st.header("📊 持仓跟踪")
    run_tracking = st.button("🔍 实时跟踪分析", use_container_width=True)
    st.divider()
    st.header("🔥 尾盘狙击 (14:45)")
    now = datetime.now(tz_shanghai)
    if 1430 <= int(now.strftime('%H%M')) <= 1500:
        st.warning("🎯 尾盘狙击模式可用")
        run_tail = st.button("🎯 运行尾盘狙击", type="primary", use_container_width=True)
    else:
        run_tail = False
        st.caption("尾盘狙击仅在 14:30-15:00 可用")
    st.divider()

    # 全市场扫描按钮 → 启动流程
    if st.button("🚀 全市场四轨扫描", type="primary", use_container_width=True):
        st.session_state.scan_phase = "sell"      # 标记进入卖出确认阶段
        st.session_state.scan_top_n_normal = top_n_normal
        st.session_state.scan_top_n_demon = top_n_demon
        st.rerun()

    # 自选股诊断按钮保持独立，不触发流程
    run_watchlist = st.button("👁️ 自选股深度诊断", type="secondary", use_container_width=True)
    st.caption("💡 盘后务必查看「明日竞价确认表」，明早若条件不达标，请放弃买入！")

if st.button("修复历史股票代码"):
    # 修复 Sheet1
    sh = gc.open_by_url(spreadsheet_url)
    ws = sh.worksheet(SHEET_NAME)
    codes = ws.col_values(3)[1:]  # 假设代码在第三列
    for i, code in enumerate(codes, start=2):
        new_code = str(code).replace("'","").zfill(6)
        ws.update_cell(i, 3, f"'{new_code}")
    # 修复 Tail_Snipe
    ws2 = sh.worksheet("Tail_Snipe")
    codes2 = ws2.col_values(3)[1:]
    for i, code in enumerate(codes2, start=2):
        new_code = str(code).replace("'","").zfill(6)
        ws2.update_cell(i, 3, f"'{new_code}")
    st.success("历史代码已修复")
    
# ================= 自选股深度诊断（独立，不触发持仓流程） =================
if run_watchlist:
    if not tf or not llm_client: st.error("客户端未初始化"); st.stop()
    with st.spinner("获取日线数据..."):
        df, market_avg_pct = get_data_tickflow()
        if df is None: st.error("数据获取失败"); st.stop()
    market_context, market_ratio = get_market_context(tf, df)
    st.subheader("🌍 大盘环境")
    st.text(market_context)

    symbols = [s.strip() for s in re.split(r'[,\n\s]+', watchlist_input) if s.strip()]
    if not symbols:
        st.warning("请至少输入一个股票代码")
        st.stop()

    st.info(f"正在获取 {len(symbols)} 只自选股的基本数据...")
    w_df = get_tickflow_data_for_symbols(tf, symbols)
    if w_df.empty:
        st.warning("⚠️ 未获取到有效自选股数据，请检查代码是否正确")
        st.stop()

    st.info("正在进行财务排雷...")
    w_df = financial_blacklist_filter(w_df)
    if w_df.empty:
        st.warning("⚠️ 所有自选股均未通过财务排雷（每股净资产<1）")
        st.stop()

    st.info("正在计算量比与筹码警告...")
    w_df = calculate_real_vol_ratio(w_df)

    watchlist_results = []
    total = len(w_df)
    progress_bar = st.progress(0)
    status_text = st.empty()

    for idx, (_, row) in enumerate(w_df.iterrows()):
        code = row.get('code', '')
        name = row.get('name', '')
        status_text.text(f"正在分析 {name}({code}) ... ({idx+1}/{total})")
        try:
            history = get_history_context(tf, row['tf_code'])
            reasoning, final = analyze_with_llm(row.to_dict(), '', market_context, history, "watchlist")
            watchlist_results.append({'row': row, 'reasoning': reasoning, 'final': final})
        except Exception as e:
            st.warning(f"分析 {name}({code}) 失败: {e}")
            # 即使失败也添加一个空结果，以保持计数
            watchlist_results.append({'row': row, 'reasoning': '', 'final': f'分析失败: {e}'})
        progress_bar.progress((idx + 1) / total)
        time.sleep(0.5)   # 避免API限流

    progress_bar.empty()
    status_text.empty()

    if not watchlist_results:
        st.warning("⚠️ 所有自选股分析均失败，请稍后重试")
        st.stop()

    st.subheader("🚑 自选股诊断")
    for item in watchlist_results:
        with st.expander(f"{item['row']['name']} ({item['row']['code']})"):
            if item['final']:
                st.markdown(item['final'])
            else:
                st.info("该股分析无输出，请检查日志")

    # HTML报告
    st.divider()
    with st.spinner("正在生成诊断报告..."):
        html_data = export_to_html_report([], [], [], watchlist_results, market_context, safe_dates)
        if html_data:
            st.session_state.html_report_data = html_data
            st.session_state.html_report_filename = f"自选股诊断_{safe_dates['now_str']}.html"
            st.success("✅ 诊断报告已生成，可滑动至底部下载")
        else:
            st.warning("报告生成失败，但诊断结果仍可查看")

# ================= 全市场扫描流程（基于会话阶段） =================
scan_phase = st.session_state.get("scan_phase", None)

if scan_phase == "sell":
    # ----- 阶段1：持仓卖出确认 -----
    portfolio_df = load_portfolio()
    if portfolio_df is None or portfolio_df.empty:
        # 没有持仓，直接跳到买入阶段
        st.session_state.scan_phase = "buy"
        st.rerun()

    st.subheader("📌 持仓卖出确认")
    st.warning(f"您目前持有 {len(portfolio_df)} 只股票，请确认是否卖出：")
    with st.form(key="sell_form"):
        sell_records = []
        for _, holding in portfolio_df.iterrows():
            col1, col2 = st.columns([1, 3])
            sell = col1.checkbox(f"卖出 {holding['名称']}({holding['代码']})", key=f"sell_{holding['代码']}")
            price = col2.number_input("卖出价", value=float(holding['买入价']), step=0.01, key=f"sell_price_{holding['代码']}")
            if sell:
                sell_records.append((holding, price))
        if st.form_submit_button("确认卖出", key="submit_sell"):
            for holding, price in sell_records:
                record_sell_and_review(holding, price, safe_dates['today'])
            st.success("卖出记录已更新")
            st.rerun()

    # 提供“跳过卖出，直接下一步”的按钮，避免死循环
    if st.button("无持仓需卖出，进入买入确认"):
        st.session_state.scan_phase = "buy"
        st.rerun()

elif scan_phase == "buy":
    # ----- 阶段2：昨日推荐买入确认 -----
    st.subheader("📥 昨日推荐买入确认")
    try:
        df_hist = pd.DataFrame(st.session_state.get("sheet1_data", []))
        if df_hist.empty:
            st.info("暂无历史推荐记录")
            if st.button("直接进入扫描"):
                st.session_state.scan_phase = "scan"
                st.rerun()
        else:
            date_col = next((col for col in df_hist.columns if '日' in str(col)), df_hist.columns[0])
            yesterday_str = safe_dates['yesterday']
            mask = df_hist[date_col].astype(str).str.strip() == str(yesterday_str)
            yesterday_recs = df_hist[mask]
            if yesterday_recs.empty:
                st.info(f"昨日({yesterday_str})无推荐记录")
                if st.button("直接进入扫描"):
                    st.session_state.scan_phase = "scan"
                    st.rerun()
            else:
                with st.form(key="buy_form"):
                    buy_records = []
                    for _, rec in yesterday_recs.iterrows():
                        name = rec.get('名称', '未知')
                        code = rec.get('代码', '')
                        ai_buy = 0.0
                        try:
                            ai_buy = float(rec.get('AI建议买点', 0))
                        except:
                            pass
                        col1, col2, col3 = st.columns([1, 1.5, 1.5])
                        bought = col1.checkbox(f"已买入 {name}({code})", key=f"bought_{code}")
                        buy_price = col2.number_input("实际买入价", value=ai_buy, step=0.01, key=f"buy_price_{code}")
                        quantity = col3.number_input("买入股数", value=100, step=100, key=f"qty_{code}")
                        if bought:
                            buy_records.append((rec, buy_price, quantity))
                    if st.form_submit_button("确认买入", key="submit_buy"):
                        for rec, price, qty in buy_records:
                            save_new_buy(rec.to_dict(), rec.get('策略赛道', ''), price, qty, safe_dates['today'])
                        st.success("买入记录已保存")
                        st.session_state.scan_phase = "scan"
                        st.rerun()
                if st.button("跳过买入，直接开始扫描"):
                    st.session_state.scan_phase = "scan"
                    st.rerun()
    except Exception as e:
        st.caption(f"无法加载昨日推荐: {e}")

elif scan_phase == "scan":
    # ----- 阶段3：全市场扫描 -----
    if not tf or not llm_client:
        st.error("客户端未初始化")
        st.stop()

    # 应用侧边栏滑块的数值
    CONFIG["TOP_N_NORMAL"] = st.session_state.get("scan_top_n_normal", CONFIG["TOP_N_NORMAL"])
    CONFIG["TOP_N_DEMON"] = st.session_state.get("scan_top_n_demon", CONFIG["TOP_N_DEMON"])

    with st.spinner("获取日线数据..."):
        df, market_avg_pct = get_data_tickflow()
        if df is None: st.error("数据获取失败"); st.stop()
    market_context, market_ratio = get_market_context(tf, df)
    st.subheader("🌍 大盘环境")
    st.text(market_context)

    normal_results, demon_results, defense_results = [], [], []

    # 轨道一
    normal_df = filter_normal_stocks(df)
    normal_df = financial_blacklist_filter(normal_df)
    normal_df = filter_recent_surge(normal_df, days=5, max_pct=25)
    if not normal_df.empty:
        normal_df = calculate_real_vol_ratio(normal_df)
        normal_df = normal_df[normal_df['vol_ratio'] <= 1.2].head(CONFIG['TOP_N_NORMAL'])

    # 轨道二：妖股
    demon_df = filter_demon_stocks(df)
    demon_df = financial_blacklist_filter(demon_df)
    demon_df = filter_recent_surge(demon_df, days=3, max_pct=40)
    if not demon_df.empty:
        demon_df = calculate_real_vol_ratio(demon_df).head(CONFIG['TOP_N_DEMON'])

    # 轨道三：逆风突破
    defense_df = pd.DataFrame()
    if market_ratio < 1.0 or market_avg_pct < 0.0:
        defense_df = filter_defense_stocks(df, tf, market_avg_pct)
        defense_df = financial_blacklist_filter(defense_df)
        defense_df = filter_recent_surge(defense_df, days=5, max_pct=20)
        if not defense_df.empty:
            defense_df = calculate_real_vol_ratio(defense_df)
            defense_df = defense_df[defense_df['vol_ratio'] <= 2.5].head(CONFIG['TOP_N_DEFENSE'])

    all_codes = []
    if not normal_df.empty: all_codes.extend(normal_df['tf_code'].tolist())
    if not demon_df.empty: all_codes.extend(demon_df['tf_code'].tolist())
    if not defense_df.empty: all_codes.extend(defense_df['tf_code'].tolist())
    minute_features = get_minute_features(tf, list(set(all_codes)))
    total_tasks = len(normal_df) + len(demon_df) + len(defense_df)
    if total_tasks == 0:
        st.warning("无符合条件标的")
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
            # 为每个结果关联轨道名称
            all_stocks = []
            for item in normal_results:
                all_stocks.append({'row': item['row'], 'final': item['final'], 'track_name': '缩量潜伏'})
            for item in demon_results:
                all_stocks.append({'row': item['row'], 'final': item['final'], 'track_name': '主板妖股'})
            for item in defense_results:
                all_stocks.append({'row': item['row'], 'final': item['final'], 'track_name': '逆风突破'})
            
            for item in all_stocks:
                row, final, track_name = item['row'], item['final'], item['track_name']
                # 根据轨道名称确定 track 类型
                if '妖股' in track_name:
                    track = "妖股"
                elif '逆风' in track_name:
                    track = "逆风"
                else:
                    track = "缩量"
                cond = generate_auction_checklist(row, final, track)
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
    for idx, item in enumerate(demon_results, 1):
        row = item['row']
        with st.expander(f"[{idx}] {row['name']} 涨幅{row['pct_chg']:.1f}%"):
            if row.get('chip_warning'):
                st.caption(f"⚠️ 风险提示：{row['chip_warning']}")
            st.markdown(item['final'])
    st.subheader("🔥 轨道三：逆风突破池")
    for i, item in enumerate(defense_results,1):
        with st.expander(f"[{i}] {item['row']['name']} 涨幅{item['row']['pct_chg']:.1f}%"):
            st.markdown(item['final'])

    # 扫描完成，清除阶段状态，回到初始界面
    if st.button("返回主界面"):
        st.session_state.scan_phase = None
        st.rerun()

if run_tracking:
    portfolio_df = load_portfolio()
    if portfolio_df is None or portfolio_df.empty:
        st.info("当前无持仓")
    else:
        st.subheader("📊 持仓实时跟踪分析")
        for _, holding in portfolio_df.iterrows():
            # 强制标准化代码（无论原始格式如何，都补全为6位文本）
            raw_code = holding.get('代码') or holding.get('code')
            code = str(raw_code).replace("'", "").replace(" ", "").strip().zfill(6)
            name = holding.get('名称') or holding.get('name', '未知')
            buy_price = holding.get('买入价', '?')
            with st.expander(f"{name}({code}) | 成本{buy_price}"):
                # 传入标准化后的代码副本，避免影响原始数据
                holding_copy = holding.to_dict()
                holding_copy['代码'] = code
                result = analyze_holding(holding_copy, tf)
                st.write(result)
                
# ========== 尾盘狙击（独立功能） ==========
if run_tail:
    tail_df = tail_sniper_scan()
    if not tail_df.empty:
        st.success(f"发现 {len(tail_df)} 只")
        tail_save_data = []
        for _, row in tail_df.iterrows():
            advice = analyze_tail_snipe(row.to_dict())
            st.write(f"**{row['name']}** - {advice}")
            # 提取买点止损：可从 advice 中解析，暂时用现价作为买点，下浮2%作为止损
            buy_price = round(row['price'], 2)
            stop_price = round(row['price'] * 0.98, 2)
            tail_save_data.append([
                safe_dates['today'],
                row['name'],
                "'" + row['symbol'].split('.')[0],
                round(row['price'], 2),
                buy_price,
                stop_price,
                advice,
                ""   # 竞价条件留空
            ])
        save_tail_snipe_results(tail_save_data, safe_dates['today'])
    else:
        st.info("无尾盘目标")

# ================= 📥 全局下载按钮 =================
st.divider()
if st.session_state.get("html_report_data"):
    st.subheader("📥 下载报告")
    # 🔧 FIX-7: 删除 type="primary"（st.download_button 不支持该参数，会 TypeError）
    st.download_button(
        label=f"💾 点击下载: {st.session_state.html_report_filename}",
        data=st.session_state.html_report_data,
        file_name=st.session_state.html_report_filename,
        mime="text/html",
        use_container_width=True
    )
else:
    st.caption("💡 提示：运行全市场扫描或自选股诊断后，这里会出现 HTML 报告下载按钮。")
