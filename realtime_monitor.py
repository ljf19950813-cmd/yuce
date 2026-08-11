import asyncio
import json
import time
import threading
from datetime import datetime
import requests
import websockets
from openai import OpenAI


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
        self.last_prices = {}        # 新增：code -> price
        self.portfolio_review_interval = 30 * 60  # 30分钟
        self._review_stop_event = threading.Event()

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
        self._has_quotes = False

    def run(self):
        # 启动定时评估线程
        self.review_thread = self.start_review_timer()
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

                    # 重新订阅（断线重连后必须重发）
                    if self.tf_symbols:
                        await ws.send(json.dumps({
                            "op": "subscribe",
                            "channel": "quotes",
                            "symbols": self.tf_symbols
                        }))

                    # 消息处理循环
                    while not self._stop_event.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                            msg = json.loads(raw)
                            if msg.get("op") == "quotes":
                                with self.status_lock:
                                    self.status_info["last_msg_time"] = datetime.now().strftime("%H:%M:%S")
                                for q in msg.get("data", []):
                                    self.handle_quote(q)
                        except asyncio.TimeoutError:
                            continue
                        except Exception as e:
                            with self.status_lock:
                                self.status_info["error"] = str(e)
                            break   # 连接异常，退出内层循环，准备重连
            except Exception as e:
                with self.status_lock:
                    self.status_info["connected"] = False
                    self.status_info["error"] = str(e)

            # 等待重连（若未主动停止）
            if not self._stop_event.is_set():
                await asyncio.sleep(5)   # 5 秒后自动重试

    def handle_quote(self, data):
        # ... 原有解析代码不变 ...
        symbol = data.get("symbol", "")
        code = symbol.split('.')[0] if '.' in symbol else symbol
        price = float(data.get("last_price", 0))
        if price <= 0:
            return

        name = data.get("ext", {}).get("name", symbol)
        chg = data.get("ext", {}).get("change_pct", 0) * 100

        summary = {
            'symbol': symbol,
            'name': name,
            'code': code,
            'price': price,
            'chg': round(chg, 2),
            'time': datetime.now().strftime('%H:%M:%S')
        }
        with self.status_lock:
            self.latest_quotes.append(summary)
            if len(self.latest_quotes) > self.max_quotes:
                self.latest_quotes.pop(0)
            # 强制标记为已连接，消除前端延迟
            self._has_quotes = True
            self.status_info["connected"] = True
            self.status_info["error"] = None
        with self.status_lock:
            # ... 原有 latest_quotes 处理 ...
        # 新增：更新最新价格
        self.last_prices[code] = price

        # 1. 买入提醒（分档判断）
        for target in self.target_dicts[:]:
            if target.get('code', '').zfill(6) == code:
                buy_price = target.get('buy_price', 0)
                if buy_price > 0 and abs(price - buy_price) / buy_price <= 0.005:
                    # 获取三档规则
                    auction_rules = target.get('auction_rules')
                    if auction_rules:
                        prev_close = data.get('prev_close', 0)
                        if prev_close > 0:
                            chg_pct = (price - prev_close) / prev_close * 100
                            if chg_pct > 2.0:
                                rule = auction_rules.get('high')
                            elif chg_pct < -2.0:
                                rule = auction_rules.get('low')
                            else:
                                rule = auction_rules.get('flat')
                            if rule and rule.get('action') == 'ignore':
                                # AI建议该情形放弃，不推送
                                break
                    # 通过规则检查，生成推送
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
                    text = f"{name}({code}) 当前价 {price:.2f}，接近建议买点 {buy_price:.2f}"
                    if advice:
                        text += f"\n\nAI综合建议：{advice}"
                    self.send_dingtalk_alert("🔥 买入提醒", text)
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
        for attempt in range(2):   # 重试2次
            try:
                resp = self.llm_client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=1000,
                    timeout=20   # 增加超时到20秒
                )
                content = resp.choices[0].message.content
                if content and content.strip():
                    return content.strip()
            except Exception as e:
                print(f"AI分析失败 (第{attempt+1}次): {e}")
                time.sleep(1)   # 等1秒重试
        return None   # 两次都失败则返回None
        
    def get_comprehensive_advice(self, stock_info, action_type, original_analysis):
        """基于原始分析和实时行情给出综合建议"""
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
            
    def send_dingtalk_alert(self, title, text):
        headers = {"Content-Type": "application/json"}
        data = {
            "msgtype": "markdown",
            "markdown": {"title": title, "text": f"### {title}\n{text}"}
        }
        try:
            requests.post(self.dingtalk_webhook, headers=headers, json=data)
        except Exception as e:
            print(f"钉钉推送失败: {e}")
    def periodic_review(self):
        """定时对所有持仓进行重新评估，并推送到钉钉"""
        if not self.llm_client or not self.portfolio_dicts:
            return
        
        model = self.llm_config.get("LLM_MODEL", "deepseek-chat")
        # 获取简要大盘描述（可以用涨跌家数，此处简化）
        market_brief = ""
        try:
            # 尝试从 status_info 中获取大盘涨跌比（如果之前有更新）
            # 这里简单写死一个描述，实际可以从外部传入或使用 REST 接口
            market_brief = "请基于当前市场整体环境给出建议。"
        except:
            pass

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
            time.sleep(0.5)  # 控制推送频率
    
    def stop(self):
        self._stop_event.set()
        self._review_stop_event.set()  # 新增
