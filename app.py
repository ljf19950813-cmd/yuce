# ================= 最先执行：导入所有依赖 =================
import logging
import time
import re
import json
import numpy as np
import pandas as pd
from openai import OpenAI
from datetime import datetime, timedelta
from io import BytesIO
import warnings
import httpx
import streamlit as st

st.set_page_config(page_title="V27.4 四轨猎魔 (尾盘狙击+财务排雷)", layout="wide")

# ================= 🕒 时区修复：强制锁定北京时间 =================
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

# ================= 0. 云端数据库初始化 (Google Sheets - gspread 直连版) =================
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
        logging.info("✅ Google Sheets (gspread) 连接成功！")
    else:
        st.warning("⚠️ 未在 Secrets 中找到 [gsheets] 配置，云端存储与验尸功能将被禁用。")
except Exception as e:
    st.error(f"❌ Google Sheets 连接失败: {e}")
    logging.error(f"gspread 初始化失败: {e}")

spreadsheet_url = st.secrets.get("SPREADSHEET_URL", "")

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

# ================= 2. 客户端安全初始化 (付费版) =================
tf = None
if TickFlow and TF_API_KEY:
    try:
        tf = TickFlow(api_key=TF_API_KEY, base_url="https://api.tickflow.org")
        logging.info("✅ TickFlow 付费版连接成功")
    except Exception as e:
        logging.error(f"TickFlow 客户端初始化失败: {e}")
        st.error(f"❌ TickFlow 初始化失败: {e}")

llm_client = None
if LLM_API_KEY and LLM_API_KEY != "YOUR_LLM_API_KEY":
    try:
        llm_client = OpenAI(
            api_key=LLM_API_KEY, 
            base_url=CONFIG["LLM_BASE_URL"],
            timeout=httpx.Timeout(180.0, connect=15.0)
        )
    except Exception as e:
        logging.error(f"LLM 客户端初始化失败: {e}")

# ================= Session State 初始化 (完整) =================
if "current_active_prompt" not in st.session_state:
    st.session_state.current_active_prompt = "当前使用默认四轨制 Prompt（未进化）"
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
    # 默认 Prompt，将在后面定义后重新赋值
    st.session_state.active_prompts = {
        "normal": "",
        "demon": "",
        "defense": "",
        "watchlist": ""
    }

# ================= 安全日期生成器 (保持不变) =================
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
        st.info(f"⏳ **防盘中失真保护已激活**：当前北京时间({now.strftime('%H:%M')})未过 15:30。基准日回退至上一完整交易日({t_day})。")
    return {
        "today": t_day, "yesterday": t_minus_1, "day_before": t_minus_2,
        "last_week": last_week, "now_str": now.strftime('%Y%m%d_%H%M'), "t_plus_1_label": "次日(T+1)"
    }

# ================= 财务数据与除权因子 =================
def financial_blacklist_filter(df):
    """
    根据财务指标过滤问题股。需包含 'tf_code' 列。
    使用 tf.financials.metrics 获取最新指标。
    """
    if df is None or df.empty or not tf:
        return df
    try:
        symbols = df['tf_code'].unique().tolist()
        fin = tf.financials.metrics(symbols, latest=True, as_dataframe=True)
        if fin is None or fin.empty:
            return df
        fin['code'] = fin['symbol'].str.split('.').str[0]
        merged = df.merge(
            fin[['code', 'eps_basic', 'bps', 'net_income_yoy', 'debt_to_asset_ratio']],
            on='code', how='left'
        )
        mask = (
            (merged['eps_basic'].isna() | (merged['eps_basic'] > 0)) &
            (merged['bps'].isna() | (merged['bps'] >= 1.0)) &
            (merged['net_income_yoy'].isna() | (merged['net_income_yoy'] > -50)) &
            (merged['debt_to_asset_ratio'].isna() | (merged['debt_to_asset_ratio'] < 80))
        )
        removed = merged[~mask]
        if not removed.empty:
            st.caption(f"🚮 财务排雷剔除 {len(removed)} 只: {removed['name'].tolist()}")
        return df.loc[merged[mask].index]
    except Exception as e:
        st.warning(f"财务过滤异常: {e}")
        return df

def adjust_kline(df_k):
    """若存在除权因子列，对K线进行前复权"""
    if df_k is None or df_k.empty or 'factor' not in df_k.columns:
        return df_k
    latest_factor = df_k['factor'].iloc[-1]
    if latest_factor <= 0:
        return df_k
    for col in ['open', 'high', 'low', 'close']:
        if col in df_k.columns:
            df_k[col] = df_k[col] * (df_k['factor'] / latest_factor)
    return df_k

# ================= 3. 数据获取与清洗 (原有) =================
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
            # 应用除权因子
            df_k = adjust_kline(df_k)
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
                'open': open_price, 'close': close_today, 'high': high, 'low': low,
                'pre_close': close_prev, 'pct_chg': pct, 'turnover': turnover,
                'amount': amount, 'vol_ratio': vol_ratio,
                'board': 'Main' if tf_code.endswith('.SH') or tf_code.startswith('00') else 'GEM',
                'industry': '自选股'
            })
            time.sleep(0.1)
        except Exception as e:
            logging.error(f"获取 {tf_code} 失败: {e}")
    return pd.DataFrame(valid_rows)

# ================= 4. 四轨制筛选器 (保持不变) =================
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
            df_k = adjust_kline(df_k)
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
    chip_gap_warnings = []
    for _, row in candidate_df.iterrows():
        try:
            df_k = tf.klines.get(row['tf_code'], period="1d", count=25, as_dataframe=True)
            df_k = adjust_kline(df_k)
            if df_k is not None and len(df_k) >= 20:
                today_vol = pd.to_numeric(df_k.iloc[-1]['volume'], errors='coerce')
                today_close = pd.to_numeric(df_k.iloc[-1].get('close', df_k.iloc[-1].get('last_price')), errors='coerce')
                today_high = pd.to_numeric(df_k.iloc[-1].get('high', df_k.iloc[-1].get('high_price')), errors='coerce')
                past_5d_avg_vol = pd.to_numeric(df_k.iloc[-6:-1]['volume'], errors='coerce').mean()
                vol_ratio = today_vol / past_5d_avg_vol if past_5d_avg_vol > 0 else 99.0
                ma20 = pd.to_numeric(df_k['close'].tail(20), errors='coerce').mean()
                if today_close < ma20:
                    vol_ratio = 99.0
                    chip_gap_warnings.append(f"⚠️ {row['name']} 跌破MA20({ma20:.2f})，空头趋势已强制过滤。")
                    real_vol_ratios.append(vol_ratio)
                    time.sleep(0.05)
                    continue
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
                                if today_high < d_high * 0.98:
                                    chip_gap_found = True
                                    chip_gap_price = d_high
                                    break
                    except: continue
                if chip_gap_found:
                    vol_ratio = 99.0
                    chip_gap_warnings.append(f"⚠️ {row['name']} 上方存在筹码断层 (大阴线高点 {chip_gap_price:.2f})，已强制过滤。")
                else:
                    chip_gap_warnings.append(f"✅ {row['name']} 上方筹码干净，无套牢盘压力。 (量比:{vol_ratio:.2f})")
            else:
                vol_ratio = 99.0
                chip_gap_warnings.append(f"⚠️ {row['name']} 历史数据不足，无法检测筹码断层。")
        except:
            vol_ratio = 99.0
            chip_gap_warnings.append(f"⚠️ {row['name']} 数据获取异常。")
        real_vol_ratios.append(vol_ratio)
        time.sleep(0.05)
    candidate_df['vol_ratio'] = real_vol_ratios
    for warning in chip_gap_warnings:
        if warning.startswith("⚠️"):
            st.caption(warning)
        else:
            st.caption(f"🟢 {warning}")
    return candidate_df

# ================= 5. 历史趋势快照 (不变) =================
def get_history_context(tf_client, tf_code):
    if not tf_client: return "【历史趋势数据缺失】"
    try:
        df_k = tf_client.klines.get(tf_code, period="1d", count=60, as_dataframe=True)
        df_k = adjust_kline(df_k)
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
        avg_vol_20 = df_k['volume'].tail(20).mean()
        curr_vol = df_k.iloc[-1]['volume']
        vol_mult = curr_vol / avg_vol_20 if avg_vol_20 > 0 else 1.0
        pct_changes = df_k['close'].pct_change() * 100
        limit_ups_60d = (pct_changes > 9.5).sum()
        limit_downs_60d = (pct_changes < -9.5).sum()
        res = f"""
【60日趋势快照】
- 坐标: {pos_desc} (分位{position_pct:.0f}%)
- 均线: {trend_desc} (MA5:{ma5:.2f}/MA20:{ma20:.2f}/MA60:{ma60:.2f})
- 边界: 压力{high_60d:.2f} | 支撑{low_60d:.2f}
- 量能: 今日是20日均量{vol_mult:.1f}倍
- 股性: 60日内{limit_ups_60d}涨停/{limit_downs_60d}跌停
"""
        return res
    except Exception as e:
        return f"【历史数据获取异常: {e}】"

# ================= 6. Prompt 定义 (完整) =================
ANTI_HALLUCINATION_RULES = """
⚠️ 游资铁律（违反=严重亏损）：
1.【禁编数据】所有价格必须基于我提供的真实数据计算并展示公式，精确到分。严禁编造历史/题材/财务数据。若最高=最低=现价，说明数据缺失，须基于昨收+涨幅反推区间。
2.【散户视角】我资金<50万，追求一击必杀的确定性。直接给结论：买/不买？什么价买？什么价割？拒绝端水。
3.【T+1条件策略】基于T日收盘复盘，制定次日买入计划。必须分3档：高开>2% / 平开±1% / 低开>2%，每档给不同买点和仓位。
4.【时间止损】买入后30分钟未突破成本价2%→立刻平仓；全天织布机震荡(±1%)→14:45前清仓。小资金时间成本=生命，严禁"再看看"。
5.【冲高防御】次日9:30-10:00冲高>4%后15分钟内回落>2%→立刻止盈一半，剩余仓位止损上移至成本价。
"""

STRUCTURED_OUTPUT_SUFFIX = """
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⚠️⚠️⚠️ 【强制输出格式 - 违反即作废】⚠️⚠️⚠️

你的回答必须严格遵守以下规则：
1. 正文分析部分：每段3-6句话，总字数800-1500字。信息密度高，每句话必须有数据支撑或逻辑推演，禁止废话套话。
2. 正文段落标题：严格按照 system prompt 中定义的段落标题输出，不得自行更改或遗漏。
3. 最后一段（评级段）中，必须包含以下5行（一字不改格式）：
   - **评级**：S/A/B/C（四选一）
   - **仓位**：X成
   - **信心**：X/10
   - **时间止损**：X分钟
   - **一句话**：（20字内）
4. 在回答的最末尾，必须单独一行输出一个JSON对象（用花括号包裹），格式如下：
   {"rating":"S或A或B或C","buy_price":数字,"stop_price":数字,"position":"X成","confidence":数字,"time_stop":数字,"summary":"20字内"}
   其中 buy_price 和 stop_price 必须是合理股价（与当前价偏差不超过±15%），严禁填入成交量、时间等非价格数字。
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"""

PROMPT_NORMAL = f"""你是A股15年实战游资，精通"缩量洗盘反包"与"反量化埋伏"。
{ANTI_HALLUCINATION_RULES}
按以下格式输出(每段3-6句，总字数800-1500字，信息密度高，禁止废话)：
### 1. 盘面语言 (结合历史快照+今日量价，看透主力意图)
### 2. 量化排雷 (流动性/筹码断层)
### 3. T+1买点策略 (分高开/平开/低开三档，展示计算过程)
### 4. 止损位 (价格止损+时间止损，展示计算过程)
### 5. 猎手评级
- **评级**：S/A/B/C
- **仓位**：X成
- **信心**：1-10分
- **时间止损**：X分钟不突破则离场
- **一句话**：20字内"""

PROMPT_DEMON = f"""你是A股15年实战游资，精通"龙头首阴反包"与"妖股情绪博弈"。
{ANTI_HALLUCINATION_RULES}
按以下格式输出(每段3-6句，总字数800-1500字，信息密度高，禁止废话)：
### 1. 妖气指数 (连板高度/市场身位/盘口语言)
### 2. 死亡换手排雷 (筹码断层/流动性)
### 3. T+1买点策略 (高开>3%抢筹/平开半路/低开放弃，展示计算)
### 4. 止损位 (价格止损+时间止损，展示计算过程)
### 5. 猎手评级
- **评级**：S/A/B/C
- **仓位**：X成
- **信心**：1-10分
- **时间止损**：X分钟不突破则离场
- **一句话**：20字内"""

PROMPT_DEFENSE = f"""你是精通"弱市逆风突破"的A股猎手。大盘萎靡时寻找逆市真金。
{ANTI_HALLUCINATION_RULES}
按以下格式输出(每段3-6句，总字数800-1500字，信息密度高，禁止废话)：
### 1. 逆风强度 (量价背离/突破有效性)
### 2. 筹码健康度 (量能/套牢盘)
### 3. T+1买点策略 (高开追/平开伏击/低开低吸，展示计算)
### 4. 止损位 (价格止损+时间止损，展示计算过程)
### 5. 逆风评级
- **评级**：S/A/B/C
- **仓位**：X成
- **信心**：1-10分
- **时间止损**：X分钟不突破则离场
- **一句话**：20字内"""

PROMPT_WATCHLIST = f"""你是冷酷的"账户急救操盘手"。客户自选股全部套牢，只讲残酷真相和操作纪律。
{ANTI_HALLUCINATION_RULES}
按以下格式输出(每段3-6句，总字数800-1500字，信息密度高，禁止废话)：
### 1. 套牢诊断 (套牢深度/上方压力/趋势阶段)
### 2. 反弹动能 (量价结构/做T空间/破位信号)
### 3. 急救决断 (四选一，严禁模棱两可)：
   🩸割肉 | 🛌装死 | 🔄做T | 💰补仓
### 4. 操作锚点 (做T买卖点/补仓位/清仓破位价，展示计算)
### 5. 猎手评级
- **评级**：S/A/B/C
- **仓位**：X成
- **信心**：1-10分
- **时间止损**：X分钟不突破则离场
- **一句话**：20字内"""

# 更新 session state 中的默认 Prompt
st.session_state.active_prompts = {
    "normal": PROMPT_NORMAL,
    "demon": PROMPT_DEMON,
    "defense": PROMPT_DEFENSE,
    "watchlist": PROMPT_WATCHLIST
}

def analyze_with_llm(stock_dict, minute_feature_text, market_context, history_context, mode="normal"):
    if not llm_client:
        return "⚠️ 未配置大模型", "⚠️ 无Key"
    news_context = "【请纯粹基于盘面量价与情绪进行推演】"

    active_prompts = st.session_state.get("active_prompts", {})
    if mode == "demon":
        system_p = active_prompts.get("demon", PROMPT_DEMON)
    elif mode == "defense":
        system_p = active_prompts.get("defense", PROMPT_DEFENSE)
    elif mode == "watchlist":
        system_p = active_prompts.get("watchlist", PROMPT_WATCHLIST)
    else:
        system_p = active_prompts.get("normal", PROMPT_NORMAL)

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
⚠️ 【交易计划】：我将于【明日（T+1日）】进行买入操作。请基于上述T日收盘数据，为我制定明日的集合竞价观察点及盘中条件买入策略。
{STRUCTURED_OUTPUT_SUFFIX}"""

    try:
        response = llm_client.chat.completions.create(
            model=CONFIG["LLM_MODEL"],
            messages=[
                {"role": "system", "content": system_p},
                {"role": "user", "content": user_prompt}
            ],
            max_tokens=32768
        )
        reasoning = getattr(response.choices[0].message, 'reasoning_content', '') or ''
        final = response.choices[0].message.content or ''
        return reasoning, final
    except Exception as e:
        return str(e), f"❌ AI 调用失败: {e}"

def generate_auction_checklist(stock_dict, analysis_text):
    """
    从 AI 分析文本中解析买点，生成竞价确认清单。
    返回字典：{ 'code': str, 'name': str, 'conditions': [str, ...], 'active': True }
    若无法解析，返回空列表。
    """
    code = stock_dict['code']
    name = stock_dict['name']
    conditions = []

    # 最低竞价量要求（默认昨日总量的3%）
    yesterday_vol = stock_dict.get('volume', 0)
    min_auction_vol = round(yesterday_vol * 0.03) if yesterday_vol > 0 else 0
    if min_auction_vol > 0:
        conditions.append(f"竞价成交量 ≥ {min_auction_vol}手")

    # 高开/低开限制：基于 AI 分析中的三档买点提取
    try:
        # 提取高开买点（如果有）
        high_open_match = re.search(r'高开[^:]*[：:]\s*(\d+\.\d+)', analysis_text)
        if high_open_match:
            high_price = float(high_open_match.group(1))
            conditions.append(f"高开幅度不能超过 {high_price:.2f}元 (即{((high_price/stock_dict['close'])-1)*100:.1f}%) 否则放弃追高")
    except:
        pass

    # 默认必加条件：不能大幅低开（>3%）
    conditions.append(f"低开幅度不能超过3% (低于{stock_dict['close']*0.97:.2f})，否则放弃所有买入计划")

    return {'code': code, 'name': name, 'conditions': conditions, 'active': True}

def get_minute_features(tf_client, tf_codes):
    features_map = {}
    for tf_code in tf_codes:
        try:
            df_k = tf_client.klines.get(tf_code, period="15m", count=16, as_dataframe=True)
            if df_k is None or df_k.empty: 
                features_map[tf_code] = "【分时缺失】"
                continue
            df_k['volume'] = pd.to_numeric(df_k['volume'], errors='coerce')
            df_k['close'] = pd.to_numeric(df_k.get('close', df_k.get('last_price')), errors='coerce')
            df_k['amount'] = pd.to_numeric(df_k.get('amount', 0), errors='coerce')
            total_vol = df_k['volume'].sum()
            tail_vol = df_k['volume'].tail(2).sum()
            tail_ratio = (tail_vol / total_vol * 100) if total_vol > 0 else 0
            total_amount = df_k['amount'].sum()
            avg_price_all_day = (total_amount / total_vol) if total_vol > 0 and total_amount > 0 else df_k['close'].mean()
            last_price = df_k['close'].iloc[-1]
            tail_avg_price = df_k['close'].tail(2).mean()
            logic_text = ""
            if tail_ratio > 20 and last_price > avg_price_all_day and tail_avg_price > avg_price_all_day:
                logic_text = " 🔥【尾盘主力抢筹+站稳均价线】(尾盘放量且价格高于全天均价，主力真金白银买入，次日溢价预期极高！)"
            elif tail_ratio > 20 and last_price < avg_price_all_day:
                logic_text = " 💀【尾盘主力抢跑】(尾盘放量但价格跳水，次日大概率低开，极度危险！)"
            elif tail_ratio > 20 and last_price > avg_price_all_day and tail_avg_price <= avg_price_all_day:
                logic_text = " ⚠️【尾盘偷袭拉尾盘】(虽然尾盘放量，但均价线未跟上，可能是主力做收盘价，次日谨防低开)"
            elif tail_ratio < 10:
                logic_text = " ⚠️【尾盘平庸/无量】(尾盘无资金关注)"
            else:
                logic_text = " (尾盘表现正常)"
            features_map[tf_code] = f"尾盘30分量占比: {tail_ratio:.1f}%{logic_text}"
            time.sleep(0.05)
        except Exception as e:
            features_map[tf_code] = f"【分时异常: {e}】"
    return features_map

# ================= 7. HTML 报告导出模块 (完整) =================
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
    html_parts.append(f"<div class='header'><h1>👑 四轨制猎手实战报告 (V27.4)</h1><p>生成时间: {safe_dates['now_str']} | 基准日(T日): {safe_dates['today']}</p></div>")
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

# ================= 价格提取、评级、保存 (完整) =================
def extract_price_from_text(final_text, close_price, price_type="buy"):
    json_match = re.search(r'\{[^{}]*"rating"[^{}]*\}', final_text)
    if json_match:
        try:
            data = json.loads(json_match.group(0))
            if price_type == "buy" and "buy_price" in data:
                val = float(data["buy_price"])
                if close_price * 0.7 <= val <= close_price * 1.3: return val
            elif price_type == "stop" and "stop_price" in data:
                val = float(data["stop_price"])
                if close_price * 0.7 <= val <= close_price * 1.3: return val
        except: pass
    if price_type == "buy":
        patterns = [r'(?:买点|买入价|介入价|介入点)[：:\s]*(\d{1,3}\.\d{1,2})', r'(?:买点|买入|介入).*?(\d{1,3}\.\d{1,2})\s*元']
    else:
        patterns = [r'(?:止损价|止损位|止损)[：:\s]*(\d{1,3}\.\d{1,2})', r'(?:止损|割肉|离场|跌破).*?(\d{1,3}\.\d{1,2})\s*元']
    for pattern in patterns:
        matches = re.findall(pattern, final_text)
        for m in matches:
            val = float(m)
            if close_price * 0.7 <= val <= close_price * 1.3: return val
    all_prices = re.findall(r'(\d{1,3}\.\d{2})\s*元', final_text)
    valid_prices = [float(p) for p in all_prices if close_price * 0.8 <= float(p) <= close_price * 1.2]
    if valid_prices:
        if price_type == "buy":
            above = [p for p in valid_prices if p >= close_price * 0.98]
            return min(above, key=lambda x: abs(x - close_price)) if above else valid_prices[0]
        else:
            below = [p for p in valid_prices if p < close_price]
            return max(below) if below else valid_prices[0]
    return 0.0

def extract_rating_from_text(final_text):
    json_match = re.search(r'\{[^{}]*"rating"[^{}]*\}', final_text)
    if json_match:
        try:
            data = json.loads(json_match.group(0))
            rating = str(data.get("rating", "")).upper().strip()
            if rating in ("S", "A", "B", "C"): return rating
        except: pass
    section5_match = re.search(r'###\s*5[.、]?\s*.*?(?:评级|综合评级|逆风评级)[：:\s]*\*{0,2}([SABC])\*{0,2}', final_text, re.IGNORECASE | re.DOTALL)
    if section5_match: return section5_match.group(1).upper()
    rating_match = re.search(r'\*\*评级\*\*[：:\s]*([SABC])', final_text, re.IGNORECASE)
    if rating_match: return rating_match.group(1).upper()
    return "未评级"

def save_today_predictions(normal_res, demon_res, defense_res, safe_dates):
    if not gc or not spreadsheet_url: return
    try:
        sh = gc.open_by_url(spreadsheet_url)
        worksheet = sh.worksheet(SHEET_NAME)
        existing_data = worksheet.get_all_values()
        if len(existing_data) > 1:
            existing_dates = [row[0] for row in existing_data[1:]]
            if safe_dates['today'] in existing_dates:
                st.warning(f"⚠️ **防重复拦截**：{safe_dates['today']} 已存在，跳过写入。")
                return
    except Exception as e:
        logging.error(f"检查重复数据失败: {e}")

    all_results = []
    for res_list, track_name in [(normal_res, "缩量潜伏"), (demon_res, "主板妖股"), (defense_res, "逆风突破")]:
        for item in res_list:
            row = item['row']
            final_text = item['final']
            close_price = float(row['close'])
            buy_price = extract_price_from_text(final_text, close_price, "buy")
            stop_price = extract_price_from_text(final_text, close_price, "stop")
            rating = extract_rating_from_text(final_text)
            all_results.append([
                safe_dates['today'],
                row['name'],
                row['code'],
                track_name,
                round(close_price, 2),
                round(buy_price, 2),
                round(stop_price, 2),
                rating,
                final_text,
                None, None, None,
                conditions_str,   # 新增：竞价条件
                "待验尸"
            ])
    if all_results:
        try:
            worksheet.append_rows(all_results, value_input_option='USER_ENTERED')
            st.success(f"✅ 已将今日 {len(all_results)} 条 AI 策略存入云端！")
        except Exception as e:
            st.error(f"❌ 存入 Google Sheets 失败: {e}")
    else:
        st.warning("⚠️ 今日没有生成任何有效结果，跳过保存。")

# ================= 智能验尸 (完整) =================
def ai_autopsy_record(analysis_text, t1_data_dict, stock_name, stock_code, mode):
    if not llm_client or not analysis_text: return None
    system_prompt = """你是冷静的A股短线交易复盘助手。
请阅读 AI 在 T 日的完整分析（包含买入计划、止损等），结合 T+1 日的实际行情数据，判断：
1. 该交易计划是否具备可执行性？（是否触及买入价？是否一字板/秒板/全天无机会？）
2. 如果可执行，实际盈亏如何？最高盈利多少？是否触及止损？
3. 策略逻辑本身是否正确？有无重大误判？（如忽视大盘风险、误判趋势、止损设置不合理等）
4. 最终给出一个简短结论（≤40字），并使用以下标签之一：
   - ✅策略有效 （预测符合，可获利）
   - ⚠️执行偏差 （触及买点但未按计划止盈/止损）
   - ❌策略错误 （买点逻辑本身错误，导致亏损）
   - ⛔不可执行 （未触及买点、一字板、流动性枯竭等）
5. 如果不可执行，必须说明具体原因。

请严格按照以下格式输出（不要添加多余说明）：
---
复盘结论：[标签] [简短总结]
详细分析：[2-3句核心复盘]
---
"""
    user_prompt = f"""股票：{stock_name}({stock_code})
策略轨道：{mode}

T日 AI 分析全文：
{analysis_text[:2000]}

T+1日实际行情数据：
- 开盘价：{t1_data_dict.get('open')}
- 最高价：{t1_data_dict.get('high')}
- 最低价：{t1_data_dict.get('low')}
- 收盘价：{t1_data_dict.get('close')}
- 涨跌幅：{t1_data_dict.get('pct_chg', '未知')}%
- 换手率：{t1_data_dict.get('turnover', '未知')}%
- 量比：{t1_data_dict.get('vol_ratio', '未知')}
- 前收盘（T日收盘）：{t1_data_dict.get('pre_close')}

请严格按照要求输出复盘结果。"""
    try:
        response = llm_client.chat.completions.create(
            model=CONFIG["LLM_MODEL"],
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            max_tokens=500
        )
        content = response.choices[0].message.content or ""
        return content
    except Exception as e:
        logging.error(f"AI验尸调用失败 ({stock_code}): {e}")
        return None

def check_buy_feasibility_simple(buy_price, t1_data):
    if buy_price <= 0: return False, "AI未提供有效买点"
    high = t1_data['high']
    low = t1_data['low']
    close = t1_data['close']
    turnover = t1_data['turnover']
    pre_close = t1_data['pre_close']
    if pre_close > 0:
        limit_up = round(pre_close * 1.1, 2)
        if (abs(high - limit_up) < 0.01 and abs(low - limit_up) < 0.01
            and abs(close - limit_up) < 0.01 and turnover < 0.5):
            return False, f"全天一字涨停(换手{turnover:.2f}%)"
    if buy_price < low - 0.01:
        return False, f"最低价{low:.2f} > 买点{buy_price:.2f}"
    if buy_price > high + 0.01:
        return False, f"最高价{high:.2f} < 买点{buy_price:.2f}"
    return True, "可成交"

def run_autopsy(safe_dates):
    if not gc or not spreadsheet_url: return
    try:
        sh = gc.open_by_url(spreadsheet_url)
        worksheet = sh.worksheet(SHEET_NAME)
        df_history = pd.DataFrame(worksheet.get_all_records())
        if df_history.empty: return
        if '验尸结果' not in df_history.columns: return
        pending_rows = df_history[df_history['验尸结果'] == '待验尸'].copy()
        if pending_rows.empty: return
        if '日期' in pending_rows.columns:
            today_str = safe_dates['today']
            pending_rows = pending_rows[pending_rows['日期'].astype(str) < today_str]
            if pending_rows.empty:
                st.info("💡 所有待验尸记录均为今日生成，需等到下一个交易日才能验证，暂跳过验尸。")
                return
        st.info(f"🔍 检测到 {len(pending_rows)} 条历史 AI 策略，正在进行智能验尸...")
        symbols_to_check = pending_rows['代码'].unique().tolist()
        if not tf: return
        today_real_data = get_tickflow_data_for_symbols(tf, symbols_to_check)
        if today_real_data.empty:
            st.warning("⚠️ 无法获取T+1日行情数据，验尸跳过。")
            return
        updated_rows = worksheet.get_all_values()
        header = updated_rows[0]
        try:
            col_high = header.index('T+1日最高') + 1
            col_low = header.index('T+1日最低') + 1
            col_close = header.index('T+1日收盘') + 1
            col_result = header.index('验尸结果') + 1
        except ValueError:
            st.warning("⚠️ 表格表头缺失必要列，请检查。")
            return
        update_count = 0
        for idx, row in pending_rows.iterrows():
            code = str(row['代码']).strip()
            real_row = today_real_data[today_real_data['code'].astype(str).str.strip() == code]
            if real_row.empty: continue
            real = real_row.iloc[0]
            t1_high = real['high']
            t1_low = real['low']
            t1_close = real['close']
            t1_open = real.get('open', real['close'])
            raw_buy = row.get('AI建议买点', None)
            raw_stop = row.get('AI建议止损', None)
            try:
                ai_buy = float(raw_buy) if raw_buy and str(raw_buy).strip() not in ('nan','None') else 0.0
                ai_stop = float(raw_stop) if raw_stop and str(raw_stop).strip() not in ('nan','None') else 0.0
            except:
                ai_buy = 0.0; ai_stop = 0.0
            t1_dict = {
                'open': t1_open,
                'high': t1_high,
                'low': t1_low,
                'close': t1_close,
                'pre_close': real.get('pre_close', 0.0),
                'pct_chg': real.get('pct_chg', 0.0),
                'turnover': real.get('turnover', 0.0),
                'vol_ratio': real.get('vol_ratio', 1.0),
            }
            analysis_text = row.get('AI分析全文', '无分析内容')
            stock_name = row.get('名称', '')
            mode = row.get('策略赛道', '')
            ai_result = None
            if llm_client and analysis_text:
                ai_result = ai_autopsy_record(analysis_text, t1_dict, stock_name, code, mode)
            if ai_result:
                clean_ai = ai_result.strip()
                final_result = f"🤖 AI复盘:\n{clean_ai[:400]}"
            else:
                feasible, reason = check_buy_feasibility_simple(ai_buy, t1_dict)
                if not feasible:
                    final_result = f"⛔ 不符合买入条件：{reason}"
                else:
                    if t1_low <= ai_stop:
                        final_result = f"❌ 爆头止损 (最低{t1_low:.2f}破止损{ai_stop:.2f})"
                    elif t1_high >= ai_buy * 1.05:
                        final_result = f"🏆 大肉止盈 (最高{t1_high:.2f})"
                    elif t1_close > ai_buy:
                        final_result = f"✅ 浮盈收盘 (收{t1_close:.2f})"
                    else:
                        final_result = f"⚠️ 阴跌套牢 (收{t1_close:.2f})"
            sheet_row = idx + 2
            worksheet.update_cell(sheet_row, col_high, round(t1_high, 2))
            worksheet.update_cell(sheet_row, col_low, round(t1_low, 2))
            worksheet.update_cell(sheet_row, col_close, round(t1_close, 2))
            worksheet.update_cell(sheet_row, col_result, final_result)
            update_count += 1
            time.sleep(0.15)
        if update_count > 0:
            df_latest = pd.DataFrame(worksheet.get_all_records())
            completed = df_latest[df_latest['验尸结果'] != '待验尸']
            success_count = len(completed[completed['验尸结果'].str.contains('✅策略有效|🏆|大肉|浮盈', na=False)])
            total = max(len(completed), 1)
            win_rate = success_count / total * 100
            st.success(f"💀 智能验尸完毕！更新了 {update_count} 条记录。AI 历史策略有效率: **{win_rate:.1f}%** ({success_count}/{total})")
        else:
            st.warning("⚠️ 验尸未更新任何记录。")
    except Exception as e:
        st.warning(f"验尸过程出现异常 (不影响今日复盘): {e}")
        import traceback; logging.error(f"验尸异常详情: {traceback.format_exc()}")

# ================= 导师 AI 进化引擎 =================
def generate_prompt_evolution(failed_cases_text, current_prompt_desc):
    if not llm_client: return "⚠️ 未配置大模型", None
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
            messages=[{"role": "system", "content": mentor_system_prompt}, {"role": "user", "content": user_prompt}],
            max_tokens=32768
        )
        full_response = response.choices[0].message.content or ''
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

# ================= 尾盘狙击完整功能 =================
def tail_sniper_scan():
    if not tf: return pd.DataFrame()
    st.info("🎯 正在扫描尾盘异动...")
    try:
        realtime = tf.quotes.get(universes=["CN_Equity_A"], as_dataframe=True)
        if realtime is None or realtime.empty: return pd.DataFrame()
        realtime = realtime[~realtime['symbol'].str.contains('ST|退', na=False)]
        main_cond = (realtime['board'] == 'Main') & (realtime['pct_chg'] >= 2.0) & (realtime['pct_chg'] <= 7.5)
        gem_cond = (realtime['board'] == 'GEM') & (realtime['pct_chg'] >= 2.0) & (realtime['pct_chg'] <= 15.0)
        realtime = realtime[main_cond | gem_cond]
        realtime = realtime[realtime['amount'] > 1e8]
    except Exception as e:
        st.error(f"实时行情获取失败: {e}")
        return pd.DataFrame()
    candidates = []
    for _, row in realtime.iterrows():
        symbol = row['symbol']
        try:
            df_15m = tf.klines.get(symbol, period='15m', count=16, as_dataframe=True)
            if df_15m is None or len(df_15m) < 2: continue
            vol_last = df_15m.iloc[-1]['volume']
            vol_prev = df_15m.iloc[-2]['volume']
            tail_vol_ratio = vol_last / vol_prev if vol_prev > 0 else 0
            if tail_vol_ratio < 1.5: continue
        except: continue
        try:
            df_1m = tf.klines.get(symbol, period='1m', count=240, as_dataframe=True)
            if df_1m is None or len(df_1m) < 10: continue
            avg_price = df_1m['amount'].sum() / df_1m['volume'].sum() if df_1m['volume'].sum() > 0 else 0
            if avg_price <= 0 or row['last_price'] < avg_price: continue
        except: continue
        try:
            depth = tf.depth.get(symbol)
            if not depth: continue
            bid_vol = sum(b[1] for b in depth.bids) if hasattr(depth, 'bids') else 0
            ask_vol = sum(a[1] for a in depth.asks) if hasattr(depth, 'asks') else 0
            if bid_vol <= ask_vol * 1.2: continue
        except Exception as e:
            logging.warning(f"五档获取失败 {symbol}: {e}")
            continue
        candidates.append({
            'symbol': symbol,
            'name': row.get('name', ''),
            'price': row['last_price'],
            'pct_chg': row['pct_chg'],
            'vol_ratio': tail_vol_ratio,
            'bid_vol': bid_vol,
            'ask_vol': ask_vol,
            'reason': '尾盘放量抢筹+买盘强势'
        })
        time.sleep(0.1)
    return pd.DataFrame(candidates)

def analyze_tail_snipe(stock_dict):
    if not llm_client: return "无 AI 客户端"
    prompt = f"""尾盘狙击目标：
{stock_dict['name']} ({stock_dict['symbol']})
现价：{stock_dict['price']}，涨幅：{stock_dict['pct_chg']}%
尾盘放量比：{stock_dict['vol_ratio']}，买盘/卖盘：{stock_dict['bid_vol']}/{stock_dict['ask_vol']}
请立即给出操作建议（买入/观望/卖出），目标价和止损价（精确到分），50字内。"""
    try:
        resp = llm_client.chat.completions.create(
            model=CONFIG["LLM_MODEL"],
            messages=[{"role": "user", "content": prompt}],
            max_tokens=200
        )
        return resp.choices[0].message.content
    except: return "AI调用失败"

# ================= 8. Streamlit Web 主界面 =================
st.title("👑 四轨制猎手 V27.4 (尾盘狙击+财务排雷)")
safe_dates = get_safe_trade_dates()
st.caption(f"📅 当前基准交易日: {safe_dates['today']} | 上一交易日: {safe_dates['yesterday']}")

# 自动验尸
run_autopsy(safe_dates)

with st.sidebar:
    st.header("⚙️ 全市场扫描参数")
    top_n_normal = st.slider("🛡️ 缩量轨 TOP N", 1, 20, CONFIG["TOP_N_NORMAL"])
    top_n_demon = st.slider("🐉 妖股轨 TOP N", 1, 10, CONFIG["TOP_N_DEMON"])
    st.divider()
    st.header("👁️ 自选股监控")
    watchlist_input = st.text_area("输入代码 (每行一个或逗号分隔)", value="600519, 000858, 300750", height=150)
    st.divider()
    st.header("🧬 AI 策略进化中心")
    st.caption(f"当前状态: {st.session_state.current_active_prompt}")
    run_prompt_evolution = st.button("🔍 分析错题本并生成优化方案", use_container_width=True)
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
    run_market_scan = st.button("🚀 全市场四轨扫描", type="primary", use_container_width=True)
    run_watchlist = st.button("👁️ 自选股深度诊断", type="secondary", use_container_width=True)
    st.caption("💡 盘后务必查看「明日竞价确认表」，明早若条件不达标，请放弃买入！")

# 进化逻辑
if run_prompt_evolution:
    if not llm_client:
        st.error("❌ 未配置 LLM 客户端")
    else:
        with st.spinner("正在从 Sheet1 提取错题本..."):
            try:
                if not gc or not spreadsheet_url:
                    st.warning("⚠️ Google Sheets 未连接")
                    st.stop()
                sh = gc.open_by_url(spreadsheet_url)
                worksheet = sh.worksheet(SHEET_NAME)
                df_history = pd.DataFrame(worksheet.get_all_records())
                if df_history.empty:
                    st.warning("⚠️ 还没有历史数据")
                else:
                    failed_df = df_history[
                        df_history['验尸结果'].str.contains('❌策略错误|⚠️执行偏差|⛔不可执行|爆头|套牢', na=False)
                    ]
                    if failed_df.empty:
                        st.success("🎉 近期 AI 预测全部盈利，暂无需进化。")
                    else:
                        failed_text = ""
                        for _, row in failed_df.tail(8).iterrows():
                            failed_text += f"【复盘结论】{row['验尸结果']}\n"
                            raw_pred = str(row.get('AI分析全文', ''))[:300]
                            failed_text += f"原始预测摘要: {raw_pred}...\n\n"
                        st.info(f"📝 提取了 {len(failed_df.tail(8))} 个失败案例，正在调用导师 AI...")
                        report, new_patch = generate_prompt_evolution(
                            failed_text,
                            st.session_state.current_active_prompt
                        )
                        st.session_state.analysis_report = report
                        st.session_state.prompt_draft = new_patch
                        st.rerun()
            except Exception as e:
                st.error(f"❌ 读取错题本失败: {e}")

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
            new_patch = st.session_state.prompt_draft
            base_rules = st.session_state.base_anti_hallucination_rules
            evolved_rules = base_rules + "\n\n## 进化补丁 (来自错题分析)\n" + new_patch
            st.session_state.active_prompts["normal"] = f"""你是一位在A股摸爬滚打15年的顶尖游资，精通"缩量洗盘后的反包博弈"与"反量化盘中埋伏"。
{evolved_rules}
请务必严格按照以下格式输出：
### 1. 盘面语言解读 (结合【历史趋势快照】与今日量价，看透主力意图)
### 2. 流动性与量化排雷
### 3. 次日(T+1)竞价与买点策略 (必须分情况讨论：次日高开/平开/低开的应对买点，展示基于今日收盘价和昨收价的计算过程，精确到分)
### 4. 断臂求生止损位 (基于次日买入后的持仓成本，给出动态止损价计算过程，包含时间止损规则)
你必须以如下格式结束你的回答（不可省略）：
---
### 5. 猎手评级与仓位建议
- **综合评级**：【S/A/B/C 中选一个】
- **仓位建议**：【X成仓位】
- **信心指数**：【1-10分】
- **时间止损**：【买入后X分钟不突破则离场】
- **一句话总结**：【20字以内】
⚠️ 【强制输出要求】：你的回答必须包含以上全部5个段落标题，缺少任何一段都视为不合格！尤其不能遗漏"### 5."的评级结论！"""
            # 简化其他轨道 Prompt 更新，此处省略长文本，可类似处理或跳过
            st.session_state.current_active_prompt = f"已进化 (补丁应用时间: {datetime.now(tz_shanghai).strftime('%m-%d %H:%M')})"
            st.session_state.analysis_report = None
            st.session_state.prompt_draft = None
            st.rerun()

# 扫描与狙击逻辑
if run_market_scan or run_watchlist:
    if not tf or not llm_client: st.error("❌ 客户端初始化失败"); st.stop()
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
        # 轨道一
        st.info("🛡️ 【轨道一】筛选缩量洗盘猎物...")
        normal_df = filter_normal_stocks(df)
        normal_df = financial_blacklist_filter(normal_df)
        if not normal_df.empty:
            normal_df = calculate_real_vol_ratio(normal_df)
            normal_df = normal_df[normal_df['vol_ratio'] <= 1.2].head(CONFIG['TOP_N_NORMAL'])
        # 轨道二
        st.info("🐉 【轨道二】扫描主板妖股...")
        demon_df = filter_demon_stocks(df)
        demon_df = financial_blacklist_filter(demon_df)
        if not demon_df.empty:
            demon_df = calculate_real_vol_ratio(demon_df).head(CONFIG['TOP_N_DEMON'])
        # 轨道三
        defense_df = pd.DataFrame()
        if market_ratio < 1.0 or market_avg_pct < 0.0:
            st.warning("🔥 【轨道三】检测到市场偏弱/冰点，自动激活逆风突破池！")
            defense_df = filter_defense_stocks(df, tf, market_avg_pct)
            defense_df = financial_blacklist_filter(defense_df)
            if not defense_df.empty:
                defense_df = calculate_real_vol_ratio(defense_df)
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

        # 🆕 竞价确认表（与 if run_market_scan: 对齐，注意缩进是 8 个空格）
        if normal_results or demon_results or defense_results:
            st.subheader("📋 明日竞价入场确认表")
            all_stocks = normal_results + demon_results + defense_results
            conditions_data = []
            for item in all_stocks:
                row = item['row']
                final_text = item['final']
                cond = generate_auction_checklist(row, final_text)
                conditions_data.append(cond)
            
            for cond in conditions_data:
                with st.expander(f"🔍 {cond['name']} ({cond['code']}) 竞价条件"):
                    for c in cond['conditions']:
                        st.write(f"- {c}")
                    st.caption("⚠️ 若任一条件不满足，放弃买入计划")

        # 保存（同样与 if run_market_scan: 对齐）
        save_today_predictions(normal_results, demon_results, defense_results, safe_dates)
        st.subheader("🛡️ 轨道一：缩量潜伏池")
        if normal_results:
            for idx, item in enumerate(normal_results, 1):
                row, reasoning, final = item['row'], item['reasoning'], item['final']
                with st.expander(f"[{idx}] {row['name']} ({row['code']}) | 涨幅:{row['pct_chg']:.1f}% 换手:{row['turnover']:.1f}%"):
                    if reasoning: st.caption(f"🧠 脑内推演: {reasoning[:500]}...")
                    st.markdown(clean_display_text(final))
        else: st.warning("今日暂无符合轨道一条件的标的")
        st.subheader("🐉 轨道二：主板妖股池")
        if demon_results:
            for idx, item in enumerate(demon_results, 1):
                row, reasoning, final = item['row'], item['reasoning'], item['final']
                with st.expander(f"[{idx}] {row['name']} ({row['code']}) | 涨幅:{row['pct_chg']:.1f}% 换手:{row['turnover']:.1f}%"):
                    if reasoning: st.caption(f"🧠 脑内推演: {reasoning[:500]}...")
                    st.markdown(clean_display_text(final))
        else: st.warning("今日暂无符合轨道二条件的标的")
        st.subheader("🔥 轨道三：逆风突破池")
        if defense_results:
            for idx, item in enumerate(defense_results, 1):
                row, reasoning, final = item['row'], item['reasoning'], item['final']
                with st.expander(f"[{idx}] {row['name']} ({row['code']}) | 涨幅:{row['pct_chg']:.1f}% 换手:{row['turnover']:.1f}%"):
                    if reasoning: st.caption(f"🧠 脑内推演: {reasoning[:500]}...")
                    st.markdown(clean_display_text(final))
        else: st.info("今日大盘情绪强势，逆风池未激活 (或无符合条件标的)")
        st.divider()
        html_data = export_to_html_report(normal_results, demon_results, defense_results, [], market_context, safe_dates)
        if html_data:
            st.session_state.html_report_data = html_data
            st.session_state.html_report_filename = f"四轨制复盘_{safe_dates['now_str']}.html"
            st.info("✅ 报告已生成，请滑动到页面最底部点击下载按钮。")

    if run_watchlist:
        st.info("👁️ 【自选股】正在获取您的持仓数据...")
        watchlist_symbols = [s.strip() for s in re.split(r'[,\n\s]+', watchlist_input) if s.strip()]
        watchlist_df = get_tickflow_data_for_symbols(tf, watchlist_symbols)
        watchlist_df = financial_blacklist_filter(watchlist_df)
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
            st.subheader("🚑 自选股套牢急救诊断书")
            st.warning("⚠️ **急救原则**：截断亏损，让利润奔跑。不要在下跌趋势中盲目补仓接飞刀！")
            for idx, item in enumerate(watchlist_results, 1):
                row, reasoning, final = item['row'], item['reasoning'], item['final']
                pct_color = "red" if row['pct_chg'] < 0 else "green"
                with st.expander(f"🩸 [{idx}] {row['name']} ({row['code']}) | 当前价:{row['close']:.2f} | 今日涨幅: :{pct_color}[{row['pct_chg']:.1f}%]"):
                    if reasoning: st.caption(f"🧠 操盘手脑内推演: {reasoning[:500]}...")
                    st.markdown(clean_display_text(final))
            st.divider()
            html_data = export_to_html_report([], [], [], watchlist_results, market_context, safe_dates)
            if html_data:
                st.session_state.html_report_data = html_data
                st.session_state.html_report_filename = f"自选股诊断_{safe_dates['now_str']}.html"
                st.info("✅ 报告已生成，请滑动到页面最底部点击下载按钮。")
        else:
            st.warning("⚠️ 未获取到有效自选股数据，请检查代码输入是否正确")

if run_tail:
    tail_df = tail_sniper_scan()
    if not tail_df.empty:
        st.success(f"发现 {len(tail_df)} 只尾盘狙击目标")
        results = []
        for _, row in tail_df.iterrows():
            advice = analyze_tail_snipe(row.to_dict())
            results.append({"row": row, "advice": advice})
            st.write(f"**{row['name']}** ({row['symbol']}) - {advice}")
        if gc:
            try:
                sh = gc.open_by_url(spreadsheet_url)
                ws = sh.worksheet(SHEET_NAME)
                save_list = []
                for r in results:
                    row = r['row']
                    save_list.append([safe_dates['today'], row['name'], row['symbol'].split('.')[0],
                                    "尾盘狙击", row['price'], row['price'], row['price']*0.98,
                                    "B", r['advice'], None, None, None, "待验尸"])
                ws.append_rows(save_list, value_input_option='USER_ENTERED')
                st.success("✅ 尾盘狙击结果已保存")
            except Exception as e:
                st.error(f"保存失败: {e}")
    else:
        st.info("无符合条件标的")

# 下载按钮
st.divider()
if st.session_state.get("html_report_data"):
    st.download_button(
        label=f"💾 下载报告: {st.session_state.html_report_filename}",
        data=st.session_state.html_report_data,
        file_name=st.session_state.html_report_filename,
        mime="text/html",
        use_container_width=True
    )
else:
    st.caption("💡 运行全市场扫描或自选股诊断后，这里会出现 HTML 报告下载按钮。")
