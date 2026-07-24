import logging
import time
import re
import numpy as np
import pandas as pd
from openai import OpenAI
from datetime import datetime
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
    "TOP_N_DEFENSE": 3, # 稍微增加逆风池数量
    "TF_API_KEY": TF_API_KEY,
    "LLM_API_KEY": LLM_API_KEY,
    "LLM_BASE_URL": "https://api.deepseek.com/v1", 
    "LLM_MODEL": "deepseek-reasoner" 
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
            if col_name in df.columns:
                return pd.to_numeric(df[col_name], errors='coerce').fillna(default).values
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
        
        high_final = np.where(high_arr == 0, close_arr, high_arr)
        low_final = np.where(low_arr == 0, close_arr, low_arr)
        
        pre_close_final = pre_close_arr.copy()
        mask_no_pre = pre_close_final == 0
        if mask_no_pre.any():
            safe_pct = pct_chg[mask_no_pre]
            safe_pct = np.where(safe_pct == -100, -99.9, safe_pct)
            pre_close_final[mask_no_pre] = close_arr[mask_no_pre] / (1 + safe_pct / 100)
            
        df['close'] = close_arr
        df['high'] = high_final
        df['low'] = low_final
        df['pre_close'] = pre_close_final
        df['pct_chg'] = pct_chg
        df['turnover'] = turnover
        df['amount'] = amount
        df['volume'] = vol_arr
        
        if 'ext.industry' in df.columns:
            df['industry'] = df['ext.industry'].astype(str)
        else:
            df['industry'] = '未知行业'
        
        def identify_board(code):
            code = str(code)
            if code.startswith(('60', '00')): return 'Main'
            elif code.startswith(('30', '68')): return 'GEM'
            return 'Other'
        df['board'] = df['code'].apply(identify_board)
        
        # 计算全市场平均涨幅 (用于逆风池动态基准)
        market_avg_pct = float(df['pct_chg'].mean())
        
        logging.info(f"✅ 成功清洗 {len(df)} 只标的的数据，全市场均涨幅: {market_avg_pct:.2f}%")
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
            
            if dt_main > 10:
                market_summary.append(f"⚠️ 极度恶劣行情: 跌停 {dt_main} 家，大面(跌>7%) {big_loss} 家！【退潮期/核按钮期，严禁接力，空仓保平安】")
            elif dt_main > 3:
                market_summary.append(f"⚠️ 局部亏钱效应: 跌停 {dt_main} 家，大面 {big_loss} 家。【接力需极度谨慎】")
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
        if '.' in s:
            parts = s.split('.')
            parsed_symbols.append(f"{parts[1]}.{parts[0]}")
        else:
            if s.startswith('6'): parsed_symbols.append(f"{s}.SH")
            else: parsed_symbols.append(f"{s}.SZ")
            
    valid_rows = []
    for tf_code in parsed_symbols:
        try:
            df_k = tf_client.klines.get(tf_code, period="1d", count=2, as_dataframe=True)
            if df_k is None or df_k.empty or len(df_k) < 2: continue
            latest, prev = df_k.iloc[-1], df_k.iloc[-2]
            close_today = float(latest.get('close', latest.get('last_price')))
            close_prev = float(prev.get('close', prev.get('last_price'))) 
            pct = (close_today - close_prev) / close_prev * 100 if close_prev > 0 else 0
            high = float(latest.get('high', latest.get('high_price', close_today)))
            low = float(latest.get('low', latest.get('low_price', close_today)))
            vol_today = float(latest.get('volume', 0))
            vol_prev = float(prev.get('volume', 0))
            vol_ratio = vol_today / vol_prev if vol_prev > 0 else 99.0
            
            name = tf_code.split('.')[0]
            try:
                info = tf_client.quotes.get(symbols=[tf_code], as_dataframe=True)
                if info is not None and not info.empty and 'ext.name' in info.columns:
                    name = str(info.iloc[0]['ext.name'])
            except: pass
            
            valid_rows.append({
                'tf_code': tf_code, 'code': tf_code.split('.')[0], 'name': name,
                'close': close_today, 'high': high, 'low': low, 'pre_close': close_prev,
                'pct_chg': pct, 'turnover': 0.0, 'amount': 0.0, 'vol_ratio': vol_ratio,
                'board': 'Main' if tf_code.endswith('.SH') or tf_code.startswith('00') else 'GEM',
                'industry': '自选股行业'
            })
            time.sleep(0.1)
        except Exception as e:
            logging.error(f"获取 {tf_code} 失败: {e}")
            continue
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
    """
    🔧 重构：从“防御池”升级为“逆风池”
    核心逻辑：在大盘萎靡时，寻找逆市上涨、筹码稳健且有突破潜力的标的
    """
    df = df[~df['name'].str.contains('ST|退', na=False)]
    df = df[df['board'] == 'Main'] 
    
    # 1. 逆市上涨标准：涨幅 > max(0.5%, 大盘均涨 + 1.0%)，且未涨停(保留上车空间)
    lower_pct = max(0.5, market_avg_pct + 1.0)
    
    # 2. 稳健突破条件：成交额>1.5亿(有资金关注)，换手3%~15%(活跃但不失控)，价格>5元
    mask = (df['pct_chg'] >= lower_pct) & (df['pct_chg'] <= 9.5) & \
           (df['amount'] >= 150000000) & (df['turnover'] >= 3.0) & (df['turnover'] <= 15.0) & \
           (df['close'] >= 5.0)
           
    candidates = df[mask].sort_values(by='pct_chg', ascending=False).head(20)
    if candidates.empty: 
        return pd.DataFrame()
    
    verified_codes = []
    for _, row in candidates.iterrows():
        try:
            df_k = tf_client.klines.get(row['tf_code'], period="1d", count=5, as_dataframe=True)
            if df_k is not None and len(df_k) >= 3:
                today_close = float(df_k.iloc[-1].get('close', df_k.iloc[-1].get('last_price')))
                yesterday_high = float(df_k.iloc[-2].get('high', df_k.iloc[-2].get('high_price')))
                
                # 3. 突破验证：今日收盘价站上昨日最高价（或近3日最高价）
                recent_3d_high = float(df_k.iloc[-3:]['high'].max()) if 'high' in df_k.columns else yesterday_high
                
                # 4. 稳健性验证：剔除今日极端爆量(量比>3.0)的票，防止诱多出货
                vol_today = float(df_k.iloc[-1].get('volume', 0))
                vol_yesterday = float(df_k.iloc[-2].get('volume', 0))
                vol_ratio = vol_today / vol_yesterday if vol_yesterday > 0 else 99.0
                
                if today_close >= recent_3d_high * 0.99 and vol_ratio <= 3.0:
                    verified_codes.append(row['tf_code'])
            time.sleep(0.05)
        except Exception:
            continue
            
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

# ================= 5. 概念探针与四轨 Prompt (防幻觉重构) =================
@st.cache_data(ttl=3600, show_spinner=False)
def build_hot_concept_dict(df_market):
    if df_market is None or df_market.empty:
        return {}, []
    try:
        industry_stats = df_market.groupby('industry')['pct_chg'].mean().reset_index()
        industry_stats = industry_stats[industry_stats['industry'] != '未知行业']
        top_industries = industry_stats.sort_values(by='pct_chg', ascending=False).head(10)
        hot_concepts = top_industries['industry'].tolist()
        concept_dict = {}
        for _, row in df_market.iterrows():
            code = str(row['code'])
            ind = str(row['industry'])
            if ind != '未知行业':
                concept_dict[code] = [ind]
        return concept_dict, hot_concepts
    except Exception as e:
        logging.warning(f"热门行业计算失败: {e}")
        return {}, []

@st.cache_data(ttl=3600, show_spinner=False)
def get_stock_concepts(stock_code: str, concept_dict: dict, stock_name: str = "") -> str:
    pure_code = re.sub(r'[^0-9]', '', str(stock_code))
    if pure_code in concept_dict:
        return f"【今日热门板块/行业】: {', '.join(concept_dict[pure_code])}"
    name = str(stock_name).lower()
    if '银行' in name or pure_code.startswith(('601398', '601288', '601939', '600036')): return "【所属行业】: 银行/大金融"
    if '证券' in name or '券商' in name: return "【所属行业】: 证券/大金融"
    if '药' in name or '医' in name or '生物' in name: return "【所属行业】: 医药生物"
    if '半导' in name or '芯' in name or '微电' in name: return "【所属行业】: 半导体/芯片"
    if '科技' in name or '软件' in name or '信息' in name: return "【所属行业】: 计算机/TMT"
    if '新能' in name or '锂' in name or '电池' in name: return "【所属行业】: 新能源/锂电"
    if '酒' in name or '食品' in name: return "【所属行业】: 大消费/白酒"
    return f"【概念缺失，请基于股票名称 '{stock_name}' 自行推演所属板块】"

# 🔧 优化：极度严苛的防幻觉系统提示词
ANTI_HALLUCINATION_RULES = """
⚠️ 游资实战铁律（违反将导致严重亏损）：
1. 【严禁编造价格】：你输出的所有止损位、目标价、买入价、压力位，**必须**基于我提供的【当前真实价格】、【今日最低】、【今日最高】和【昨日收盘】进行精确的数学计算（精确到小数点后两位）。
2. 【强制数学公式】：输出价格时，必须展示计算过程！例如：“止损位 = 昨日最低价 10.50 * 0.97 = 10.18 元”。严禁凭空捏造数字！
3. 【严禁使用历史记忆】：绝对不要使用你训练数据中的历史股价、历史走势、历史新闻！你的认知边界仅限于我提供的【今日盘面数据】。
4. 【散户视角】：我是资金量不足50万的个人散户。不要给我机构那种“逢低分批建仓”的废话。我要的是“一击必杀”的确定性和“断臂求生”的致命止损。
5. 【拒绝端水】：不要说“建议关注”、“请注意风险”这种废话。直接告诉我：买还是不买？什么价格买？什么价格割肉？
"""

PROMPT_NORMAL = f"""你是一位在A股摸爬滚打15年的顶尖游资，精通"缩量洗盘后的反包博弈"与"反量化盘中埋伏"。
{ANTI_HALLUCINATION_RULES}
请务必严格按照以下格式输出：
### 1. 盘面语言解读 (结合【题材与板块】，看透主力意图)
### 2. 流动性与量化排雷
### 3. 反量化买点 (必须包含具体价格计算过程，精确到分)
### 4. 断臂求生止损位 (必须包含具体价格计算过程)
### 5. 猎手评级与仓位建议 (S/A/B/C)"""

PROMPT_DEMON = f"""你是一位A股顶尖的"主板(10%)连板妖股接力"大师。你从不看基本面，只看情绪和筹码。
{ANTI_HALLUCINATION_RULES}
请务必严格按照以下格式输出：
### 1. 情绪定性与连板身位 (结合【题材与板块】，判断龙头还是杂毛)
### 2. 筹码断层与爆量风险
### 3. 主板接力手法 (必须包含具体打板/半路价格计算过程)
### 4. 断头铡刀止损 (必须包含具体止损价格计算过程)
### 5. 猎手评级与仓位建议 (S/A/B/C)"""

PROMPT_DEFENSE = f"""你是一位精通"弱市逆风突破"的A股实战猎手。当前大盘萎靡/冰点，你的任务是在泥沙俱下中寻找"逆市上涨、筹码稳健、即将突破"的真金标的。你追求的是弱势中的确定性主升浪起点。
{ANTI_HALLUCINATION_RULES}
请务必严格按照以下格式输出：
### 1. 逆风强度与突破逻辑 (结合【题材与板块】，分析其为何能逆势走强及突破有效性)
### 2. 筹码结构与量能健康度 (分析换手率与量比，确认是稳健吸筹而非诱多出货)
### 3. 稳健突破买点 (必须包含具体价格计算过程，给出回踩确认或打板的具体价位)
### 4. 证伪止损价 (突破失败必须走，基于关键支撑位给出具体价格计算过程)
### 5. 逆风评级与仓位建议 (S/A/B/C)"""

PROMPT_WATCHLIST = f"""你是一位严苛的自选股审视者。请结合当前大盘环境和所属题材，对这只自选股进行“灵魂拷问”。
{ANTI_HALLUCINATION_RULES}
请务必严格按照以下格式输出：
### 1. 趋势与题材审视
### 2. 量价背离排雷
### 3. 去留决断 (明确给出：加仓/持有/减仓/清仓)
### 4. 关键价格锚点 (必须基于真实价格计算出具体的支撑位和压力位，展示计算过程)"""

def analyze_with_llm(stock_dict, minute_feature_text, market_context, concept_dict, mode="normal"):
    if not llm_client: return "⚠️ 未配置大模型", "⚠️ 无Key"
    
    concept_info = get_stock_concepts(stock_dict.get('code'), concept_dict, stock_dict.get('name'))
    news_context = "【今日无重大突发新闻，请纯粹基于盘面量价、情绪与所属题材进行推演】"
    
    if mode == "demon": system_p = PROMPT_DEMON
    elif mode == "defense": system_p = PROMPT_DEFENSE
    elif mode == "watchlist": system_p = PROMPT_WATCHLIST
    else: system_p = PROMPT_NORMAL
    
    # 🔧 优化：在 User Prompt 中再次强调价格锚点，锁死 AI 边界
    price_info = f"""
【真实价格锚点 (严禁瞎编，必须基于此计算，展示公式)】
- 当前价: {stock_dict.get('close', '未知')} 元
- 今日最低: {stock_dict.get('low', '未知')} 元
- 今日最高: {stock_dict.get('high', '未知')} 元
- 昨日收盘: {stock_dict.get('pre_close', '未知')} 元
"""
    
    user_prompt = f"""【大盘与情绪】:\n{market_context}
【题材与板块】: {concept_info}
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
            logic_text = ""
            if tail_ratio > 25: logic_text = " (尾盘异动抢筹)"
            elif tail_ratio < 10: logic_text = " (尾盘平淡/资金流出)"
            features_map[tf_code] = f"尾盘30分量占比: {tail_ratio:.1f}%{logic_text}"
            time.sleep(0.05)
        except: features_map[tf_code] = "【分时异常】"
    return features_map

# ================= 6. 表格提取与 Excel 导出 =================
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
        all_data.append({"轨道": "🐉 妖股池", "股票名称": row['name'], "代码": row['code'], "当前价": row.get('close', ''), "涨幅%": f"{row['pct_chg']:.2f}", "换手%": f"{row['turnover']:.2f}", "红比": f"{row['vol_ratio']:.2f}", "评级": extract_section(final, "猎手评级"), "买点推演": extract_section(final, "主板接力手法"), "止损位": extract_section(final, "断头铡刀止损")})
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
            for cell in worksheet[1]:
                cell.font = header_font
                cell.fill = header_fill
                cell.alignment = Alignment(horizontal="center", vertical="center")
            for column in worksheet.columns:
                max_length = max((len(str(cell.value)) if cell.value else 0) for cell in column)
                worksheet.column_dimensions[column[0].column_letter].width = min((max_length + 2) * 1.1, 45)
        output.seek(0)
        return output
    except Exception as e:
        logging.error(f"❌ Excel 导出失败: {e}")
        return None

# ================= 7. Streamlit Web 主界面 =================
st.set_page_config(page_title="V22.0 四轨猎魔策略", layout="wide")
st.title("👑 四轨制猎手 V22.0 (防幻觉+逆势防御版)")

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

# ================= 8. 执行逻辑隔离 =================
if run_market_scan or run_watchlist:
    if not tf or not llm_client:
        st.error("❌ 客户端初始化失败，请检查 Secrets 配置")
        st.stop()
        
    CONFIG["TOP_N_NORMAL"] = top_n_normal
    CONFIG["TOP_N_DEMON"] = top_n_demon
    
    with st.spinner("🚀 正在获取全市场 A 股日线快照..."):
        df, market_avg_pct = get_data_tickflow()
        
    if df is None:
        st.error("❌ 大盘数据获取失败")
        st.stop()
        
    market_context, market_ratio = get_market_context(tf, df)
    st.subheader("🌍 今日大盘与情绪环境")
    st.text(market_context)
    
    concept_dict, hot_concepts = build_hot_concept_dict(df)
    if hot_concepts:
        st.info(f"🎯 今日资金主攻方向 (行业/板块): {', '.join(hot_concepts[:5])}")
    else:
        st.warning("⚠️ 行业板块数据获取失败，已降级使用名称推演数据")

    # ================= 模式 A：全市场四轨扫描 =================
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
        # 🔧 优化：逆风池激活条件放宽，只要大盘偏弱或情绪冰点就激活
        if market_ratio < 1.0 or market_avg_pct < 0.0: 
            st.warning("🔥 【轨道三】检测到市场偏弱/冰点，自动激活逆风突破池！")
            # 传入 market_avg_pct 用于动态计算逆势标准
            defense_df = filter_defense_stocks(df, tf, market_avg_pct)
            if not defense_df.empty:
                defense_df = calculate_real_vol_ratio(defense_df)
                
        all_codes = []
        if not normal_df.empty: all_codes.extend(normal_df['tf_code'].tolist())
        if not demon_df.empty: all_codes.extend(demon_df['tf_code'].tolist())
        if not defense_df.empty: all_codes.extend(defense_df['tf_code'].tolist())
        
        minute_features = get_minute_features(tf, list(set(all_codes)))
        normal_results, demon_results, defense_results = [], [], []
        
        total_tasks = len(normal_df) + len(demon_df) + len(defense_df)
        if total_tasks == 0: 
            st.warning("今日暂无符合三轨条件的标的")
        else:
            progress_bar = st.progress(0)
            current_task = 0
            
            if not normal_df.empty:
                for _, row in normal_df.iterrows():
                    current_task += 1
                    progress_bar.progress(current_task / total_tasks)
                    reasoning, final = analyze_with_llm(row.to_dict(), minute_features.get(row['tf_code'], ""), market_context, concept_dict, mode="normal")
                    normal_results.append({'row': row, 'reasoning': reasoning, 'final': final})
                    time.sleep(1)
                    
            if not demon_df.empty:
                for _, row in demon_df.iterrows():
                    current_task += 1
                    progress_bar.progress(current_task / total_tasks)
                    reasoning, final = analyze_with_llm(row.to_dict(), minute_features.get(row['tf_code'], ""), market_context, concept_dict, mode="demon")
                    demon_results.append({'row': row, 'reasoning': reasoning, 'final': final})
                    time.sleep(1)
                    
            if not defense_df.empty:
                for _, row in defense_df.iterrows():
                    current_task += 1
                    progress_bar.progress(current_task / total_tasks)
                    reasoning, final = analyze_with_llm(row.to_dict(), minute_features.get(row['tf_code'], ""), market_context, concept_dict, mode="defense")
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
            st.download_button(
                label="📥 下载全市场四轨复盘 Excel 报告",
                data=excel_data,
                file_name=f"四轨制复盘_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )

    # ================= 模式 B：自选股深度诊断 =================
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
                reasoning, final = analyze_with_llm(row.to_dict(), minute_features.get(row['tf_code'], ""), market_context, concept_dict, mode="watchlist")
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
                st.download_button(
                    label="📥 下载自选股诊断 Excel 报告",
                    data=excel_data,
                    file_name=f"自选股诊断_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                )
        else:
            st.warning("⚠️ 未获取到有效自选股数据，请检查代码输入是否正确")
