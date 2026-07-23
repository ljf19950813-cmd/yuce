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

# 尝试导入可选库
try:
    from tickflow import TickFlow
except ImportError:
    TickFlow = None

try:
    import akshare as ak
except ImportError:
    ak = None

warnings.filterwarnings("ignore")

# ================= 1. 全局配置 (安全版 - 强制从 Secrets 读取) =================
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
    if not tf: return None
    try:
        logging.info("🚀 获取全市场 A 股日线快照...")
        df = tf.quotes.get(universes=["CN_Equity_A"], as_dataframe=True)
        if df is None or df.empty: return None
        
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
        
        def identify_board(code):
            code = str(code)
            if code.startswith(('60', '00')): return 'Main'
            elif code.startswith(('30', '68')): return 'GEM'
            return 'Other'
        df['board'] = df['code'].apply(identify_board)
        
        logging.info(f"✅ 成功清洗 {len(df)} 只标的的数据")
        return df
    except Exception as e:
        logging.error(f"❌ 数据获取异常: {e}")
        return None

def get_market_context(tf_client, df):
    if not tf_client: return "【大盘数据缺失】"
    indices = {"上证指数": "000001.SH", "创业板指": "399006.SZ"}
    market_summary = []
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
            market_summary.append(f"- 全市场情绪: 涨{up_count}/跌{down_count}, 涨跌比{ratio:.2f}, 【{sentiment}】")
            zt_main = len(df[(df['board']=='Main') & (df['pct_chg']>9.5)])
            market_summary.append(f"- 主板(10%)涨停家数: {zt_main}家")
        return "\n".join(market_summary)
    except Exception as e:
        return f"【大盘数据获取异常: {e}】"

# ================= 4. 双轨制筛选器 =================
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

# ================= 5. 概念探针与双轨 Prompt =================
@st.cache_data(ttl=3600, show_spinner=False)
def get_stock_concepts(stock_code: str) -> str:
    if not ak: return "【概念缺失】"
    try:
        pure_code = re.sub(r'[^0-9]', '', str(stock_code))
        if len(pure_code) != 6: return "【代码异常】"
        df_info = ak.stock_individual_info_em(symbol=pure_code)
        if df_info is not None and not df_info.empty:
            industry = df_info[df_info['item'] == '行业'].iloc[0]['value'] if '行业' in df_info['item'].values else '未知'
            return f"所属行业: {industry}"
        return "【概念获取失败】"
    except Exception as e:
        return f"⚠️ 概念接口维护中: {type(e).__name__}"

ANTI_HALLUCINATION_RULES = """
⚠️ 游资实战铁律（违反将导致严重亏损）：
1. 【严禁编造价格】：你输出的所有止损位、目标价、买入价，**必须**基于我提供的【当前真实价格】、【今日最低】、【今日最高】和【昨日收盘】进行精确的数学计算（精确到小数点后两位）。
2. 【严禁使用历史记忆】：绝对不要使用你训练数据中的历史股价！严禁凭空捏造数字！
3. 【散户视角】：我是资金量不足50万的个人散户。不要给我机构那种“逢低分批建仓”的废话。我的优势是灵活，劣势是通道慢。我要的是“一击必杀”的确定性和“断臂求生”的果断。
4. 【拒绝端水】：不要说“建议关注”、“请注意风险”这种废话。直接告诉我：买还是不买？什么价格买？什么价格割肉？
"""

PROMPT_NORMAL = f"""你是一位在A股摸爬滚打15年的顶尖游资，精通"缩量洗盘后的反包博弈"与"反量化盘中埋伏"。你深知散户的痛点，你的指令必须像刀子一样锋利。
{ANTI_HALLUCINATION_RULES}
请务必在你的【实战指令】中，严格按照以下格式输出（不要改变标题，内容要一针见血，充满杀气）：

### 1. 盘面语言解读 (放弃新闻，看透主力意图与题材共振)
- 结合该股【题材与板块】，分析今日的K线形态与量价关系。
- 直接点破主力是在借题材"洗盘"还是在"出货"。

### 2. 流动性与量化排雷
- 该股是否被量化资金主导？换手率和成交额是否支持散户安全进出？

### 3. 反量化买点 (必须包含具体价格计算，精确到分)
- 给出**唯一**的买入价格（基于今日低点、均价线或重要支撑位计算）。
- 说明买入的逻辑（例如：跌破今日低点后迅速拉回的“黄金坑”）。

### 4. 断臂求生止损位 (必须包含具体价格计算)
- 给出**绝对止损价**（跌破此价无条件核按钮，不抱任何幻想）。

### 5. 猎手评级与仓位建议 (S/A/B/C)
- S级：重仓出击；A级：半仓试错；B级：轻仓观察；C级：狗都不看，直接拉黑。"""

PROMPT_DEMON = f"""你是一位A股顶尖的"主板(10%)连板妖股接力"大师。你深知妖股的本质是情绪的极致和资金的接力，你从不看基本面，只看情绪和筹码。
{ANTI_HALLUCINATION_RULES}
请务必在你的【实战指令】中，严格按照以下格式输出（不要改变标题，内容要一针见血，充满杀气）：

### 1. 情绪定性与连板身位 (看透该股在情绪周期中的位置)
- 结合该股【题材与板块】，当前市场情绪是“冰点”、“发酵”还是“高潮”？
- 该股是当前热门题材的“龙头”、“龙二”还是“跟风杂毛”？

### 2. 筹码断层与爆量风险
- 分析今日的换手率是否健康？是否存在获利盘兑现的“断头铡刀”风险？

### 3. 主板接力手法 (必须包含具体打板/半路价格)
- 给出**唯一**的接力手法（例如：弱转强半路、或者打板确认）。给出具体的挂单价格。

### 4. 断头铡刀止损 (必须包含具体止损价格)
- 妖股接力失败就是A杀，给出**次日开盘或盘中的绝对止损价**。

### 5. 猎手评级与仓位建议 (S/A/B/C)
- S级：龙头信仰，满仓干；A级：试错仓；B级：看戏；C级：杂毛跟风，直接拉黑。"""

def analyze_with_llm(stock_dict, minute_feature_text, market_context, is_demon=False):
    if not llm_client: return "⚠️ 未配置大模型", "⚠️ 无Key"
    
    concept_info = get_stock_concepts(stock_dict.get('code'))
    news_context = "【今日无重大突发新闻，请纯粹基于盘面量价、情绪与所属题材进行推演】"
    
    system_p = PROMPT_DEMON if is_demon else PROMPT_NORMAL
    
    price_info = f"""
【真实价格锚点 (严禁瞎编，必须基于此计算)】
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
            features_map[tf_code] = f"尾盘30分量占比: {tail_ratio:.1f}% ({'异动抢筹' if tail_ratio > 25 else '平淡'})"
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

def export_to_excel_bytes(normal_results, demon_results):
    all_data = []
    for item in normal_results:
        row, final = item['row'], item['final']
        all_data.append({"轨道": "🛡️ 潜伏池", "股票名称": row['name'], "代码": row['code'], "当前价": row.get('close', ''), "涨幅%": f"{row['pct_chg']:.2f}", "换手%": f"{row['turnover']:.2f}", "量比": f"{row['vol_ratio']:.2f}", "评级": extract_section(final, "猎手评级"), "买点推演": extract_section(final, "反量化买点"), "止损位": extract_section(final, "断臂求生止损位"), "盘面解读": extract_section(final, "盘面语言解读")})
    for item in demon_results:
        row, final = item['row'], item['final']
        all_data.append({"轨道": "🐉 妖股池", "股票名称": row['name'], "代码": row['code'], "当前价": row.get('close', ''), "涨幅%": f"{row['pct_chg']:.2f}", "换手%": f"{row['turnover']:.2f}", "量比": f"{row['vol_ratio']:.2f}", "评级": extract_section(final, "猎手评级"), "买点推演": extract_section(final, "主板接力手法"), "止损位": extract_section(final, "断头铡刀止损"), "情绪身位": extract_section(final, "情绪定性与连板身位")})
        
    if not all_data: return None
        
    df = pd.DataFrame(all_data)
    output = BytesIO()
    try:
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name='双轨制复盘')
            worksheet = writer.sheets['双轨制复盘']
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

# ================= 7. 自选股分析函数 =================
def analyze_custom_stock(stock_input, market_context, force_demon=False):
    stock_input = stock_input.strip()
    if not stock_input: return None
    pure_input = re.sub(r'[^0-9a-zA-Z]', '', stock_input)
    
    try:
        if pure_input.isdigit() and len(pure_input) == 6:
            if pure_input.startswith(('60', '68')): tf_code = f"{pure_input}.SH"
            else: tf_code = f"{pure_input}.SZ"
            
            df_k = tf.klines.get(tf_code, period="1d", count=6, as_dataframe=True)
            if df_k is None or df_k.empty:
                return {"error": f"❌ 未找到股票代码 {pure_input} 的数据"}
            
            latest = df_k.iloc[-1]
            prev = df_k.iloc[-2] if len(df_k) >= 2 else latest
            close_today = float(latest.get('close', latest.get('last_price', 0)))
            close_prev = float(prev.get('close', prev.get('last_price', 0)))
            pct_chg = (close_today - close_prev) / close_prev * 100 if close_prev > 0 else 0
            
            stock_name = pure_input
            if ak:
                try:
                    df_info = ak.stock_individual_info_em(symbol=pure_input)
                    if df_info is not None and not df_info.empty:
                        name_row = df_info[df_info['item'] == '股票简称']
                        if not name_row.empty: stock_name = str(name_row.iloc[0]['value'])
                except: pass
            
            today_vol = pd.to_numeric(df_k.iloc[-1].get('volume', 0), errors='coerce')
            past_avg_vol = pd.to_numeric(df_k.iloc[:-1]['volume'], errors='coerce').mean() if len(df_k) > 1 else 1
            vol_ratio = today_vol / past_avg_vol if past_avg_vol > 0 else 1.0
            
            high_today = float(latest.get('high', latest.get('high_price', close_today)))
            low_today = float(latest.get('low', latest.get('low_price', close_today)))
            amount_today = float(latest.get('amount', 0))
            
            minute_text = "【分时数据暂缺】"
            try:
                df_15m = tf.klines.get(tf_code, period="15m", count=16, as_dataframe=True)
                if df_15m is not None and not df_15m.empty:
                    total_vol_15 = pd.to_numeric(df_15m['volume'], errors='coerce').sum()
                    tail_vol_15 = pd.to_numeric(df_15m['volume'].tail(2), errors='coerce').sum()
                    tail_ratio_15 = (tail_vol_15 / total_vol_15 * 100) if total_vol_15 > 0 else 0
                    minute_text = f"尾盘30分量占比: {tail_ratio_15:.1f}% ({'异动抢筹' if tail_ratio_15 > 25 else '平淡'})"
            except: pass
            
            board = 'Main' if pure_input.startswith(('60', '00')) else 'GEM'
            is_demon = force_demon or (pct_chg >= 7.0 and board == 'Main')
            
            stock_dict = {
                'code': pure_input, 'name': stock_name, 'tf_code': tf_code, 'board': board,
                'close': close_today, 'high': high_today, 'low': low_today, 'pre_close': close_prev,
                'pct_chg': pct_chg, 'vol_ratio': vol_ratio, 'amount': amount_today, 'turnover': 0
            }
            return {'stock_dict': stock_dict, 'minute_text': minute_text, 'is_demon': is_demon}
            
        elif ak:
            df_all = ak.stock_zh_a_spot_em()
            if df_all is not None and not df_all.empty:
                mask = df_all['名称'].str.contains(stock_input, na=False)
                matched = df_all[mask]
                if matched.empty: matched = df_all[df_all['名称'] == stock_input]
                if not matched.empty:
                    code = str(matched.iloc[0].get('代码', ''))
                    return analyze_custom_stock(code, market_context, force_demon)
            return {"error": f"❌ 未找到名称包含 '{stock_input}' 的股票"}
        else:
            return {"error": "❌ 未安装 akshare，请直接输入6位代码"}
    except Exception as e:
        return {"error": f"❌ 自选股分析异常: {e}"}

# ================= 8. Streamlit Web 主界面 =================
st.set_page_config(page_title="V16.7 双轨猎魔策略", layout="wide")
st.title("👑 双轨制猎手 V16.7 (Web云端安全版)")

with st.sidebar:
    st.header("⚙️ 参数配置")
    top_n_normal = st.slider("正常轨 TOP N", 1, 20, CONFIG["TOP_N_NORMAL"])
    top_n_demon = st.slider("恶魔轨 TOP N", 1, 10, CONFIG["TOP_N_DEMON"])
    run_analysis = st.button("🚀 开始全市场扫描", type="primary")
    
    st.divider()
    st.header("🎯 自选股分析")
    custom_input = st.text_input("输入股票名称或代码", placeholder="例如：贵州茅台 或 600519", key="custom_stock_input")
    custom_mode = st.radio("分析模式", ["🤖 自动判断", "🛡️ 潜伏模式", "🐉 妖股模式"], key="custom_mode")
    run_custom = st.button("🔍 分析自选股", use_container_width=True)

if run_custom:
    if not tf or not llm_client:
        st.error("❌ 客户端初始化失败，请检查 Secrets 配置")
    elif not custom_input:
        st.warning("⚠️ 请输入股票名称或代码")
    else:
        force_demon = custom_mode == "🐉 妖股模式"
        force_normal = custom_mode == "🛡️ 潜伏模式"
        with st.spinner(f"🔍 正在分析自选股: {custom_input} ..."):
            df_market = get_data_tickflow()
            market_ctx = get_market_context(tf, df_market) if df_market is not None else "【大盘数据暂缺】"
            result = analyze_custom_stock(custom_input, market_ctx, force_demon)
            
            if result and "error" in result:
                st.error(result["error"])
            elif result:
                stock_dict = result['stock_dict']
                minute_text = result['minute_text']
                is_demon = result['is_demon'] if not force_normal else False
                mode_label = "🐉 妖股接力" if is_demon else "🛡️ 潜伏反包"
                st.subheader(f"🎯 {stock_dict['name']} ({stock_dict['code']}) | {mode_label}")
                
                col1, col2, col3, col4, col5 = st.columns(5)
                col1.metric("当前价", f"{stock_dict['close']:.2f}元")
                col2.metric("涨幅", f"{stock_dict['pct_chg']:.2f}%")
                col3.metric("量比", f"{stock_dict['vol_ratio']:.2f}")
                col4.metric("最高", f"{stock_dict['high']:.2f}元")
                col5.metric("最低", f"{stock_dict['low']:.2f}元")
                st.divider()
                
                with st.spinner("🤖 AI 正在深度推演..."):
                    reasoning, final = analyze_with_llm(stock_dict, minute_text, market_ctx, is_demon=is_demon)
                if reasoning:
                    with st.expander("🧠 AI 脑内推演过程"): st.caption(reasoning[:2000])
                st.markdown(final)

if run_analysis:
    if not tf or not llm_client:
        st.error("❌ 客户端初始化失败")
    else:
        CONFIG["TOP_N_NORMAL"] = top_n_normal
        CONFIG["TOP_N_DEMON"] = top_n_demon
        with st.spinner("🚀 正在获取全市场 A 股日线快照..."):
            df = get_data_tickflow()
        if df is None:
            st.error("❌ 数据获取失败")
        else:
            market_context = get_market_context(tf, df)
            st.subheader("🌍 今日大盘与情绪环境")
            st.text(market_context)
            
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
                
            all_codes = []
            if not normal_df.empty: all_codes.extend(normal_df['tf_code'].tolist())
            if not demon_df.empty: all_codes.extend(demon_df['tf_code'].tolist())
            
            minute_features = get_minute_features(tf, list(set(all_codes)))
            normal_results, demon_results = [], []
            
            progress_bar = st.progress(0)
            total_tasks = len(normal_df) + len(demon_df)
            current_task = 0
            
            if total_tasks == 0: st.warning("今日暂无符合双轨条件的标的")
            
            if not normal_df.empty:
                for _, row in normal_df.iterrows():
                    current_task += 1
                    progress_bar.progress(current_task / total_tasks)
                    reasoning, final = analyze_with_llm(row.to_dict(), minute_features.get(row['tf_code'], ""), market_context, is_demon=False)
                    normal_results.append({'row': row, 'reasoning': reasoning, 'final': final})
                    time.sleep(1)
                    
            if not demon_df.empty:
                for _, row in demon_df.iterrows():
                    current_task += 1
                    progress_bar.progress(current_task / total_tasks)
                    reasoning, final = analyze_with_llm(row.to_dict(), minute_features.get(row['tf_code'], ""), market_context, is_demon=True)
                    demon_results.append({'row': row, 'reasoning': reasoning, 'final': final})
                    time.sleep(1)
                    
            progress_bar.empty()
            
            st.subheader("🛡️ 轨道一：缩量潜伏池")
            if normal_results:
                for idx, item in enumerate(normal_results, 1):
                    row, reasoning, final = item['row'], item['reasoning'], item['final']
                    with st.expander(f"[{idx}] {row['name']} ({row['code']}) | 涨幅:{row['pct_chg']:.1f}% 换手:{row['turnover']:.1f}% 量比:{row['vol_ratio']:.2f}"):
                        if reasoning: st.caption(f"🧠 脑内推演: {reasoning[:500]}...")
                        st.markdown(final)
            else: st.warning("今日暂无符合轨道一条件的标的")
                
            st.subheader("🐉 轨道二：主板妖股池")
            if demon_results:
                for idx, item in enumerate(demon_results, 1):
                    row, reasoning, final = item['row'], item['reasoning'], item['final']
                    with st.expander(f"[{idx}] {row['name']} ({row['code']}) | 涨幅:{row['pct_chg']:.1f}% 换手:{row['turnover']:.1f}% 量比:{row['vol_ratio']:.2f}"):
                        if reasoning: st.caption(f"🧠 脑内推演: {reasoning[:500]}...")
                        st.markdown(final)
            else: st.warning("今日暂无符合轨道二条件的标的")
                
            st.divider()
            excel_bytes = export_to_excel_bytes(normal_results, demon_results)
            if excel_bytes:
                filename = f"双轨猎手复盘_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
                st.download_button(label="📥 导出分析结果 Excel", data=excel_bytes, file_name=filename, mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

if not run_analysis and not run_custom:
    st.info("👈 请在左侧操作：\n\n1️⃣ **自选股分析**：输入名称/代码，点击「🔍 分析自选股」\n\n2️⃣ **全市场扫描**：配置参数后，点击「🚀 开始全市场扫描」")
