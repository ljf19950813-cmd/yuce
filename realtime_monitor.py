import asyncio
import json
import time
import threading
from datetime import datetime, timezone, timedelta
import requests
import websockets
from openai import OpenAI

# 北京时间时区
TZ_SHANGHAI = timezone(timedelta(hours=8))

# ======================= 模块级钉钉推送函数 =======================
def send_dingtalk_alert(webhook, title, text):
    """
    通用钉钉推送函数，可被外部（如 morning_fix）和类内部复用。
    webhook: 钉钉机器人 Webhook 地址
    """
    if not webhook:
        return
    headers = {"Content-Type": "application/json"}
    data = {
        "msgtype": "markdown",
        "markdown": {"title": title, "text": f"### {title}\n{text}"}
    }
    try:
        requests.post(webhook, headers=headers, json=data)
    except Exception as e:
        print(f"钉钉推送失败: {e}")


class RealtimeMonitor(threading.Thread):
    def __init__(self, target_dicts, portfolio_dicts,
                 tickflow_api_key, dingtalk_webhook,
                 llm_client=None, llm_config=None):
        super().__init__(daemon=True)
        self.target_dicts = target_dicts
        self.portfolio_dicts = portfolio_dicts
        self.tickflow_api_key = tickflow_api_key
        self.dingtalk_webhook = dingtalk_webhook
        self.llm_client = llm_client
        self.llm_config = llm_config or {}
        self._has_quotes = False
        self.last_prices = {}         # code -> 最新价
        self.open_prices = {}         # code -> 开盘价（用于三档判断基准）
        self.portfolio_review_interval = 30 * 60
        self._review_stop_event = threading.Event()
        self.market_risk_check_interval = 60  # 每60秒检查一次大盘风险
        self._risk_stop_event = threading.Event()
        self.market_ratio = None   # 当前涨跌比
        self.dt_count = 0          # 跌停家数

        # 去重构建代码列表
        all_codes = set()
        for d in target_dicts + portfolio_dicts:
            code = str(d.get('code', '')).strip().zfill(6)
            if code:
                all_codes.add(code)
        self.symbols = list(all_codes)
        self.tf_symbols = [
            f"{code}.{'SH' if code.startswith('6') else 'SZ'}" for code in self.symbols
        ]
        self._stop_event = threading.Event()

        # 状态回传（线程安全）
        self.status_lock = threading.Lock()
        self.status_info = {"connected": False, "last_msg_time": None, "error": None}
        self.latest_quotes = []
        self.max_quotes = 5

    def run(self):
        self.review_thread = self.start_review_timer()
        self.risk_thread = self.start_market_risk_monitor()   # 新增
        asyncio.run(self.async_run())

    def start_review_timer(self):
        def _timer():
            while not self._review_stop_event.is_set():
                self._review_stop_event.wait(self.portfolio_review_interval)
                if not self._review_stop_event.is_set():
                    self.periodic_review()
        t = threading.Thread(target=_timer, daemon=True)
        t.start()
        return t

    async def async_run(self):
        url = f"wss://api.tickflow.org/v1/ws/stream?api_key={self.tickflow_api_key}"
        while not self._stop_event.is_set():
            try:
                async with websockets.connect(url) as ws:
                    with self.status_lock:
                        self.status_info["connected"] = True
                        self.status_info["error"] = None

                    if self.tf_symbols:
                        await ws.send(json.dumps({
                            "op": "subscribe",
                            "channel": "quotes",
                            "symbols": self.tf_symbols
                        }))

                    while not self._stop_event.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                            msg = json.loads(raw)
                            if msg.get("op") == "quotes":
                                with self.status_lock:
                                    self.status_info["last_msg_time"] = datetime.now(TZ_SHANGHAI).strftime("%H:%M:%S")
                                for q in msg.get("data", []):
                                    self.handle_quote(q)
                        except asyncio.TimeoutError:
                            continue
                        except Exception as e:
                            with self.status_lock:
                                self.status_info["error"] = str(e)
                            break
            except Exception as e:
                with self.status_lock:
                    self.status_info["connected"] = False
                    self.status_info["error"] = str(e)

            if not self._stop_event.is_set():
                await asyncio.sleep(5)

        def start_market_risk_monitor(self):
        def _risk_check():
            while not self._risk_stop_event.is_set():
                self._risk_stop_event.wait(self.market_risk_check_interval)
                if not self._risk_stop_event.is_set():
                    self.check_market_risk()
        t = threading.Thread(target=_risk_check, daemon=True)
        t.start()
        return t

    def check_market_risk(self):
        """检查大盘风险，若触发则生成整体减仓建议"""
        try:
            # 使用 REST 接口获取大盘涨跌比（只需轻量请求）
            import numpy as np
            df_all = tf.quotes.get(universes=["CN_Equity_A"], as_dataframe=True)
            if df_all is not None and not df_all.empty:
                up_count = len(df_all[df_all['pct_chg'] > 0])
                down_count = len(df_all[df_all['pct_chg'] < 0])
                self.market_ratio = up_count / max(down_count, 1)
                # 获取跌停家数（主板）
                dt_main = len(df_all[(df_all['board']=='Main') & (df_all['pct_chg']<=-9.8)])
                self.dt_count = dt_main

                # 风险条件：涨跌比 < 0.5 且 跌停家数 > 50（可根据需要调整）
                if self.market_ratio < 0.5 and self.dt_count > 50:
                    self.hedge_advice()
        except Exception as e:
            print(f"大盘风险检查失败: {e}")

    def hedge_advice(self):
        """当触发大盘风险时，对全部持仓生成减仓建议"""
        if not self.llm_client or not self.portfolio_dicts:
            return
        model = self.llm_config.get("LLM_MODEL", "deepseek-chat")
        holdings_text = ""
        for port in self.portfolio_dicts[:]:
            code = port.get('code', '').zfill(6)
            price = self.last_prices.get(code, port.get('buy_price', 0))
            pct = (price - port.get('buy_price', 0)) / port.get('buy_price', 1) * 100
            holdings_text += f"{port.get('name','')}({code}) 成本{port.get('buy_price')} 现价{price} 盈亏{pct:.1f}%\n"

        prompt = f"""当前大盘风险极高：涨跌比{self.market_ratio:.2f}，主板跌停{self.dt_count}家。
请为以下所有持仓给出统一的减仓或清仓建议，并说明理由（50字内）：
{holdings_text}
输出格式：建议整体减仓至X成，理由：..."""
        try:
            resp = self.llm_client.chat.completions.create(
                model=model,
                messages=[{"role":"user","content":prompt}],
                max_tokens=150,
                timeout=15
            )
            advice = resp.choices[0].message.content.strip()
            self.send_dingtalk_alert("🚨 大盘风险对冲建议", advice)
        except Exception as e:
            print(f"对冲建议生成失败: {e}")
            
    def handle_quote(self, data):
        symbol = data.get("symbol", "")
        code = symbol.split('.')[0] if '.' in symbol else symbol
        price = float(data.get("last_price", 0))
        open_price = float(data.get("open", 0))
        if price <= 0:
            return

        # 捕获开盘价（当日首次收到且有效）
        if code not in self.open_prices and open_price > 0:
            self.open_prices[code] = open_price

        name = data.get("ext", {}).get("name", symbol)
        chg = data.get("ext", {}).get("change_pct", 0) * 100

        summary = {
            'symbol': symbol,
            'name': name,
            'code': code,
            'price': price,
            'chg': round(chg, 2),
            'time': datetime.now(TZ_SHANGHAI).strftime('%H:%M:%S')
        }
        with self.status_lock:
            self.latest_quotes.append(summary)
            if len(self.latest_quotes) > self.max_quotes:
                self.latest_quotes.pop(0)
            self._has_quotes = True
            self.status_info["connected"] = True
            self.status_info["error"] = None
        # 更新最新价格（锁外）
        self.last_prices[code] = price

        # ----- 新增：盘中盘口异动检测 -----
        # 获取实时盘口数据（需要 depth 订阅，您之前已订阅了行情和盘口，可复用）
        bid_vol = sum(data.get('bid_volumes', [0])) if 'bid_volumes' in data else 0
        ask_vol = sum(data.get('ask_volumes', [0])) if 'ask_volumes' in data else 0
        # 简单异动条件：买盘总量 > 卖盘总量 * 1.5 且 最新价高于昨收2%以上
        prev_close = data.get('prev_close', 0)
        if bid_vol > ask_vol * 1.5 and prev_close > 0 and (price - prev_close) / prev_close > 0.02:
            # 主动触发买入分析，无需等待买点接近
            for target in self.target_dicts[:]:
                if target.get('code', '').zfill(6) == code:
                    analysis = target.get('analysis', '')
                    if analysis:
                        advice = self.get_comprehensive_advice(
                            {'name': name, 'code': code, 'price': price,
                             'pct_chg': chg, 'bid_vol': bid_vol, 'ask_vol': ask_vol},
                            "盘口异动买入分析",
                            analysis
                        )
                        if advice:
                            text = f"【盘口异动】{name}({code}) 当前价 {price:.2f}，买盘急增，AI建议：{advice}"
                            self.send_dingtalk_alert("⚡ 盘口异动提醒", text)
                    break
                    
        # 1. 买入提醒（分档判断，使用开盘价基准）
        for target in self.target_dicts[:]:
            if target.get('code', '').zfill(6) == code:
                buy_price = target.get('buy_price', 0)
                if buy_price > 0 and abs(price - buy_price) / buy_price <= 0.005:
                    auction_rules = target.get('auction_rules')
                    if auction_rules:
                        open_p = self.open_prices.get(code)
                        if open_p and open_p > 0:
                            # 核心修正：使用开盘价计算涨跌幅，判断高开/平开/低开
                            chg_from_open = (price - open_p) / open_p * 100
                        else:
                            # 若未收到开盘价，暂不触发（避免误判）
                            break

                        if chg_from_open > 2.0:
                            rule = auction_rules.get('high')
                        elif chg_from_open < -2.0:
                            rule = auction_rules.get('low')
                        else:
                            rule = auction_rules.get('flat')

                        if rule and rule.get('action') == 'ignore':
                            break  # AI建议该情形放弃，不推送

                    # 通过规则检查，生成推送
                    open_p_display = open_p if open_p else 0
                    text = f"... （开盘{open_p_display:.2f}，涨{chg_from_open:.1f}%）"
                    analysis = target.get('analysis', '')
                    if analysis:
                        advice = self.get_comprehensive_advice(
                            {'name': name, 'code': code, 'price': price, 'pct_chg': chg},
                            "买入提醒", analysis
                        )
                    else:
                        advice = self.get_ai_advice(
                            {'name': name, 'code': code, 'price': price, 'pct_chg': chg},
                            "买入提醒"
                        )
                    text = f"{name}({code}) 当前价 {price:.2f}，接近建议买点 {buy_price:.2f}（开盘{open_p:.2f}，涨{chg_from_open:.1f}%）"
                    if advice:
                        text += f"\n\nAI综合建议：{advice}"
                    self.send_dingtalk_alert("🔥 买入提醒", text)
                    if not open_p or open_p <= 0:
                        open_p = data.get('prev_close', price)  # 回退到昨收
                break

        # 2. 持仓止损/止盈
        for port in self.portfolio_dicts[:]:
            if port.get('code', '').zfill(6) == code:
                buy_price = port.get('buy_price', 0)
                stop_loss = port.get('stop_loss', buy_price * 0.97)
                profit_target = port.get('profit_target', buy_price * 1.05)

                if price <= stop_loss:
                    advice = self.get_ai_advice({
                        'name': name, 'code': code, 'price': price,
                        'pct_chg': chg, 'cost': buy_price,
                        'profit_pct': (price - buy_price) / buy_price * 100
                    }, "卖出提醒（止损）")
                    text = f"{name}({code}) 当前价 {price:.2f} 已跌破止损价 {stop_loss:.2f}"
                    if advice:
                        text += f"\n\nAI建议：{advice}"
                    self.send_dingtalk_alert("⚠️ 止损提醒", text)
                elif price >= profit_target:
                    advice = self.get_ai_advice({
                        'name': name, 'code': code, 'price': price,
                        'pct_chg': chg, 'cost': buy_price,
                        'profit_pct': (price - buy_price) / buy_price * 100
                    }, "卖出提醒（止盈）")
                    text = f"{name}({code}) 当前价 {price:.2f} 已达到止盈目标 {profit_target:.2f}"
                    if advice:
                        text += f"\n\nAI建议：{advice}"
                    self.send_dingtalk_alert("✅ 止盈提醒", text)
                break

    def get_ai_advice(self, stock_info, action_type):
        if not self.llm_client:
            return None
        model = self.llm_config.get("LLM_MODEL", "deepseek-chat")
        prompt = f"""你是A股超短线交易助手。根据以下实时数据给出操作建议：
股票：{stock_info['name']}({stock_info['code']})
当前价：{stock_info['price']:.2f}，涨幅：{stock_info.get('pct_chg', 0):.2f}%
触发类型：{action_type}
请用一句话给出操作建议（买入/卖出/观望），并给出建议目标价和止损价（精确到分），格式：建议：买入，目标XX.XX，止损YY.YY。"""
        for attempt in range(2):
            try:
                resp = self.llm_client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=1000,
                    timeout=20
                )
                content = resp.choices[0].message.content
                if content and content.strip():
                    return content.strip()
            except Exception as e:
                print(f"AI分析失败 (第{attempt+1}次): {e}")
                time.sleep(1)
        return None

    def get_comprehensive_advice(self, stock_info, action_type, original_analysis):
        if not self.llm_client or not original_analysis:
            return None
        model = self.llm_config.get("LLM_MODEL", "deepseek-chat")
        prompt = f"""你是A股超短线交易决策助手。请结合T日的盘后分析，以及当前实时行情，给出操作建议。

【T日盘后AI分析】（以下为该股昨日的完整分析）
{original_analysis[:1500]}

【当前实时行情】
股票：{stock_info['name']}({stock_info['code']})
最新价：{stock_info['price']:.2f}，涨幅：{stock_info.get('pct_chg', 0):.2f}%
触发类型：{action_type}

请判断：
1. T日分析逻辑是否仍然有效？
2. 当前是否适合买入（或卖出/观望）？
3. 给出建议买入价区间（例如 10.20-10.50）或卖出价区间。
4. 给出明确的止损价。
输出格式（60字内）：[建议动作]，买入/卖出区间 XX.XX-YY.YY，止损 ZZ.ZZ"""
        try:
            resp = self.llm_client.chat.completions.create(
                model=model,
                messages=[{"role":"user","content":prompt}],
                max_tokens=200,
                timeout=20
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            print(f"综合分析失败: {e}")
            return None

    # ------------------------------------------------------------------
    # 以下三个方法用于类内部推送，直接调用模块级函数
    # ------------------------------------------------------------------
    def send_dingtalk_alert(self, title, text):
        """类内部推送，使用实例的 webhook"""
        send_dingtalk_alert(self.dingtalk_webhook, title, text)

    def periodic_review(self):
        if not self.llm_client or not self.portfolio_dicts:
            return
        model = self.llm_config.get("LLM_MODEL", "deepseek-chat")
        market_brief = "请基于当前市场整体环境给出建议。"
        for port in self.portfolio_dicts[:]:
            code = port.get('code', '').zfill(6)
            price = self.last_prices.get(code)
            if price is None:
                continue
            buy_price = port.get('buy_price', 0)
            pct_chg = (price - buy_price) / buy_price * 100 if buy_price else 0
            stop_loss = port.get('stop_loss', buy_price * 0.97)
            profit_target = port.get('profit_target', buy_price * 1.05)
            track = port.get('track', '')

            prompt = f"""你是A股超短线持仓管理助手。请对以下持仓进行盘中评估，并给出操作建议（持有/减仓/卖出/加仓）。
【持仓信息】
股票：{port.get('name', '')}({code})
策略风格：{track}
买入价：{buy_price}，当前价：{price}，盈亏：{pct_chg:.1f}%
止损价：{stop_loss}，止盈目标：{profit_target}
{market_brief}

请用一句话给出操作建议和理由（20字内），格式：建议：持有，理由：...
注意：本建议仅供参考，不构成最终交易指令。"""
            try:
                resp = self.llm_client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=100,
                    timeout=15
                )
                advice = resp.choices[0].message.content.strip()
            except Exception as e:
                advice = f"评估失败：{e}"

            text = f"【持仓定期评估】{port.get('name', '')}({code}) 当前价 {price:.2f}，盈亏 {pct_chg:.1f}%\nAI建议：{advice}"
            self.send_dingtalk_alert("📊 持仓跟踪", text)
            time.sleep(0.5)

    def stop(self):
        self._stop_event.set()
        self._review_stop_event.set()
        self._risk_stop_event.set()   # 新增
