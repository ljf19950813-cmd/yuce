import asyncio
import json
import threading
from datetime import datetime
import requests
import websockets
from openai import OpenAI

class RealtimeMonitor(threading.Thread):
    def __init__(self, target_dicts, portfolio_dicts, 
                 tickflow_api_key, dingtalk_webhook,   # ← 接收密钥
                 llm_client=None, llm_config=None):
        super().__init__(daemon=True)
        self.target_dicts = target_dicts
        self.portfolio_dicts = portfolio_dicts
        self.tickflow_api_key = tickflow_api_key      # 存储
        self.dingtalk_webhook = dingtalk_webhook
        self.llm_client = llm_client
        self.llm_config = llm_config or {}
        self.symbols = list(set([d['code'] for d in target_dicts] + [d['code'] for d in portfolio_dicts]))
        self.tf_symbols = [f"{code}.{'SH' if code.startswith('6') else 'SZ'}" for code in self.symbols]
        self._stop_event = threading.Event()
        # ... 其余代码不变 ...
        # 开盘确认相关
        self.buy_confirm_state = {}
        self.MARKET_OPEN_HOUR = 9
        self.MARKET_OPEN_MINUTE = 30
        self.CONFIRM_WINDOW_SECONDS = 5 * 60

    def run(self):
        asyncio.run(self.async_run())

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

    async def async_run(self):
        url = f"wss://api.tickflow.org/v1/ws/stream?api_key={self.tickflow_api_key}"
        async with websockets.connect(url) as ws:
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
                        for q in msg.get("data", []):
                            self.handle_quote(q)
                except asyncio.TimeoutError:
                    continue
                except Exception as e:
                    print(f"WebSocket 错误: {e}")
                    break

    def handle_quote(self, data):
        symbol = data["symbol"]
        code = symbol.split('.')[0]
        price = float(data.get("last_price", 0))
        if price <= 0:
            return
        name = data.get("ext", {}).get("name", symbol)
        chg = data.get("ext", {}).get("change_pct", 0) * 100

        # 1. 买入确认检查（开盘时段）
        for target in self.target_dicts[:]:
            if target['code'] == code:
                if not self.check_morning_confirm(code, price, target):
                    continue   # 被放弃，不再检查买入提醒

        # 2. 买入提醒
        for target in self.target_dicts[:]:
            if target['code'] == code and target.get('buy_price', 0) > 0:
                if abs(price - target['buy_price']) / target['buy_price'] <= 0.005:
                    advice = self.get_ai_advice({
                        'name': name, 'code': code, 'price': price,
                        'pct_chg': chg,
                        'bid_vol': data.get('bid_vol', 0), 'ask_vol': data.get('ask_vol', 0)
                    }, "买入提醒")
                    text = f"{name}({code}) 当前价 {price:.2f}，接近建议买点 {target['buy_price']:.2f}"
                    if advice:
                        text += f"\n\nAI建议：{advice}"
                    send_dingtalk_alert("🔥 买入提醒", text)
                break

        # 3. 持仓止损/止盈
        for port in self.portfolio_dicts[:]:
            if port['code'] == code:
                if price <= port.get('stop_loss', 0):
                    advice = self.get_ai_advice({
                        'name': name, 'code': code, 'price': price,
                        'pct_chg': chg, 'cost': port['buy_price'],
                        'profit_pct': (price - port['buy_price'])/port['buy_price']*100
                    }, "卖出提醒（止损）")
                    text = f"{name}({code}) 当前价 {price:.2f} 已跌破止损价 {port['stop_loss']:.2f}"
                    if advice:
                        text += f"\n\nAI建议：{advice}"
                    send_dingtalk_alert("⚠️ 止损提醒", text)
                elif price >= port.get('profit_target', 0):
                    advice = self.get_ai_advice({
                        'name': name, 'code': code, 'price': price,
                        'pct_chg': chg, 'cost': port['buy_price'],
                        'profit_pct': (price - port['buy_price'])/port['buy_price']*100
                    }, "卖出提醒（止盈）")
                    text = f"{name}({code}) 当前价 {price:.2f} 已达到止盈目标 {port['profit_target']:.2f}"
                    if advice:
                        text += f"\n\nAI建议：{advice}"
                    send_dingtalk_alert("✅ 止盈提醒", text)

                # 4. 妖股炸板保护（需持仓数据中有 prev_close 和 track）
                if port.get('track') == '妖股':
                    prev_close = port.get('prev_close')
                    if prev_close and prev_close > 0:
                        limit_up = prev_close * 1.1
                        if price < limit_up * 0.98:
                            send_dingtalk_alert(
                                "🚨 妖股炸板提醒",
                                f"{port['name']}({code}) 炸板！当前价 {price:.2f}，建议立即清仓"
                            )
                    # 移动止盈
                    if price > port['buy_price'] * 1.05:
                        port['stop_loss'] = port['buy_price']
                break

    def check_morning_confirm(self, code, price, target):
        now = datetime.now()
        # 非开盘时段直接通过
        if now.hour < self.MARKET_OPEN_HOUR or (now.hour == self.MARKET_OPEN_HOUR and now.minute < self.MARKET_OPEN_MINUTE):
            return True
        if now.hour > self.MARKET_OPEN_HOUR:
            return True

        # 初始化状态
        if code not in self.buy_confirm_state:
            self.buy_confirm_state[code] = {'start_time': now, 'buy_ready': False}

        state = self.buy_confirm_state[code]

        # 低开检查
        if not state.get('low_checked'):
            low_tolerance = target.get('low_tolerance', 0.025)
            buy_price = target.get('buy_price', 0)
            if buy_price > 0 and price < buy_price * (1 - low_tolerance):
                send_dingtalk_alert(
                    "⛔ 买入放弃提醒",
                    f"{target['name']}({code}) 低开超过限制，放弃买入计划"
                )
                self.target_dicts.remove(target)
                return False
            state['low_checked'] = True

        # 5分钟站稳检查
        elapsed = (now - state['start_time']).total_seconds()
        if elapsed >= self.CONFIRM_WINDOW_SECONDS:
            if price < target['buy_price'] * 0.995:
                send_dingtalk_alert(
                    "⛔ 买入放弃提醒",
                    f"{target['name']}({code}) 开盘5分钟未站稳买点，放弃"
                )
                self.target_dicts.remove(target)
                return False
            else:
                state['buy_ready'] = True
        return True

    def get_ai_advice(self, stock_info, action_type):
        if not self.llm_client:
            return None
        model = self.llm_config.get("LLM_MODEL", "deepseek-chat")
        prompt = f"""你是A股超短线交易助手。根据以下实时数据给出操作建议：
股票：{stock_info['name']}({stock_info['code']})
当前价：{stock_info['price']:.2f}，涨幅：{stock_info.get('pct_chg', 0):.2f}%
触发类型：{action_type}
请用一句话给出操作建议（买入/卖出/观望），并给出建议目标价和止损价（精确到分），格式：建议：买入，目标XX.XX，止损YY.YY。"""
        try:
            resp = self.llm_client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=100,
                timeout=10
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            print(f"AI分析失败: {e}")
            return None

    def stop(self):
        self._stop_event.set()
