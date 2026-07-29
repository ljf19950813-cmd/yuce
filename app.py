# ================= 最先执行：导入所有依赖 =================
import logging, time, re, numpy as np, pandas as pd, warnings, httpx, json
from openai import OpenAI
from datetime import datetime, timedelta
from io import BytesIO
import streamlit as st

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

# ================= 0. 云端数据库初始化 =================
import gspread
from oauth2client.service_account import ServiceAccountCredentials

gc = None
SHEET_NAME = "Sheet1"
try:
    if "gsheets" in st.secrets:
        creds_dict = dict(st.secrets["gsheets"])
        if "private_key" in creds_dict:
            creds_dict["private_key"] = creds_dict["private_key"].replace("\\n", "\n")
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        gc = gspread.authorize(creds)
except Exception as e:
    st.error(f"❌ Google Sheets 连接失败: {e}")

spreadsheet_url = st.secrets.get("SPREADSHEET_URL", "")

# ================= 1. 全局配置 =================
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s', datefmt='%H:%M:%S')
try:
    TF_API_KEY = st.secrets["TF_API_KEY"]
    LLM_API_KEY = st.secrets["LLM_API_KEY"]
except KeyError as e:
    st.error(f"❌ 缺少必要的密钥配置: {e}")
    st.stop()

CONFIG = {
    "TOP_N_NORMAL": 5, "TOP_N_DEMON": 3, "TOP_N_DEFENSE": 3,
    "TF_API_KEY": TF_API_KEY, "LLM_API_KEY": LLM_API_KEY,
    "LLM_BASE_URL": "https://api.deepseek.com/v1", "LLM_MODEL": "deepseek-chat"
}

# ================= 2. 客户端安全初始化 =================
tf = None
if TickFlow:
    try:
        tf = TickFlow.free() if CONFIG["TF_API_KEY"] == "YOUR_TICKFLOW_API_KEY" else TickFlow(api_key=CONFIG["TF_API_KEY"])
    except Exception as e:
        logging.error(f"TickFlow 客户端初始化失败: {e}")

llm_client = None
if CONFIG["LLM_API_KEY"] != "YOUR_LLM_API_KEY":
    try:
        llm_client = OpenAI(api_key=CONFIG["LLM_API_KEY"], base_url=CONFIG["LLM_BASE_URL"], timeout=httpx.Timeout(60.0, connect=15.0))
    except Exception as e:
        logging.error(f"LLM 客户端初始化失败: {e}")

# ================= Session State 初始化 =================
if "current_active_prompt" not in st.session_state: st.session_state.current_active_prompt = "默认四轨制 Prompt（未进化）"
if "analysis_report" not in st.session_state: st.session_state.analysis_report = None
if "prompt_draft" not in st.session_state: st.session_state.prompt_draft = None
if "html_report_data" not in st.session_state: st.session_state.html_report_data = None
if "html_report_filename" not in st.session_state: st.session_state.html_report_filename = ""
if "base_anti_hallucination_rules" not in st.session_state: st.session_state.base_anti_hallucination_rules = "【核心纪律】"
if "active_prompts" not in st.session_state: st.session_state.active_prompts = {"normal": "", "demon": "", "defense": "", "watchlist": ""}

# ================= 安全日期生成器 =================
def get_safe_trade_dates():
    holidays_2026 = {'20260101','20260102','20260103','20260104','20260214','20260215','20260216','20260217','20260218','20260219','20260220','20260221','20260222','20260223','20260228','20260404','20260405','20260406','20260501','20260502','20260503','20260504','20260505','20260509','20260619','20260620','20260621','20260925','20260926','20260927','20261001','20261002','20261003','20261004','20261005','20261006','20261007','20261010'}
    now = datetime.now(tz_shanghai)
    current_str = now.strftime('%Y%m%d')
    is_data_stable = int(now.strftime('%H%M')) >= 1530
    dates = []
    for i in range(20):
        d = now - timedelta(days=i)
        d_str = d.strftime('%Y%m%d')
        if d.weekday() < 5 and d_str not in holidays_2026: dates.append(d_str)
    if not is_data_stable and current_str in dates: dates.remove(current_str)
    t_day = dates[0] if dates else current_str
    if not is_data_stable: st.info(f"⏳ **防盘中失真保护**：当前时间未过15:30，基准日回退至 {t_day}。")
    return {"today": t_day, "yesterday": dates[1] if len(dates)>1 else t_day, "now_str": now.strftime('%Y%m%d_%H%M')}

# ================= 3. 数据获取与清洗 =================
def get_data_tickflow():
    if not tf: return None, 0.0
    try:
        df = tf.quotes.get(universes=["CN_Equity_A"], as_dataframe=True)
        if df is None or df.empty: return None, 0.0
        
        # 🆕 调试：打印所有字段名，帮助定位流通市值字段
        st.session_state['tf_columns'] = list(df.columns)
        
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
        
        # 🆕 智能提取流通市值 (兼容多种字段名)
        circ_mv_raw = safe_col('ext.circulating_market_cap', 0.0)
        if np.all(circ_mv_raw == 0): circ_mv_raw = safe_col('ext.float_market_cap', 0.0)
        if np.all(circ_mv_raw == 0): circ_mv_raw = safe_col('circulating_market_cap', 0.0)
        if np.all(circ_mv_raw == 0): circ_mv_raw = safe_col('free_float_market_cap', 0.0)
        if np.all(circ_mv_raw == 0): circ_mv_raw = safe_col('market_cap_float', 0.0)
        if np.all(circ_mv_raw == 0): circ_mv_raw = safe_col('float_cap', 0.0)
        if np.all(circ_mv_raw == 0): circ_mv_raw = safe_col('circ_mv', 0.0)
        
        positive_vals = circ_mv_raw[circ_mv_raw > 0]
        if len(positive_vals) > 0:
            median_val = np.median(positive_vals)
            if median_val > 1e11: circ_mv = circ_mv_raw / 1e8
            elif median_val > 1e7: circ_mv = circ_mv_raw / 1e4
            elif median_val > 1e3: circ_mv = circ_mv_raw / 1e2
            else: circ_mv = circ_mv_raw.copy()
        else:
            circ_mv = circ_mv_raw.copy()

        non_zero_pct = pct_arr[pct_arr != 0]
        if len(non_zero_pct) > 0 and np.median(np.abs(non_zero_pct)) < 0.5: pct_chg = pct_arr * 100
        else: pct_chg = pct_arr.copy()
        
        non_zero_turnover = turnover_arr[turnover_arr != 0]
        if len(non_zero_turnover) > 0 and np.median(np.abs(non_zero_turnover)) < 0.5: turnover = turnover_arr * 100
        else: turnover = turnover_arr.copy()
        
        if np.mean(amount_arr) < 100000: amount = amount_arr * 10000
        else: amount = amount_arr.copy()
        
        pre_close_final = pre_close_arr.copy()
        mask_no_pre = pre_close_final == 0
        if mask_no_pre.any():
            safe_pct = np.where(pct_chg[mask_no_pre] == -100, -99.9, pct_chg[mask_no_pre])
            pre_close_final[mask_no_pre] = close_arr[mask_no_pre] / (1 + safe_pct / 100)
            
        high_final = np.where((high_arr == 0) | (high_arr < close_arr), np.maximum(close_arr, pre_close_final * 1.05), high_arr)
        low_final = np.where((low_arr == 0) | (low_arr > close_arr), np.minimum(close_arr, pre_close_final * 0.95), low_arr)
        
        df['close'], df['high'], df['low'], df['pre_close'] = close_arr, high_final, low_final, pre_close_final
        df['pct_chg'], df['turnover'], df['amount'], df['volume'], df['circ_mv'] = pct_chg, turnover, amount, vol_arr, circ_mv
        df['board'] = df['code'].apply(lambda x: 'Main' if str(x).startswith(('60','00')) else ('GEM' if str(x).startswith(('30','68')) else 'Other'))
        
        return df, float(df['pct_chg'].mean())
    except Exception as e:
        logging.error(f"❌ 数据获取异常：{e}")
        return None, 0.0

def get_market_context(tf_client, df):
    if not tf_client: return "【大盘数据缺失】", 1.0
    indices = {"上证指数": "000001.SH", "创业板指": "399006.SZ"}
    market_summary, ratio = [], 1.0
    try:
        for name, code in indices.items():
            df_k = tf_client.klines.get(code, period="1d", count=5, as_dataframe=True)
            if df_k is not None and len(df_k) >= 2:
                latest, prev = df_k.iloc[-1], df_k.iloc[-2]
                c1, c2 = float(latest.get('close', latest.get('last_price'))), float(prev.get('close', prev.get('last_price')))
                pct = (c1 - c2) / c2 * 100 if c2 > 0 else 0
                a1, a2 = float(latest.get('amount', 0)), float(prev.get('amount', 0))
                if a1 == 0: a1, a2 = float(latest.get('volume', 0)), float(prev.get('volume', 0))
                vol_status = "放量" if a1 > a2*1.05 else ("缩量" if a1 < a2*0.95 else "平量")
                market_summary.append(f"- {name}: 涨幅 {pct:.2f}%, {vol_status}")
                time.sleep(0.1)
        if df is not None and not df.empty:
            up, down = len(df[df['pct_chg']>0]), len(df[df['pct_chg']<0])
            ratio = up / max(down, 1)
            sent = "极度亢奋" if ratio>3 else ("强势" if ratio>1.5 else ("均衡" if ratio>0.8 else ("弱势" if ratio>0.5 else "极度冰点")))
            zt = len(df[(df['board']=='Main') & (df['pct_chg']>=9.8)])
            dt = len(df[(df['board']=='Main') & (df['pct_chg']<=-9.8)])
            market_summary.append(f"- 全市场情绪：涨{up}/跌{down}, 涨跌比{ratio:.2f}, 【{sent}】")
            market_summary.append(f"- 赚钱效应：主板涨停 {zt} 家 | 跌停 {dt} 家")
        return "\n".join(market_summary), ratio
    except Exception as e:
        return f"【大盘数据获取异常：{e}】", 1.0

def get_tickflow_data_for_symbols(tf_client, symbols_list):
    if not tf_client: return pd.DataFrame()
    parsed = [f"{s.split('.')[1]}.{s.split('.')[0]}" if '.' in s else (f"{s}.SH" if s.startswith('6') else f"{s}.SZ") for s in symbols_list]
    valid_rows = []
    for tf_code in parsed:
        try:
            df_k = tf_client.klines.get(tf_code, period="1d", count=2, as_dataframe=True)
            if df_k is None or len(df_k) < 2: continue
            l, p = df_k.iloc[-1], df_k.iloc[-2]
            c1, c2 = float(l.get('close', l.get('last_price'))), float(p.get('close', p.get('last_price')))
            pct = (c1-c2)/c2*100 if c2>0 else 0
            h = float(l.get('high', 0))
            lw = float(l.get('low', 0))
            if h==0 or h<c1: h = max(c1, c2*1.05)
            if lw==0 or lw>c1: lw = min(c1, c2*0.95)
            v1, v2 = float(l.get('volume',0)), float(p.get('volume',0))
            vol_ratio = v1/v2 if v2>0 else 99.0
            name, turnover, amount, circ_mv = tf_code.split('.')[0], 0.0, 0.0, 0.0
            try:
                info = tf_client.quotes.get(symbols=[tf_code], as_dataframe=True)
                if info is not None and not info.empty:
                    if 'ext.name' in info.columns: name = str(info.iloc[0]['ext.name'])
                    turnover = float(info.iloc[0].get('ext.turnover_rate', info.iloc[0].get('turnover_rate', 0)))
                    amount = float(info.iloc[0].get('amount', 0))
                    for mv_col in ['ext.circulating_market_cap','ext.float_market_cap','circulating_market_cap','free_float_market_cap','market_cap_float','float_cap','circ_mv']:
                        if mv_col in info.columns:
                            raw = float(info.iloc[0].get(mv_col, 0))
                            if raw > 0:
                                circ_mv = raw/1e8 if raw>1e11 else (raw/1e4 if raw>1e7 else raw)
                                break
                    if 0<turnover<1.5: turnover*=100
                    if 0<amount<100000: amount*=10000
            except: pass
            valid_rows.append({'tf_code':tf_code,'code':tf_code.split('.')[0],'name':name,'close':c1,'high':h,'low':lw,'pre_close':c2,'pct_chg':pct,'turnover':turnover,'amount':amount,'vol_ratio':vol_ratio,'board':'Main' if tf_code.endswith('.SH') or tf_code.startswith('00') else 'GEM','industry':'自选','circ_mv':circ_mv})
            time.sleep(0.1)
        except: continue
    return pd.DataFrame(valid_rows)

# ================= 4. 筛选器与历史快照 =================
def filter_normal_stocks(df):
    df = df[~df['name'].str.contains('ST|退', na=False)]
    df = df[df['board'].isin(['Main', 'GEM'])]
    mv_mask = (df['circ_mv'] >= 20) & (df['circ_mv'] <= 150) if df['circ_mv'].sum() > 0 else pd.Series([True]*len(df))
    main_mask = (df['board']=='Main') & (df['pct_chg']>=1.5) & (df['pct_chg']<=7.5)
    gem_mask = (df['board']=='GEM') & (df['pct_chg']>=1.5) & (df['pct_chg']<=12.0)
    common = (df['amount']>=120000000) & (df['turnover']<=12.0)
    return df[(main_mask|gem_mask) & common & mv_mask].sort_values(by='turnover').head(20)

def filter_demon_stocks(df):
    df = df[~df['name'].str.contains('ST|退', na=False)]
    df = df[df['board']=='Main']
    return df[(df['close']<=30) & (df['turnover']>=10) & (df['turnover']<=40) & (df['amount']>=3e8) & (df['pct_chg']>=7)].sort_values(by='pct_chg', ascending=False).head(10)

def filter_defense_stocks(df, tf_client, m_avg=0.0):
    df = df[~df['name'].str.contains('ST|退', na=False)]
    df = df[df['board']=='Main']
    low_pct = max(0.5, m_avg+1.0)
    cand = df[(df['pct_chg']>=low_pct) & (df['pct_chg']<=9.5) & (df['amount']>=1.5e8) & (df['turnover']>=3) & (df['turnover']<=15) & (df['close']>=5)].sort_values(by='pct_chg', ascending=False).head(20)
    if cand.empty: return pd.DataFrame()
    verified = []
    for _, r in cand.iterrows():
        try:
            k = tf_client.klines.get(r['tf_code'], period="1d", count=5, as_dataframe=True)
            if k is not None and len(k)>=3:
                tc = float(k.iloc[-1].get('close', k.iloc[-1].get('last_price')))
                rh = float(k.iloc[-3:]['high'].max()) if 'high' in k.columns else 0
                v1, v2 = float(k.iloc[-1].get('volume',0)), float(k.iloc[-2].get('volume',0))
                if tc >= rh*0.99 and (v1/v2 if v2>0 else 99) <= 3.0: verified.append(r['tf_code'])
                time.sleep(0.05)
        except: continue
    return cand[cand['tf_code'].isin(verified)].head(CONFIG['TOP_N_DEFENSE'])

def calculate_real_vol_ratio(cdf):
    ratios = []
    for _, r in cdf.iterrows():
        try:
            k = tf.klines.get(r['tf_code'], period="1d", count=6, as_dataframe=True)
            if k is not None and len(k)>=2:
                tv = pd.to_numeric(k.iloc[-1]['volume'], errors='coerce')
                av = pd.to_numeric(k.iloc[:-1]['volume'], errors='coerce').mean()
                ratios.append(tv/av if av>0 else 99.0)
            else: ratios.append(99.0)
        except: ratios.append(99.0)
        time.sleep(0.05)
    cdf['vol_ratio'] = ratios
    return cdf

def get_history_context(tf_client, tf_code):
    if not tf_client: return "【历史缺失】"
    try:
        k = tf_client.klines.get(tf_code, period="1d", count=60, as_dataframe=True)
        if k is None or len(k)<20: return "【历史不足】"
        k['close'] = pd.to_numeric(k['close'], errors='coerce')
        k['high'] = pd.to_numeric(k['high'], errors='coerce')
        k['low'] = pd.to_numeric(k['low'], errors='coerce')
        k['volume'] = pd.to_numeric(k['volume'], errors='coerce')
        cc = k.iloc[-1]['close']
        h60, l60 = k['high'].max(), k['low'].min()
        pos = (cc-l60)/(h60-l60)*100 if h60>l60 else 50
        pos_d = "高位" if pos>80 else ("低位" if pos<20 else "中位")
        m5, m20, m60 = k['close'].rolling(5).mean().iloc[-1], k['close'].rolling(20).mean().iloc[-1], k['close'].mean()
        trend = "多头" if m5>m20>m60 else ("空头" if m5<m20<m60 else "缠绕")
        vm = k.iloc[-1]['volume'] / k['volume'].tail(20).mean() if k['volume'].tail(20).mean()>0 else 1.0
        pct_c = k['close'].pct_change()*100
        return f"【60日快照】坐标:{pos_d}({pos:.0f}%) | 均线:{trend}(MA5:{m5:.2f}/MA20:{m20:.2f}) | 压力:{h60:.2f}/支撑:{l60:.2f} | 量能:{vm:.1f}倍 | 涨停:{(pct_c>9.5).sum()}/跌停:{(pct_c<-9.5).sum()}"
    except: return "【历史异常】"

# ================= 6. 终极超短线 Prompt 体系 =================
ANTI_HALLUCINATION_RULES = """
【游资实战铁律 - 精简版】
1. 严禁追高：买点必须在分时均线附近或下方，急拉5%以上不追。
2. 量化排雷：尾盘量占比>20%且接近60日高点时，视为诱多，降级为C。
3. 时间止损：买入后3个交易日内未创出新高，无条件清仓。
4. 仓位纪律：单票仓位不超过3成，S级除外。
5. 市场环境：大盘情绪为"冰点"或"退潮"时，最高评级只能是B。
"""

# 🆕 核心修复：极简版 Prompt（用于自动重试时调用）
PROMPT_MINIMAL_NORMAL = """你是A股顶尖游资。必须严格按以下5个标题输出，每段限100字以内，总字数<600字！
### 1. 盘面解读
### 2. 量化排雷
### 3. 次日买点 (给出具体价格)
### 4. 止损位 (给出具体价格和时间点)
### 5. 猎手评级
- **综合评级**：【S/A/B/C选一】
- **仓位建议**：【X成】
- **信心指数**：【1-10分】
- **一句话**：【10字内】"""

PROMPT_MINIMAL_DEMON = """你是A股妖股猎手。必须严格按以下5个标题输出，每段限100字以内，总字数<600字！
### 1. 妖气指数
### 2. 流动性排雷
### 3. 次日买点 (给出具体价格)
### 4. 止损位 (给出具体价格和时间点)
### 5. 猎手评级
- **综合评级**：【S/A/B/C选一】
- **仓位建议**：【X成】
- **信心指数**：【1-10分】
- **一句话**：【10字内】"""

PROMPT_MINIMAL_DEFENSE = """你是A股逆风突破专家。必须严格按以下5个标题输出，每段限100字以内，总字数<600字！
### 1. 逆风强度
### 2. 筹码健康度
### 3. 次日买点 (给出具体价格)
### 4. 止损位 (给出具体价格和时间点)
### 5. 逆风评级
- **综合评级**：【S/A/B/C选一】
- **仓位建议**：【X成】
- **信心指数**：【1-10分】
- **一句话**：【10字内】"""

PROMPT_MINIMAL_WATCHLIST = """你是账户急救操盘手。必须严格按以下4个标题输出，每段限120字以内，总字数<600字！
### 1. 套牢诊断
### 2. 反弹动能
### 3. 急救决断 (四选一：割肉/装死/做T/补仓)
### 4. 操作锚点 (给出具体价格)"""

# 🆕 完整版 Prompt（带字数强制限制）
PROMPT_NORMAL = f"""你是一位在A股摸爬滚打15年的顶尖游资，精通"缩量洗盘后的反包博弈"与"反量化盘中埋伏"。
{ANTI_HALLUCINATION_RULES}

⚠️【字数死命令】总输出必须<1200字！每段严格限制字数！宁可少说也不能被截断！

请务必严格按照以下格式输出：
### 1. 盘面语言解读 (结合历史趋势与今日量价，限150字)
### 2. 流动性与量化排雷 (限100字)
### 3. 次日(T+1)竞价与买点策略 (分高开/平开/低开三种情况，给出精确到分的计算过程和价格，限300字)
### 4. 断臂求生止损位 (价格止损计算+时间止损触发条件，限150字)

你必须以如下格式结束（不可省略，这是最重要的输出）：
---
### 5. 猎手评级与仓位建议
- **综合评级**：【S/A/B/C中选一个】
- **仓位建议**：【X成仓位】
- **信心指数**：【1-10分】
- **一句话总结**：【20字以内】

⚠️⚠️⚠️ 三重警告：
1. 必须包含全部5个段落(### 1.~### 5.)！
2. 绝不能遗漏### 5.！
3. 前4段如果写太多导致### 5.没输出=严重失败！"""

PROMPT_DEMON = f"""你是一位在A股摸爬滚打15年的顶尖游资，精通"龙头首阴反包"与"妖股接力情绪博弈"。
{ANTI_HALLUCINATION_RULES}

⚠️【字数死命令】总输出必须<1200字！每段严格限制字数！宁可少说也不能被截断！

请务必严格按照以下格式输出：
### 1. 妖气指数与龙头信仰 (结合历史趋势分析连板高度，限150字)
### 2. 死亡换手与流动性排雷 (限100字)
### 3. 次日(T+1)竞价与买点策略 (分高开>3%/平开/低开三种情况，给出精确价格，限300字)
### 4. 断臂求生止损位 (价格+时间止损，限150字)

你必须以如下格式结束（不可省略）：
---
### 5. 猎手评级与仓位建议
- **综合评级**：【S/A/B/C中选一个】
- **仓位建议**：【X成仓位】
- **信心指数**：【1-10分】
- **一句话总结**：【20字以内】

⚠️⚠️⚠️ 三重警告：必须包含全部5段！绝不能遗漏### 5.！前4段精简！"""

PROMPT_DEFENSE = f"""你是一位精通"弱市逆风突破"的A股实战猎手。当前大盘萎靡，任务是寻找逆市上涨的真金标的。
{ANTI_HALLUCINATION_RULES}

⚠️【字数死命令】总输出必须<1200字！每段严格限制字数！宁可少说也不能被截断！

请务必严格按照以下格式输出：
### 1. 逆风强度与突破逻辑 (限150字)
### 2. 筹码结构与量能健康度 (限100字)
### 3. 次日(T+1)竞价与买点策略 (分高开/平开/低开三种情况，给出精确价格，限300字)
### 4. 断臂求生止损位 (价格+时间止损，限150字)

你必须以如下格式结束（不可省略）：
---
### 5. 逆风评级与仓位建议
- **综合评级**：【S/A/B/C中选一个】
- **仓位建议**：【X成仓位】
- **信心指数**：【1-10分】
- **一句话总结**：【20字以内】

⚠️⚠️⚠️ 三重警告：必须包含全部5段！绝不能遗漏### 5.！前4段精简！"""

PROMPT_WATCHLIST = f"""你是一位冷酷的"账户急救与解套操盘手"。客户持有的自选股处于套牢状态，任务是最理性的断臂求生或降本解套方案。
{ANTI_HALLUCINATION_RULES}

⚠️【字数死命令】总输出必须<1000字！每段严格限制字数！

请务必严格按照以下格式输出：
### 1. 套牢病情诊断 (结合历史趋势，分析套牢深度和筹码压力，限200字)
### 2. 盘面语言与反弹动能 (分析是否有做T空间，限150字)
### 3. 账户急救决断 (四选一：🩸割肉/🛌装死/🔄做T/💰补仓，限150字)
### 4. 关键操作锚点 (做T买卖点/补仓支撑位/破位清仓价，精确到分，限200字)

⚠️⚠️⚠️ 必须包含全部4段！"""

def analyze_with_llm(stock_dict, minute_feature_text, market_context, history_context, mode="normal"):
    if not llm_client: return "⚠️ 未配置大模型", "⚠️ 无Key"
    
    active_prompts = st.session_state.get("active_prompts", {})
    if mode == "demon": system_p = active_prompts.get("demon", PROMPT_DEMON)
    elif mode == "defense": system_p = active_prompts.get("defense", PROMPT_DEFENSE)
    elif mode == "watchlist": system_p = active_prompts.get("watchlist", PROMPT_WATCHLIST)
    else: system_p = active_prompts.get("normal", PROMPT_NORMAL)
    
    price_info = f"""
【真实价格锚点】
- 当前价: {stock_dict.get('close', 0)} 元
- 今日最低: {stock_dict.get('low', 0)} 元
- 今日最高: {stock_dict.get('high', 0)} 元
- 昨日收盘: {stock_dict.get('pre_close', 0)} 元
- 流通市值: {stock_dict.get('circ_mv', 0):.0f} 亿
"""
    user_prompt = f"""【大盘与情绪】:\n{market_context}
【历史趋势快照】:\n{history_context}
{price_info}
【股票】: {stock_dict.get('name')} ({stock_dict.get('code')}) | {stock_dict.get('board')}
【数据】: 涨幅 {stock_dict.get('pct_chg', 0):.2f}%, 量比 {stock_dict.get('vol_ratio', 0):.2f}, 成交额 {stock_dict.get('amount', 0)/1e8:.1f}亿, 换手 {stock_dict.get('turnover', 0):.2f}%
【分时】: {minute_feature_text}
⚠️ 我将于明日(T+1)买入。请制定明日集合竞价观察及盘中条件买入策略。"""

    def call_llm(sys_prompt, usr_prompt, max_tk=8192):
        response = llm_client.chat.completions.create(
            model=CONFIG["LLM_MODEL"],
            messages=[{"role": "system", "content": sys_prompt}, {"role": "user", "content": usr_prompt}],
            max_tokens=max_tk
        )
        reasoning = getattr(response.choices[0].message, 'reasoning_content', '')
        return reasoning, response.choices[0].message.content

    try:
        reasoning, final = call_llm(system_p, user_prompt)
        
        # 🆕 核心修复：自动重试机制
        if mode != "watchlist" and "### 5." not in final:
            logging.warning(f"⚠️ {stock_dict.get('name', '')} 输出被截断！启用极简版 Prompt 重试...")
            minimal_p = PROMPT_MINIMAL_NORMAL if mode == "normal" else (PROMPT_MINIMAL_DEMON if mode == "demon" else PROMPT_MINIMAL_DEFENSE)
            _, retry_final = call_llm(minimal_p, user_prompt, max_tk=4096)
            final = retry_final + "\n\n⚠️【注：首次输出被截断，此为系统自动重试的精简版结果】"
        
        if mode != "watchlist" and "### 5." not in final:
            final += "\n\n⚠️⚠️⚠️ 【系统警告】：AI输出仍被截断，缺少第5段评级！请手动评估。"
            
        return reasoning, final
    except Exception as e:
        return str(e), f"❌ AI调用失败: {e}"

def get_minute_features(tf_client, tf_codes):
    fmap = {}
    for code in tf_codes:
        try:
            k = tf_client.klines.get(code, period="15m", count=16, as_dataframe=True)
            if k is None or k.empty: fmap[code] = "【分时缺失】"; continue
            tv = pd.to_numeric(k['volume'], errors='coerce').sum()
            tail = pd.to_numeric(k['volume'].tail(2), errors='coerce').sum()
            ratio = (tail/tv*100) if tv>0 else 0
            fmap[code] = f"尾盘30分量占比:{ratio:.1f}%{'(抢筹)' if ratio>25 else ('(流出)' if ratio<10 else '')}"
            time.sleep(0.05)
        except: fmap[code] = "【分时异常】"
    return fmap

# ================= 7. HTML 报告导出 =================
def robust_md_to_html(md_text):
    if not md_text: return "<p>暂无内容</p>"
    html = md_text.replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')
    html = re.sub(r'^#{1,4}\s+(.*?)$', r'<h3>\1</h3>', html, flags=re.MULTILINE)
    html = re.sub(r'\*\*(.*?)\*\*', r'<strong>\1</strong>', html)
    html = re.sub(r'\*(.*?)\*', r'<em>\1</em>', html)
    lines, in_list = [], False
    processed = []
    for line in html.split('\n'):
        s = line.strip()
        is_li = re.match(r'^[-*]\s+(.*)', s) or re.match(r'^\d+\.\s+(.*)', s)
        if is_li:
            if not in_list: processed.append('<ul>'); in_list = True
            c = re.sub(r'^[-*\d.]\s+', '', s)
            processed.append(f'<li>{c}</li>')
        else:
            if in_list: processed.append('</ul>'); in_list = False
            if s.startswith('<h3>'): processed.append(s)
            elif s == '': processed.append('<br>')
            else: processed.append(f'<p>{s}</p>')
    if in_list: processed.append('</ul>')
    return '\n'.join(processed)

def export_to_html_report(normal_results, demon_results, defense_results, watchlist_results, market_context, safe_dates):
    css = """<style>
body{font-family:'Segoe UI','Microsoft YaHei',sans-serif;line-height:1.6;color:#333;max-width:1000px;margin:0 auto;padding:20px;background:#f9f9f9}
.header{text-align:center;border-bottom:3px solid #2c3e50;padding-bottom:15px;margin-bottom:30px}
.header h1{color:#2c3e50;margin:0}.header p{color:#7f8c8d}
.market-box{background:#fff;border-left:5px solid #3498db;padding:15px;margin-bottom:30px;white-space:pre-wrap;font-family:monospace}
.track-title{background:#2c3e50;color:#fff;padding:10px 15px;border-radius:5px 5px 0 0;margin-top:40px;font-size:1.2em;page-break-before:always}
.stock-card{background:#fff;border:1px solid #ddd;border-radius:0 0 5px 5px;padding:20px;margin-bottom:20px;page-break-inside:avoid}
.stock-header{display:flex;justify-content:space-between;border-bottom:1px dashed #ccc;padding-bottom:10px;margin-bottom:15px}
.stock-name{font-size:1.3em;font-weight:bold;color:#e74c3c}.stock-code{color:#7f8c8d}
.stock-metrics{display:flex;flex-wrap:wrap;gap:10px;font-size:.9em;color:#555;margin-bottom:15px;background:#f8f9fa;padding:8px;border-radius:4px}
.metric-item{padding:4px 8px;background:#e9ecef;border-radius:3px}
.analysis-content h3{color:#2980b9;border-bottom:1px solid #eee;padding-bottom:5px;margin-top:20px}
.analysis-content ul{padding-left:20px;margin:10px 0}.analysis-content li{margin-bottom:8px}
.analysis-content strong{color:#c0392b}.analysis-content p{margin:8px 0}
@media print{body{background:#fff}.stock-card{break-inside:avoid}.track-title{break-before:page}}
</style>"""
    parts = [f"<!DOCTYPE html><html lang='zh-CN'><head><meta charset='UTF-8'><title>四轨制猎手复盘报告</title>{css}</head><body>"]
    parts.append(f"<div class='header'><h1>👑 四轨制猎手实战报告</h1><p>生成时间: {safe_dates['now_str']} | 基准日(T日): {safe_dates['today']}</p></div>")
    parts.append("<h2>🌍 今日大盘与情绪环境</h2>")
    parts.append(f"<div class='market-box'>{market_context}</div>")
    
    def render_track(name, emoji, results):
        if not results: return ""
        html = f"<div class='track-title'>{emoji} {name}</div>"
        for item in results:
            row, final = item['row'], item['final']
            pc = "#e74c3c" if row['pct_chg']>=0 else "#27ae60"
            mv = f"{row.get('circ_mv',0):.0f}" if row.get('circ_mv',0)>0 else "未知"
            html += f"""<div class="stock-card">
<div class="stock-header"><span class="stock-name">{row['name']}</span><span class="stock-code">{row['code']} | {row['board']}</span></div>
<div class="stock-metrics">
<span class="metric-item">当前价: {row['close']:.2f}</span>
<span class="metric-item" style="color:{pc}">涨幅: {row['pct_chg']:.2f}%</span>
<span class="metric-item">换手: {row['turnover']:.2f}%</span>
<span class="metric-item">量比: {row.get('vol_ratio',0):.2f}</span>
<span class="metric-item">成交额: {row['amount']/1e8:.1f}亿</span>
<span class="metric-item" style="background:#fff3cd;">流通市值: {mv}亿</span>
</div><div class="analysis-content">{robust_md_to_html(final)}</div></div>"""
        return html
    
    parts.append(render_track("轨道一：缩量潜伏池", "🛡️", normal_results))
    parts.append(render_track("轨道二：主板妖股池", "🐉", demon_results))
    parts.append(render_track("轨道三：逆风突破池", "🔥", defense_results))
    parts.append(render_track("自选股深度诊断", "👁️", watchlist_results))
    parts.append("</body></html>")
    return "\n".join(parts).encode('utf-8')

# ================= 验尸与数据记录 =================
def save_today_predictions(normal_res, demon_res, defense_res, safe_dates):
    if not gc or not spreadsheet_url: return
    try:
        sh = gc.open_by_url(spreadsheet_url)
        ws = sh.worksheet(SHEET_NAME)
        existing = ws.get_all_values()
        if len(existing) > 1:
            dates = [r[0] for r in existing[1:]]
            if safe_dates['today'] in dates:
                st.warning(f"⚠️ {safe_dates['today']} 已存在，防重复跳过。"); return
    except Exception as e: st.warning(f"⚠️ 检查重复失败: {e}")
    
    all_res = []
    for res_list, track in [(normal_res,"缩量潜伏"),(demon_res,"主板妖股"),(defense_res,"逆风突破")]:
        for item in res_list:
            row, final = item['row'], item['final']
            buy_m = re.search(r'(?:买点|买入价|竞价买点)[：:\s]*(\d{1,3}(?:\.\d{1,2})?)', final)
            stop_m = re.search(r'(?:止损|止损价|离场价)[：:\s]*(\d{1,3}(?:\.\d{1,2})?)', final)
            rating_m = re.search(r'综合评级[：:\s]*[【\[]?([SABC])[】\]]?(?!\d)', final)
            all_res.append([safe_dates['today'],row['name'],row['code'],track,round(row['close'],2),
                float(buy_m.group(1)) if buy_m else 0.0, float(stop_m.group(1)) if stop_m else 0.0,
                rating_m.group(1) if rating_m else "未评级", final[:2000], None,None,None,"待验尸"])
    if all_res:
        try: ws.append_rows(all_res); st.success(f"✅ {len(all_res)}条策略已存入云端！")
        except Exception as e: st.error(f"❌ 写入失败: {e}")

def run_autopsy(safe_dates):
    if not gc or not spreadsheet_url: return
    try:
        sh = gc.open_by_url(spreadsheet_url)
        ws = sh.worksheet(SHEET_NAME)
        rows = ws.get_all_values()
        if len(rows) < 2: return
        header = rows[0]
        try:
            ci = {k: header.index(k) for k in ['验尸结果','代码','AI建议买点','AI建议止损','T+1日最高','T+1日最低','T+1日收盘']}
        except: return
        
        pending = [(i, r) for i, r in enumerate(rows[1:], 2) if len(r)>ci['验尸结果'] and r[ci['验尸结果']]=='待验尸']
        if not pending: return
        
        st.info(f"🔍 检测到 {len(pending)} 条待验尸记录...")
        codes = list(set([r[ci['代码']] for _, r in pending if len(r)>ci['代码']]))
        real_data = get_tickflow_data_for_symbols(tf, codes)
        
        cnt = 0
        for idx, r in pending:
            code = r[ci['代码']] if len(r)>ci['代码'] else ""
            real = real_data[real_data['code']==code]
            if real.empty: continue
            rr = real.iloc[0]
            try: buy = float(r[ci['AI建议买点']]) if r[ci['AI建议买点']] else 0
            except: buy = 0
            try: stop = float(r[ci['AI建议止损']]) if r[ci['AI建议止损']] else 0
            except: stop = 0
            
            res = "数据不足"
            if buy>0 and stop>0:
                if rr['low']<=stop: res=f"❌爆头(低{rr['low']:.2f}破止损{stop:.2f})"
                elif rr['high']>=buy*1.05: res=f"🏆大肉(高{rr['high']:.2f})"
                elif rr['close']>buy: res=f"✅浮盈(收{rr['close']:.2f})"
                else: res=f"⚠️阴跌(收{rr['close']:.2f})"
            
            ws.update_cell(idx, ci['T+1日最高']+1, round(rr['high'],2))
            ws.update_cell(idx, ci['T+1日最低']+1, round(rr['low'],2))
            ws.update_cell(idx, ci['T+1日收盘']+1, round(rr['close'],2))
            ws.update_cell(idx, ci['验尸结果']+1, res)
            cnt += 1
        if cnt>0: st.success(f"💀 验尸完毕！更新{cnt}条。")
    except Exception as e: st.warning(f"验尸异常: {e}")

# ================= 导师AI进化引擎 =================
def generate_prompt_evolution(failed_text, current_desc):
    if not llm_client: return "⚠️ 未配置大模型", None
    mentor_sys = """你是A股量化策略的导师级AI。任务：诊断失败交易案例的思维错误，生成诊断报告和进化补丁。
输出格式：
## 📊 错题诊断报告（300字内分析）
## 🔧 进化补丁（编号列表，具体可执行，不超过5条）"""
    try:
        resp = llm_client.chat.completions.create(
            model=CONFIG["LLM_MODEL"],
            messages=[{"role":"system","content":mentor_sys},{"role":"user","content":f"当前Prompt: {current_desc}\n\n失败案例:\n{failed_text}\n\n请诊断并生成进化补丁。"}],
            max_tokens=3000)
        full = resp.choices[0].message.content
        patch = ""
        if "## 🔧 进化补丁" in full:
            parts = full.split("## 🔧 进化补丁")
            report = parts[0].replace("## 📊 错题诊断报告","").strip()
            patch = parts[1].strip()
        else: report = full
        return report, patch
    except Exception as e: return f"❌ 导师AI失败: {e}", None

# ================= 8. Streamlit 主界面 =================
st.set_page_config(page_title="V26.0 四轨猎魔策略 (防截断版)", layout="wide")
st.title("👑 四轨制猎手 V26.0 (防截断 + 自动重试版)")
safe_dates = get_safe_trade_dates()
st.caption(f"📅 基准交易日: {safe_dates['today']} | 上一交易日: {safe_dates['yesterday']}")
run_autopsy(safe_dates)

with st.sidebar:
    st.header("⚙️ 全市场扫描参数")
    top_n_normal = st.slider("🛡️ 缩量轨 TOP N", 1, 20, CONFIG["TOP_N_NORMAL"])
    top_n_demon = st.slider("🐉 妖股轨 TOP N", 1, 10, CONFIG["TOP_N_DEMON"])
    st.divider()
    st.header("👁️ 自选股监控")
    watchlist_input = st.text_area("输入代码(逗号/换行分隔)", value="600519, 000858, 300750", height=150)
    st.divider()
    
    # 🆕 市值字段诊断工具
    st.header("🔬 字段诊断工具")
    if st.button("🔍 探测TickFlow流通市值字段", use_container_width=True):
        if tf:
            try:
                test_df = tf.quotes.get(universes=["CN_Equity_A"], as_dataframe=True)
                if test_df is not None:
                    cols = list(test_df.columns)
                    st.session_state['tf_columns'] = cols
                    mv_cols = [c for c in cols if any(k in c.lower() for k in ['cap','mv','float','circ','market'])]
                    st.info(f"📋 全部字段({len(cols)}个):\n{', '.join(cols)}")
                    if mv_cols:
                        st.success(f"🎯 疑似市值字段: {', '.join(mv_cols)}")
                        for mc in mv_cols:
                            sample = test_df[mc].dropna().head(3).tolist()
                            st.write(f"  - `{mc}`: 样本值 = {sample}")
                    else:
                        st.warning("⚠️ 未发现含cap/mv/float/circ/market关键字的字段")
            except Exception as e: st.error(f"探测失败: {e}")
        else: st.warning("TickFlow未初始化")
    
    st.divider()
    st.header("🧬 AI策略进化中心")
    st.caption(f"状态: {st.session_state.current_active_prompt}")
    run_evo = st.button("🔍 分析错题本", use_container_width=True)
    st.divider()
    run_scan = st.button("🚀 全市场四轨扫描", type="primary", use_container_width=True)
    run_wl = st.button("👁️ 自选股深度诊断", type="secondary", use_container_width=True)

if run_evo:
    if not llm_client: st.error("❌ 未配置LLM")
    else:
        with st.spinner("导师AI正在批改作业..."):
            try:
                if not gc or not spreadsheet_url: st.warning("⚠️ Sheets未连接"); st.stop()
                sh = gc.open_by_url(spreadsheet_url)
                df_h = pd.DataFrame(sh.worksheet("Sheet1").get_all_records())
                if df_h.empty: st.warning("⚠️ 无历史数据")
                else:
                    failed = df_h[df_h['验尸结果'].str.contains('爆头|套牢|数据不足', na=False)]
                    if failed.empty: st.success("🎉 近期全部盈利！")
                    else:
                        txt = ""
                        rc = 'AI预测理由' if 'AI预测理由' in failed.columns else '轨道'
                        for _, r in failed.tail(8).iterrows():
                            txt += f"【案例】{r['日期']}|{r['股票名称']}({r['代码']})|{r['轨道']}\nAI理由:{str(r.get(rc,'无'))[:500]}\n结果:{r['验尸结果']}\n\n"
                        report, patch = generate_prompt_evolution(txt, st.session_state.current_active_prompt)
                        st.session_state.analysis_report = report
                        st.session_state.prompt_draft = patch
                        st.rerun()
            except Exception as e: st.error(f"❌ 读取失败: {e}")

if st.session_state.analysis_report and st.session_state.prompt_draft:
    st.header("🧬 AI策略进化工作台")
    st.markdown("### 📊 导师诊断报告")
    st.markdown(st.session_state.analysis_report)
    st.markdown("### 🔧 进化补丁预览")
    st.code(st.session_state.prompt_draft, language="text")
    c1, c2 = st.columns(2)
    with c1:
        if st.button("✅ 确认应用进化补丁", type="primary"):
            new_p = st.session_state.prompt_draft
            base = st.session_state.base_anti_hallucination_rules
            evolved = base + "\n\n## 进化补丁\n" + new_p
            fmt = """
⚠️【字数死命令】总输出<1200字！
### 1. 盘面解读 (限150字)
### 2. 量化排雷 (限100字)
### 3. 次日买点 (限300字，给出精确价格)
### 4. 止损位 (限150字，价格+时间)
### 5. 猎手评级
- **综合评级**：【S/A/B/C选一】
- **仓位建议**：【X成】
- **信心指数**：【1-10分】
- **一句话**：【20字内】
⚠️⚠️⚠️ 必须包含全部5段！不能遗漏### 5.！"""
            st.session_state.active_prompts["normal"] = f"你是A股顶尖游资，精通缩量反包。\n{evolved}\n{fmt}"
            st.session_state.active_prompts["demon"] = f"你是A股妖股猎手。\n{evolved}\n{fmt}"
            st.session_state.active_prompts["defense"] = f"你是A股逆风突破专家。\n{evolved}\n{fmt}"
            st.session_state.active_prompts["watchlist"] = f"你是账户急救操盘手。\n{evolved}\n{fmt}"
            st.session_state.current_active_prompt = f"已进化({datetime.now(tz_shanghai).strftime('%m-%d %H:%M')})"
            try:
                sh = gc.open_by_url(spreadsheet_url)
                try:
                    pw = sh.worksheet("Prompt_History")
                    ver = f"v1.{len(pw.get_all_values())-1}"
                except:
                    pw = sh.add_worksheet(title="Prompt_History", rows=100, cols=4)
                    pw.append_row(["Timestamp","Version","Content","Report"]); ver="v1.0"
                pw.append_row([datetime.now(tz_shanghai).strftime("%Y-%m-%d %H:%M"), ver, new_p, st.session_state.analysis_report])
                st.success(f"🎉 {ver} 进化成功！")
            except Exception as e: st.warning(f"⚠️ 补丁已生效，云端写入失败: {e}")
            st.session_state.analysis_report = None
            st.session_state.prompt_draft = None
            st.rerun()

if run_scan or run_wl:
    if not tf or not llm_client: st.error("❌ 客户端初始化失败"); st.stop()
    CONFIG["TOP_N_NORMAL"] = top_n_normal
    CONFIG["TOP_N_DEMON"] = top_n_demon
    
    with st.spinner("🚀 获取全市场A股数据..."):
        df, m_avg = get_data_tickflow()
        if df is None: st.error("❌ 数据获取失败"); st.stop()
    
    market_ctx, m_ratio = get_market_context(tf, df)
    st.subheader("🌍 今日大盘与情绪")
    st.text(market_ctx)
    
    normal_res, demon_res, defense_res, watchlist_res = [], [], [], []
    
    if run_scan:
        st.info("🛡️ 【轨道一】筛选缩量洗盘猎物...")
        ndf = filter_normal_stocks(df)
        if not ndf.empty:
            ndf = calculate_real_vol_ratio(ndf)
            ndf = ndf[ndf['vol_ratio']<=1.3].sort_values(by='vol_ratio').head(CONFIG['TOP_N_NORMAL'])
        
        st.info("🐉 【轨道二】扫描主板妖股...")
        ddf = filter_demon_stocks(df)
        if not ddf.empty:
            ddf = calculate_real_vol_ratio(ddf).head(CONFIG['TOP_N_DEMON'])
        
        defense_df = pd.DataFrame()
        if m_ratio < 1.0 or m_avg < 0.0:
            st.warning("🔥 【轨道三】市场偏弱，激活逆风突破池！")
            defense_df = filter_defense_stocks(df, tf, m_avg)
            if not defense_df.empty: defense_df = calculate_real_vol_ratio(defense_df)
        
        all_codes = []
        if not ndf.empty: all_codes.extend(ndf['tf_code'].tolist())
        if not ddf.empty: all_codes.extend(ddf['tf_code'].tolist())
        if not defense_df.empty: all_codes.extend(defense_df['tf_code'].tolist())
        min_feat = get_minute_features(tf, list(set(all_codes)))
        
        total = len(ndf) + len(ddf) + len(defense_df)
        if total == 0: st.warning("今日无符合标的")
        else:
            pb = st.progress(0)
            task = 0
            if not ndf.empty:
                for _, r in ndf.iterrows():
                    task += 1; pb.progress(task/total)
                    hctx = get_history_context(tf, r['tf_code'])
                    reason, final = analyze_with_llm(r.to_dict(), min_feat.get(r['tf_code'],""), market_ctx, hctx, "normal")
                    normal_res.append({'row':r,'reasoning':reason,'final':final})
                    time.sleep(1)
            if not ddf.empty:
                for _, r in ddf.iterrows():
                    task += 1; pb.progress(task/total)
                    hctx = get_history_context(tf, r['tf_code'])
                    reason, final = analyze_with_llm(r.to_dict(), min_feat.get(r['tf_code'],""), market_ctx, hctx, "demon")
                    demon_res.append({'row':r,'reasoning':reason,'final':final})
                    time.sleep(1)
            if not defense_df.empty:
                for _, r in defense_df.iterrows():
                    task += 1; pb.progress(task/total)
                    hctx = get_history_context(tf, r['tf_code'])
                    reason, final = analyze_with_llm(r.to_dict(), min_feat.get(r['tf_code'],""), market_ctx, hctx, "defense")
                    defense_res.append({'row':r,'reasoning':reason,'final':final})
                    time.sleep(1)
            pb.empty()
        
        try: save_today_predictions(normal_res, demon_res, defense_res, safe_dates)
        except Exception as e: st.warning(f"⚠️ 预测写入失败: {e}")
        
        st.subheader("🛡️ 轨道一：缩量潜伏池")
        if normal_res:
            for i, item in enumerate(normal_res, 1):
                r = item['row']
                mv = f"{r.get('circ_mv',0):.0f}" if r.get('circ_mv',0)>0 else "未知"
                with st.expander(f"[{i}] {r['name']} ({r['code']}) | 涨:{r['pct_chg']:.1f}% | 换手:{r['turnover']:.1f}% | 市值:{mv}亿"):
                    if item['reasoning']: st.caption(f"🧠 推演: {item['reasoning'][:500]}...")
                    st.markdown(item['final'])
        else: st.warning("今日无符合轨道一标的")
        
        st.subheader("🐉 轨道二：主板妖股池")
        if demon_res:
            for i, item in enumerate(demon_res, 1):
                r = item['row']
                with st.expander(f"[{i}] {r['name']} ({r['code']}) | 涨:{r['pct_chg']:.1f}% | 换手:{r['turnover']:.1f}%"):
                    if item['reasoning']: st.caption(f"🧠 推演: {item['reasoning'][:500]}...")
                    st.markdown(item['final'])
        else: st.warning("今日无符合轨道二标的")
        
        st.subheader("🔥 轨道三：逆风突破池")
        if defense_res:
            for i, item in enumerate(defense_res, 1):
                r = item['row']
                with st.expander(f"[{i}] {r['name']} ({r['code']}) | 涨:{r['pct_chg']:.1f}% | 换手:{r['turnover']:.1f}%"):
                    if item['reasoning']: st.caption(f"🧠 推演: {item['reasoning'][:500]}...")
                    st.markdown(item['final'])
        else: st.info("逆风池未激活或无标的")
        
        st.divider()
        html_data = export_to_html_report(normal_res, demon_res, defense_res, [], market_ctx, safe_dates)
        if html_data:
            st.session_state.html_report_data = html_data
            st.session_state.html_report_filename = f"四轨制复盘_{safe_dates['now_str']}.html"
    
    if run_wl:
        st.info("👁️ 获取自选股数据...")
        symbols = [s.strip() for s in re.split(r'[,\n\s]+', watchlist_input) if s.strip()]
        wdf = get_tickflow_data_for_symbols(tf, symbols)
        if not wdf.empty:
            wdf = calculate_real_vol_ratio(wdf)
            wc = wdf['tf_code'].tolist()
            min_feat = get_minute_features(tf, wc)
            pb = st.progress(0)
            for idx, (_, r) in enumerate(wdf.iterrows()):
                pb.progress((idx+1)/len(wdf))
                hctx = get_history_context(tf, r['tf_code'])
                reason, final = analyze_with_llm(r.to_dict(), min_feat.get(r['tf_code'],""), market_ctx, hctx, "watchlist")
                watchlist_res.append({'row':r,'reasoning':reason,'final':final})
                time.sleep(1)
            pb.empty()
            st.subheader("🚑 自选股套牢急救")
            for i, item in enumerate(watchlist_res, 1):
                r = item['row']
                with st.expander(f"🩸 [{i}] {r['name']} ({r['code']}) | 价:{r['close']:.2f} | 涨幅:{r['pct_chg']:.1f}%"):
                    if item['reasoning']: st.caption(f"🧠 推演: {item['reasoning'][:500]}...")
                    st.markdown(item['final'])
            st.divider()
            html_data = export_to_html_report([], [], [], watchlist_res, market_ctx, safe_dates)
            if html_data:
                st.session_state.html_report_data = html_data
                st.session_state.html_report_filename = f"自选股诊断_{safe_dates['now_str']}.html"
        else: st.warning("⚠️ 自选股数据获取失败")

st.divider()
if st.session_state.get("html_report_data"):
    st.subheader("📥 下载报告")
    st.download_button(
        label=f"💾 下载: {st.session_state.html_report_filename}",
        data=st.session_state.html_report_data,
        file_name=st.session_state.html_report_filename,
        mime="text/html",
        use_container_width=True,
        type="primary"
    )
else:
    st.caption("💡 提示：运行扫描或诊断后，这里会出现下载按钮。")
