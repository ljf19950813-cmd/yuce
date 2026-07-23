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

# 【安全铁律】公开仓库严禁硬编码密钥，必须全部走 Secrets
try:
    TF_API_KEY = st.secrets["TF_API_KEY"]
    LLM_API_KEY = st.secrets["LLM_API_KEY"]
except KeyError as e:
    st.error(f"❌ 缺少必要的密钥配置: {e}")
    st.info("请在 Streamlit Cloud 的 Settings -> Secrets 中添加 TF_API_KEY 和 LLM_API_KEY")
    st.stop()  # 直接停止应用运行，防止后续代码使用空密钥报错

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
            timeout=httpx.Timeout(30.0, connect=10.0)
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

# ================= 5. 新闻探针与双轨 Prompt (已修复超时与崩溃问题) =================
@st.cache_data(ttl=300, show_spinner=False)
def get_stock_news_akshare(stock_code: str, stock_name: str, max_news: int = 3) -> str:
    if not ak: 
        return "⚠️ 环境缺失 akshare 库，无法获取新闻，请基于盘面独立思考。"
    
    try:
        # 强力清洗代码，只保留6位数字
        pure_code = re.sub(r'[^0-9]', '', str(stock_code))
        if len(pure_code) != 6:
            return f"⚠️ 股票代码格式异常: {stock_code}，请基于盘面独立思考。"
        
        # 尝试获取新闻，增加容错
        df = ak.stock_news_em(symbol=pure_code)
        
        if df is None or df.empty:
            return f"📭 {stock_name}({pure_code}) 近期暂无重大新闻，请基于盘面数据独立判断。"
        
        news_list = []
        for _, row in df.head(max_news).iterrows():
            title = str(row.get('新闻标题', '')).strip()
            content = str(row.get('新闻内容', ''))[:80].strip()
            pub_time = str(row.get('发布时间', ''))
            if title:
                news_list.append(f"[{pub_time}] {title}：{content}")
                
        if not news_list:
            return f"📭 {stock_name} 新闻解析为空，请基于盘面数据独立判断。"
            
        return "\n".join(news_list)
        
    except Exception as e:
        # 捕获所有异常（包括超时、网络错误、接口变动），防止程序崩溃
        logging.warning(f"新闻获取异常 [{stock_name}({stock_code})]: {type(e).__name__}")
        return f"⚠️ {stock_name} 新闻接口暂时维护中，请基于盘面数据独立思考。"

ANTI_HALLUCINATION_RULES = """
⚠️ 绝对铁律（违反将导致严重亏损）：
1. 【严禁编造价格】：你输出的所有止损位、目标价、买入价，**必须**基于我提供的【当前真实价格】、【今日最低】和【今日最高】进行数学计算。
2. 【严禁使用历史记忆】：绝对不要使用你训练数据中的历史股价！严禁凭空捏造数字！
"""

PROMPT_NORMAL = f"""你是一位A股顶尖游资，精通"缩量洗盘后的反包博弈"与"反量化盘中埋伏"。
{ANTI_HALLUCINATION_RULES}
请务必在你的【实战指令】中，严格按照以下格式输出（不要改变标题）：
### 1. 政策/新闻验证
### 2. 流动性排雷
### 3. 反量化买点 (必须包含具体价格计算)
### 4. 条件止损位 (必须包含具体价格计算)
### 5. 猎手评级 (S/A/B/C)"""

PROMPT_DEMON = f"""你是一位A股顶尖的"主板(10%)连板妖股接力"大师。
{ANTI_HALLUCINATION_RULES}
请务必在你的【实战指令】中，严格按照以下格式输出（不要改变标题）：
### 1. 情绪定性与连板身位
### 2. 筹码断层与爆量风险
### 3. 主板接力手法 (必须包含具体打板/半路价格)
### 4. 断头铡刀止损 (必须包含具体止损价格)
### 5. 猎手评级 (S/A/B/C)"""

def analyze_with_llm(stock_dict, minute_feature_text, market_context, is_demon=False):
    if not llm_client: return "⚠️ 未配置大模型", "⚠️ 无Key"
    news_context = get_stock_news_akshare(stock_dict.get('code'), stock_dict.get('name'))
    system_p = PROMPT_DEMON if is_demon else PROMPT_NORMAL
    
    price_info = f"""
【真实价格锚点 (严禁瞎编，必须基于此计算)】
- 当前价: {stock_dict.get('close', '未知')} 元
- 今日最低: {stock_dict.get('low', '未知')} 元
- 今日最高: {stock_dict.get('high', '未知')} 元
- 昨日收盘: {stock_dict.get('pre_close', '未知')} 元
"""
    
    user_prompt = f"""【大盘与情绪】:\n{market_context}\n【实时新闻】:\n{news_context}\n{price_info}
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
        all_data.append({"轨道": "🛡️ 潜伏池", "股票名称": row['name'], "代码": row['code'], "当前价": row.get('close', ''), "涨幅%": f"{row['pct_chg']:.2f}", "换手%": f"{row['turnover']:.2f}", "量比": f"{row['vol_ratio']:.2f}", "评级": extract_section(final, "猎手评级"), "买点推演": extract_section(final, "反量化买点"), "止损位": extract_section(final, "条件止损位"), "新闻验证": extract_section(final, "政策/新闻验证")})
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

# ================= 7. Streamlit Web 主界面 =================
st.set_page_config(page_title="V16.7 双轨猎魔策略", layout="wide")
st.title("👑 双轨制猎手 V16.7 (Web云端安全版)")

with st.sidebar:
    st.header("⚙️ 参数配置")
    top_n_normal = st.slider("正常轨 TOP N", 1, 20, CONFIG["TOP_N_NORMAL"])
    top_n_demon = st.slider("恶魔轨 TOP N", 1, 10, CONFIG["TOP_N_DEMON"])
    run_analysis = st.button("🚀 开始分析", type="primary")

if run_analysis:
    if not tf or not llm_client:
        st.error("❌ 客户端初始化失败，请检查 Streamlit Secrets 配置或网络连接")
    else:
        CONFIG["TOP_N_NORMAL"] = top_n_normal
        CONFIG["TOP_N_DEMON"] = top_n_demon
        
        with st.spinner("🚀 正在获取全市场 A 股日线快照..."):
            df = get_data_tickflow()
            
        if df is None:
            st.error("❌ 数据获取失败，请检查 TickFlow 服务状态")
        else:
            market_context = get_market_context(tf, df)
            st.subheader("🌍 今日大盘与情绪环境")
            st.text(market_context)
            
            st.info("🛡️ 【轨道一】正在筛选缩量洗盘、博弈反标的稳健猎物...")
            normal_df = filter_normal_stocks(df)
            if not normal_df.empty:
                normal_df = calculate_real_vol_ratio(normal_df)
                normal_df = normal_df[normal_df['vol_ratio'] <= 0.9].head(CONFIG['TOP_N_NORMAL'])
            
            st.info("🐉 【轨道二】正在扫描主板(10%)高换手、爆量、具备连板基因的妖股...")
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
            
            if total_tasks == 0:
                st.warning("今日暂无符合双轨条件的标的，请调整参数或等待明日行情。")
            
            if not normal_df.empty:
                for _, row in normal_df.iterrows():
                    current_task += 1
                    progress_bar.progress(current_task / total_tasks)
                    logging.info(f"🤖 正在推演 {row['name']} (缩量潜伏)...")
                    reasoning, final = analyze_with_llm(row.to_dict(), minute_features.get(row['tf_code'], ""), market_context, is_demon=False)
                    normal_results.append({'row': row, 'reasoning': reasoning, 'final': final})
                    time.sleep(1)
                    
            if not demon_df.empty:
                for _, row in demon_df.iterrows():
                    current_task += 1
                    progress_bar.progress(current_task / total_tasks)
                    logging.info(f"🤖 正在推演 {row['name']} (主板妖股接力)...")
                    reasoning, final = analyze_with_llm(row.to_dict(), minute_features.get(row['tf_code'], ""), market_context, is_demon=True)
                    demon_results.append({'row': row, 'reasoning': reasoning, 'final': final})
                    time.sleep(1)
                    
            progress_bar.empty()
            
            st.subheader("🛡️ 轨道一：缩量潜伏池 (稳健反包)")
            if normal_results:
                for idx, item in enumerate(normal_results, 1):
                    row, reasoning, final = item['row'], item['reasoning'], item['final']
                    with st.expander(f"[{idx}] {row['name']} ({row['code']}) | 涨幅:{row['pct_chg']:.1f}% 换手:{row['turnover']:.1f}% 量比:{row['vol_ratio']:.2f}"):
                        if reasoning: st.caption(f"🧠 脑内推演: {reasoning[:500]}...")
                        st.markdown(final)
            else:
                st.warning("今日暂无符合轨道一条件的标的")
                
            st.subheader("🐉 轨道二：主板妖股池 (10%连板接力)")
            if demon_results:
                for idx, item in enumerate(demon_results, 1):
                    row, reasoning, final = item['row'], item['reasoning'], item['final']
                    with st.expander(f"[{idx}] {row['name']} ({row['code']}) | 涨幅:{row['pct_chg']:.1f}% 换手:{row['turnover']:.1f}% 量比:{row['vol_ratio']:.2f}"):
                        if reasoning: st.caption(f"🧠 脑内推演: {reasoning[:500]}...")
                        st.markdown(final)
            else:
                st.warning("今日暂无符合轨道二条件的标的")
                
            st.divider()
            excel_bytes = export_to_excel_bytes(normal_results, demon_results)
            if excel_bytes:
                filename = f"双轨猎手复盘_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
                st.download_button(
                    label="📥 导出分析结果 Excel",
                    data=excel_bytes,
                    file_name=filename,
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                )
            else:
                st.warning("⚠️ 没有数据可导出")
else:
    st.info("👈 请在左侧配置参数后点击「开始分析」")
