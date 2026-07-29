"""
Telegram 交互式命令 bot（asyncio 长轮询 getUpdates）。

通过 Telegram 命令完成市场解析、下单、撤单、平仓、停止、删除、参数配置等
全部操作，与 Chrome 扩展是并列的操作入口，共享同一 RuntimeManager。

命令一览：
  /help                       显示帮助
  /status                     显示所有策略快照
  /parse <url|slug>           解析市场，返回 market 列表
  /markets <url|slug>         同 /parse
  /order <token_id> <entry> <exit> <size> [tp] [sl]
                              下单（tp/sl 可选，off 关闭）
  /cancel <token_id>          撤该 token 的所有挂单
  /close <token_id>           平仓（按 best_bid 市价卖）
  /stop <token_id>            停止策略（撤单 + 禁用监控，保留记录）
  /delete <token_id>          删除策略记录（不撤单不平仓）
  /price <token_id> <0-1>     修改某策略的买入价
  /exit_price <token_id> <0-1> 修改卖出价
  /amount <token_id> <n>      修改下单份额
  /takeprofit <token_id> <pct|off>  设置止盈
  /stoploss <token_id> <pct|off>    设置止损
  /list                       列出所有策略的 token_id（方便复制）

安全：
  - TELEGRAM_ALLOWED_USER_IDS 白名单控制，留空则允许任何人
  - 所有写操作走 RuntimeManager._order_lock 串行化
"""

from __future__ import annotations

import asyncio
import json
import urllib.parse
from typing import Any

import httpx
import trading
from config import StrategyConfig
from market_setup import extract_slug, fetch_markets, fetch_token_price, build_label
from runtime import RuntimeManager


TELEGRAM_API = "https://api.telegram.org"


class TelegramCommandBot:
    """长轮询 getUpdates，解析命令并执行。"""

    def __init__(
        self,
        token: str,
        manager: RuntimeManager,
        client,
        config,
        allowed_user_ids: list[int] | None = None,
        timeout: float = 30.0,
    ):
        self._token = token
        self._manager = manager
        self._client = client
        self._config = config
        self._allowed = set(allowed_user_ids or [])
        self._timeout = timeout
        self._offset = 0
        self._stop = asyncio.Event()
        self._base_url = f"{TELEGRAM_API}/bot{token}"

    async def run(self) -> None:
        """主循环：长轮询 getUpdates。"""
        if not self._token:
            print("[telegram_bot] no token, skipping")
            return

        print(
            f"[telegram_bot] polling started "
            f"(allowed_users={len(self._allowed) or 'any'})"
        )
        while not self._stop.is_set():
            try:
                updates = await self._get_updates()
                for update in updates:
                    self._offset = update["update_id"] + 1
                    await self._handle_update(update)
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                print(f"[telegram_bot] poll error: {exc}")
                await asyncio.sleep(3)

        print("[telegram_bot] polling stopped")

    def stop(self) -> None:
        self._stop.set()

    # --- Telegram API ----------------------------------------------------- #

    async def _get_updates(self) -> list[dict[str, Any]]:
        url = f"{self._base_url}/getUpdates"
        payload = {"offset": self._offset, "timeout": int(self._timeout)}
        async with httpx.AsyncClient(timeout=self._timeout + 10) as ac:
            resp = await ac.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            return data.get("result", []) if data.get("ok") else []

    async def _send_message(self, chat_id: int | str, text: str, parse_mode: str = "HTML") -> None:
        url = f"{self._base_url}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        try:
            async with httpx.AsyncClient(timeout=15) as ac:
                await ac.post(url, json=payload)
        except Exception as exc:  # noqa: BLE001
            print(f"[telegram_bot] sendMessage failed: {exc}")

    # --- 权限校验 --------------------------------------------------------- #

    def _is_allowed(self, user_id: int) -> bool:
        if not self._allowed:
            return True
        return user_id in self._allowed

    # --- 命令分发 --------------------------------------------------------- #

    async def _handle_update(self, update: dict[str, Any]) -> None:
        msg = update.get("message")
        if not msg:
            return
        chat_id = msg.get("chat", {}).get("id")
        text = (msg.get("text") or "").strip()
        user = msg.get("from", {})
        user_id = user.get("id")

        if not chat_id or not text:
            return
        if not self._is_allowed(user_id):
            await self._send_message(chat_id, "⛔ 未授权")
            return

        # 解析命令和参数
        parts = text.split()
        cmd = parts[0].lower()
        args = parts[1:]

        try:
            handler = self._dispatch.get(cmd)
            if handler is None:
                await self._send_message(
                    chat_id,
                    f"❓ 未知命令: <code>{cmd}</code>\n发送 /help 查看帮助",
                )
                return
            await handler(self, chat_id, args)
        except Exception as exc:  # noqa: BLE001
            await self._send_message(chat_id, f"❌ 执行失败: <code>{exc}</code>")

    # --- 命令实现 --------------------------------------------------------- #

    async def cmd_help(self, chat_id: int | str, args: list[str]) -> None:
        text = (
            "<b>Polymarket 交易 bot 命令</b>\n\n"
            "<b>查询</b>\n"
            "/status — 所有策略快照\n"
            "/list — 列出所有 token_id\n"
            "/parse &lt;url|slug&gt; — 解析市场\n\n"
            "<b>下单与管理</b>\n"
            "/order &lt;token_id&gt; &lt;entry&gt; &lt;exit&gt; &lt;size&gt; [tp] [sl]\n"
            "/cancel &lt;token_id&gt; — 撤所有挂单\n"
            "/close &lt;token_id&gt; — 平仓（市价）\n"
            "/stop &lt;token_id&gt; — 停止策略\n"
            "/delete &lt;token_id&gt; — 删除策略记录\n\n"
            "<b>参数修改</b>\n"
            "/price &lt;token_id&gt; &lt;0-1&gt;\n"
            "/exit_price &lt;token_id&gt; &lt;0-1&gt;\n"
            "/amount &lt;token_id&gt; &lt;n&gt;\n"
            "/takeprofit &lt;token_id&gt; &lt;pct|off&gt;\n"
            "/stoploss &lt;token_id&gt; &lt;pct|off&gt;\n\n"
            "<i>tp/sl 用小数表示，如 0.10 = 10%；off 关闭</i>"
        )
        await self._send_message(chat_id, text)

    async def cmd_status(self, chat_id: int | str, args: list[str]) -> None:
        snapshots = self._manager.status_snapshot()
        if not snapshots:
            await self._send_message(chat_id, "📭 当前无策略")
            return

        lines = [f"<b>策略数: {len(snapshots)}</b>"]
        for i, s in enumerate(snapshots, 1):
            label = s.get("label", "?")
            state = s.get("state", "?")
            cycle = s.get("cycle", 0)
            entry = s.get("entry_price")
            exit_p = s.get("exit_price")
            size = s.get("share_amount")
            pos_size = s.get("position_size", 0)
            avg = s.get("avg_price")
            tp = s.get("take_profit_pct")
            sl = s.get("stop_loss_pct")
            enabled = s.get("enabled", True)

            line = (
                f"\n<b>[{i}] {label}</b>\n"
                f"  状态: {state} | cyc {cycle} | {'启用' if enabled else '已停止'}\n"
                f"  entry={entry} exit={exit_p} size={size}\n"
            )
            if pos_size:
                line += f"  持仓: {pos_size} @ {avg}\n"
            if tp is not None or sl is not None:
                line += f"  TP={tp} SL={sl}\n"
            line += f"  token: <code>{s.get('token_id', '')[:16]}...</code>"
            lines.append(line)

        await self._send_message(chat_id, "\n".join(lines))

    async def cmd_list(self, chat_id: int | str, args: list[str]) -> None:
        snapshots = self._manager.status_snapshot()
        if not snapshots:
            await self._send_message(chat_id, "📭 当前无策略")
            return
        lines = ["<b>token_id 列表（点击复制）</b>"]
        for i, s in enumerate(snapshots, 1):
            tid = s.get("token_id", "")
            label = s.get("label", "?")
            lines.append(f"\n[{i}] {label}\n<code>{tid}</code>")
        await self._send_message(chat_id, "\n".join(lines))

    async def cmd_parse(self, chat_id: int | str, args: list[str]) -> None:
        if not args:
            await self._send_message(chat_id, "用法: /parse &lt;url|slug&gt;")
            return
        raw = " ".join(args)
        slug = extract_slug(raw)
        if not slug:
            await self._send_message(chat_id, "❌ 无法解析 slug")
            return

        await self._send_message(chat_id, f"⏳ 正在解析 <code>{slug}</code> ...")
        markets = await asyncio.to_thread(fetch_markets, slug)
        if not markets:
            await self._send_message(chat_id, "❌ 未找到市场")
            return

        lines = [f"<b>找到 {len(markets)} 个市场</b>"]
        for i, m in enumerate(markets, 1):
            q = m.get("question", "?")
            outcomes = m.get("outcomes", [])
            tokens = m.get("clob_token_ids", [])
            closed = m.get("closed", False)
            accepting = m.get("accepting", True)
            status_flag = "🔴" if closed else ("🟡" if not accepting else "🟢")

            line = f"\n<b>{status_flag} [{i}] {q}</b>"
            if not accepting:
                line += " (不接受下单)"
            for j, (oc, tid) in enumerate(zip(outcomes, tokens)):
                line += f"\n  {j+1}. {oc}\n     <code>{tid}</code>"
            lines.append(line)

        lines.append("\n\n<i>用 /order &lt;token_id&gt; ... 下单</i>")
        await self._send_message(chat_id, "\n".join(lines))

    async def cmd_order(self, chat_id: int | str, args: list[str]) -> None:
        if len(args) < 4:
            await self._send_message(
                chat_id,
                "用法: /order &lt;token_id&gt; &lt;entry&gt; &lt;exit&gt; &lt;size&gt; [tp] [sl]\n"
                "示例: /order 72123...abc 0.42 0.55 100 0.10 0.05",
            )
            return

        token_id = args[0]
        try:
            entry = float(args[1])
            exit_price = float(args[2])
            size = float(args[3])
        except ValueError:
            await self._send_message(chat_id, "❌ 价格/数量格式错误")
            return

        if not (0 < entry < 1) or not (0 < exit_price < 1) or size <= 0:
            await self._send_message(chat_id, "❌ 价格需在 (0,1)，数量需 > 0")
            return

        tp = self._parse_pct(args[4]) if len(args) >= 5 else None
        sl = self._parse_pct(args[5]) if len(args) >= 6 else None

        # 查 market 信息构造 label
        label = await self._build_label_for_token(token_id)
        if not label:
            label = f"TG策略 [{token_id[:8]}]"

        sc = StrategyConfig(
            token_id=token_id,
            label=label,
            entry_price=entry,
            exit_price=exit_price,
            share_amount=size,
            take_profit_pct=tp,
            stop_loss_pct=sl,
            trading_enabled=True,
        )

        # 检查重复
        if self._manager.find_runtime(token_id):
            await self._send_message(chat_id, "⚠️ 该 token 已有策略，请先 /delete 或 /stop")
            return

        await self._send_message(
            chat_id,
            f"⏳ 正在下单: {label}\nentry={entry} exit={exit_price} size={size} tp={tp} sl={sl}",
        )

        try:
            async with self._manager._order_lock:
                # 检查余额
                ok, msg = await self._check_balance_and_place(sc)
                if not ok:
                    await self._send_message(chat_id, f"❌ 下单失败: {msg}")
                    return

            rt = self._manager.add_strategy(sc)
            task = asyncio.create_task(rt.run(), name=f"strategy:{rt.label}")
            self._manager._tasks.append(task)

            await self._send_message(
                chat_id,
                f"✅ 已下单并启动策略: {label}\n"
                f"token: <code>{token_id[:16]}...</code>",
            )
        except Exception as exc:  # noqa: BLE001
            await self._send_message(chat_id, f"❌ 下单异常: {exc}")

    async def cmd_cancel(self, chat_id: int | str, args: list[str]) -> None:
        if not args:
            await self._send_message(chat_id, "用法: /cancel &lt;token_id&gt;")
            return
        token_id = args[0]
        rt = self._manager.find_runtime(token_id)
        if not rt:
            await self._send_message(chat_id, "❌ 未找到该 token 的策略")
            return

        await self._send_message(chat_id, "⏳ 正在撤单...")
        try:
            async with self._manager._order_lock:
                count = await asyncio.to_thread(
                    trading.cancel_open_orders_for_token, self._client, token_id
                )
            # 清状态标志
            rt._has_open_buy_order = False
            rt._has_open_sell_order = False
            rt._entry_attempted = False
            await self._send_message(chat_id, f"✅ 已撤单 (count={count})")
        except Exception as exc:  # noqa: BLE001
            await self._send_message(chat_id, f"❌ 撤单失败: {exc}")

    async def cmd_close(self, chat_id: int | str, args: list[str]) -> None:
        if not args:
            await self._send_message(chat_id, "用法: /close &lt;token_id&gt;")
            return
        token_id = args[0]
        rt = self._manager.find_runtime(token_id)
        if not rt:
            await self._send_message(chat_id, "❌ 未找到该 token 的策略")
            return

        await self._send_message(chat_id, "⏳ 正在平仓（市价）...")
        try:
            async with self._manager._order_lock:
                # 撤所有卖单
                await asyncio.to_thread(
                    trading.cancel_open_orders_for_token, self._client, token_id, "SELL"
                )
                rt._has_open_sell_order = False
                # 按 best_bid 市价卖
                bid, _ = await asyncio.to_thread(fetch_token_price, token_id)
                if bid is None:
                    await self._send_message(chat_id, "❌ 无法获取 best_bid")
                    return
                size = rt._position.size
                if size <= 0:
                    await self._send_message(chat_id, "❌ 无持仓可平")
                    return
                # trading.exit_position(client, token_id, side, amount, price)
                # side="SELL"，amount=持仓size，price=best_bid
                await asyncio.to_thread(
                    trading.exit_position,
                    self._client, token_id, "SELL", size, bid,
                )
            await self._send_message(
                chat_id,
                f"✅ 已平仓: size={size} @ {bid}",
            )
        except Exception as exc:  # noqa: BLE001
            await self._send_message(chat_id, f"❌ 平仓失败: {exc}")

    async def cmd_stop(self, chat_id: int | str, args: list[str]) -> None:
        if not args:
            await self._send_message(chat_id, "用法: /stop &lt;token_id&gt;")
            return
        token_id = args[0]
        rt = self._manager.find_runtime(token_id)
        if not rt:
            await self._send_message(chat_id, "❌ 未找到该 token 的策略")
            return

        try:
            async with self._manager._order_lock:
                await asyncio.to_thread(
                    trading.cancel_open_orders_for_token, self._client, token_id
                )
            rt._cfg.enabled = False
            rt._has_open_buy_order = False
            rt._has_open_sell_order = False
            rt._entry_attempted = False
            rt.stop()
            await self._send_message(
                chat_id,
                f"✅ 已停止策略: {rt.label}\n（记录保留，用 /delete 彻底删除）",
            )
        except Exception as exc:  # noqa: BLE001
            await self._send_message(chat_id, f"❌ 停止失败: {exc}")

    async def cmd_delete(self, chat_id: int | str, args: list[str]) -> None:
        if not args:
            await self._send_message(chat_id, "用法: /delete &lt;token_id&gt;")
            return
        token_id = args[0]
        ok = self._manager.remove_strategy(token_id)
        if ok:
            await self._send_message(chat_id, "✅ 已删除策略记录（不撤单不平仓）")
        else:
            await self._send_message(chat_id, "❌ 未找到该 token 的策略")

    async def cmd_price(self, chat_id: int | str, args: list[str]) -> None:
        await self._set_param(chat_id, args, "entry_price", "买入价", 0, 1)

    async def cmd_exit_price(self, chat_id: int | str, args: list[str]) -> None:
        await self._set_param(chat_id, args, "exit_price", "卖出价", 0, 1)

    async def cmd_amount(self, chat_id: int | str, args: list[str]) -> None:
        await self._set_param(chat_id, args, "share_amount", "份额", 0, None)

    async def cmd_takeprofit(self, chat_id: int | str, args: list[str]) -> None:
        await self._set_pct_param(chat_id, args, "take_profit_pct", "止盈")

    async def cmd_stoploss(self, chat_id: int | str, args: list[str]) -> None:
        await self._set_pct_param(chat_id, args, "stop_loss_pct", "止损")

    # --- 辅助方法 --------------------------------------------------------- #

    async def _set_param(
        self,
        chat_id: int | str,
        args: list[str],
        field: str,
        label_zh: str,
        min_val: float,
        max_val: float | None,
    ) -> None:
        if len(args) < 2:
            await self._send_message(
                chat_id, f"用法: /{label_zh} &lt;token_id&gt; &lt;值&gt;"
            )
            return
        token_id = args[0]
        try:
            val = float(args[1])
        except ValueError:
            await self._send_message(chat_id, "❌ 数值格式错误")
            return
        if val <= min_val or (max_val is not None and val >= max_val):
            await self._send_message(chat_id, f"❌ {label_zh} 需在 ({min_val}, {max_val})")
            return
        rt = self._manager.find_runtime(token_id)
        if not rt:
            await self._send_message(chat_id, "❌ 未找到该 token 的策略")
            return
        setattr(rt._cfg, field, val)
        await self._send_message(chat_id, f"✅ {label_zh} 已改为 {val}")

    async def _set_pct_param(
        self,
        chat_id: int | str,
        args: list[str],
        field: str,
        label_zh: str,
    ) -> None:
        if len(args) < 2:
            await self._send_message(
                chat_id, f"用法: /{label_zh} &lt;token_id&gt; &lt;pct|off&gt;"
            )
            return
        token_id = args[0]
        raw = args[1].lower()
        if raw == "off":
            val = None
        else:
            try:
                val = float(raw)
                if not (0 < val < 1):
                    await self._send_message(chat_id, "❌ pct 需在 (0, 1)，如 0.10 = 10%")
                    return
            except ValueError:
                await self._send_message(chat_id, "❌ 格式错误，用 off 或 0.10")
                return
        rt = self._manager.find_runtime(token_id)
        if not rt:
            await self._send_message(chat_id, "❌ 未找到该 token 的策略")
            return
        setattr(rt._cfg, field, val)
        await self._send_message(chat_id, f"✅ {label_zh} 已改为 {val if val else 'off'}")

    def _parse_pct(self, raw: str) -> float | None:
        raw = raw.lower()
        if raw == "off":
            return None
        try:
            v = float(raw)
            return v if 0 < v < 1 else None
        except ValueError:
            return None

    async def _build_label_for_token(self, token_id: str) -> str:
        """尝试从 Polymarket 反查 token 对应的 market/outcome 构造 label。"""
        try:
            async with httpx.AsyncClient(timeout=10) as ac:
                resp = await ac.get(
                    "https://gamma-api.polymarket.com/markets",
                    params={"clob_token_ids": token_id},
                )
                resp.raise_for_status()
                data = resp.json()
                if isinstance(data, list) and data:
                    m = data[0]
                    question = m.get("question", "")
                    outcomes_raw = m.get("outcomes", "[]")
                    if isinstance(outcomes_raw, str):
                        outcomes = json.loads(outcomes_raw)
                    else:
                        outcomes = outcomes_raw or []
                    tokens_raw = m.get("clobTokenIds", "[]")
                    if isinstance(tokens_raw, str):
                        tokens = json.loads(tokens_raw)
                    else:
                        tokens = tokens_raw or []
                    for oc, tid in zip(outcomes, tokens):
                        if tid == token_id:
                            return build_label(
                                {"question": question, "event_title": question},
                                oc,
                            )
                    return question or f"token_{token_id[:8]}"
        except Exception:  # noqa: BLE001
            pass
        return ""

    async def _check_balance_and_place(self, sc: StrategyConfig) -> tuple[bool, str]:
        """检查余额并下单，返回 (ok, msg)。"""
        try:
            # trading.enter_position(client, token_id, side, amount, price)
            # side="BUY"，amount=share_amount，price=entry_price
            result = await asyncio.to_thread(
                trading.enter_position,
                self._client,
                sc.token_id,
                "BUY",
                sc.share_amount,
                sc.entry_price,
            )
            if result:
                return True, f"order={result}"
            return False, "下单返回空结果"
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    # --- 命令分发表 ------------------------------------------------------- #
    _dispatch: dict[str, Any] = {}  # 类加载后填充


# 填充命令分发表
TelegramCommandBot._dispatch = {
    "/help": TelegramCommandBot.cmd_help,
    "/start": TelegramCommandBot.cmd_help,
    "/status": TelegramCommandBot.cmd_status,
    "/list": TelegramCommandBot.cmd_list,
    "/parse": TelegramCommandBot.cmd_parse,
    "/markets": TelegramCommandBot.cmd_parse,
    "/order": TelegramCommandBot.cmd_order,
    "/cancel": TelegramCommandBot.cmd_cancel,
    "/close": TelegramCommandBot.cmd_close,
    "/stop": TelegramCommandBot.cmd_stop,
    "/delete": TelegramCommandBot.cmd_delete,
    "/price": TelegramCommandBot.cmd_price,
    "/exit_price": TelegramCommandBot.cmd_exit_price,
    "/amount": TelegramCommandBot.cmd_amount,
    "/takeprofit": TelegramCommandBot.cmd_takeprofit,
    "/stoploss": TelegramCommandBot.cmd_stoploss,
}
