"""
Telegram 交互式命令 bot（asyncio 长轮询 getUpdates）。

通过 Telegram 完成市场解析、下单、撤单、平仓、停止、删除、参数配置等
全部操作，与 Chrome 扩展是并列的操作入口，共享同一 RuntimeManager。

命令一览：
  /menu /start /help          主菜单（内联键盘按钮集，含下单向导入口）
  /status                     显示所有策略快照
  /parse <url|slug>           解析市场，返回 market 列表
  /markets <url|slug>         同 /parse
  /order                      引导式下单向导（贴链接 → 按钮选择 → 确认）
  /order <token_id> <entry> <exit> <size> [tp] [sl]
                              一行式快捷下单（tp/sl 可选，off 关闭）
  /cancel <token_id>          撤该 token 的所有挂单
  /close <token_id>           平仓（带确认，按 best_bid 市价卖）
  /stop <token_id>            停止策略（撤单 + 禁用监控，保留记录）
  /delete <token_id>          删除策略记录（不撤单不平仓）
  /price <token_id> <0-1>     修改某策略的买入价
  /exit_price <token_id> <0-1> 修改卖出价
  /amount <token_id> <n>      修改下单份额
  /takeprofit <token_id> <pct|off>  设置止盈
  /stoploss <token_id> <pct|off>    设置止损
  /list                       列出所有策略的 token_id（方便复制）

安全（fail-closed）：
  - TELEGRAM_ALLOWED_USER_IDS 白名单；留空 = 拒绝所有用户（不再放行任何人）
  - 未授权时 bot 会回复你的 user_id，方便首次配置
  - 所有写操作走 RuntimeManager._order_lock 串行化
  - 下单/平仓需在预览卡片上确认后才执行
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

import httpx
import trading
from config import StrategyConfig
from market_setup import (
    MarketFetchError,
    build_label,
    extract_slug,
    fetch_markets,
    fetch_token_price,
)
from runtime import RuntimeManager


TELEGRAM_API = "https://api.telegram.org"

logger = logging.getLogger("telegram_bot")

# 外部 API（gamma）解析超时上限：DNS/代理异常时防止向导无限卡住
FETCH_TIMEOUT = 45.0


def _html_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


@dataclass
class _WizardSession:
    """一次交互会话（按 chat_id 隔离，纯内存，无操作超时作废）。

    order 为下单向导草稿；close/cancel/stop 列表供内联按钮按索引选择
    （callback_data 有 64 字节上限，不能直接塞 token_id）。
    """

    order: dict[str, Any] | None = None
    close_list: list[dict[str, Any]] = field(default_factory=list)
    cancel_list: list[dict[str, Any]] = field(default_factory=list)
    stop_list: list[dict[str, Any]] = field(default_factory=list)
    ts: float = field(default_factory=time.time)


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
        self._sessions: dict[int, _WizardSession] = {}
        self._draft_ttl = 600.0  # 会话/草稿无操作过期时间（秒）

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
                logger.error("getUpdates 轮询异常: %s", exc, exc_info=True)
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

    async def _send_message(
        self,
        chat_id: int | str,
        text: str,
        parse_mode: str = "HTML",
        reply_markup: dict | None = None,
    ) -> None:
        url = f"{self._base_url}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        try:
            async with httpx.AsyncClient(timeout=15) as ac:
                await ac.post(url, json=payload)
        except Exception as exc:  # noqa: BLE001
            print(f"[telegram_bot] sendMessage failed: {exc}")
            logger.error("sendMessage 失败: %s", exc, exc_info=True)

    async def _answer_callback(
        self, callback_id: str, text: str, alert: bool = False
    ) -> None:
        """应答 callback query（让按钮停止转圈，可选弹提示）。"""
        url = f"{self._base_url}/answerCallbackQuery"
        payload = {
            "callback_query_id": callback_id,
            "text": text,
            "show_alert": alert,
        }
        try:
            async with httpx.AsyncClient(timeout=15) as ac:
                await ac.post(url, json=payload)
        except Exception as exc:  # noqa: BLE001
            print(f"[telegram_bot] answerCallbackQuery failed: {exc}")
            logger.error("answerCallbackQuery 失败: %s", exc, exc_info=True)

    # --- 权限校验 --------------------------------------------------------- #

    def _is_allowed(self, user_id: int) -> bool:
        """fail-closed：白名单为空时拒绝所有用户，防止"找到 bot 就能下单"。"""
        return user_id in self._allowed

    # --- 命令分发 --------------------------------------------------------- #

    async def _handle_update(self, update: dict[str, Any]) -> None:
        cb = update.get("callback_query")
        if cb:
            await self._handle_callback(cb)
            return

        msg = update.get("message")
        if not msg:
            return
        chat_id = msg.get("chat", {}).get("id")
        text = (msg.get("text") or "").strip()
        user = msg.get("from", {})
        user_id = user.get("id")

        if not chat_id or not text:
            return
        logger.info("[tg][%s] 消息 from=%s: %s", chat_id, user_id, text[:120])
        if not self._is_allowed(user_id):
            if not self._allowed:
                await self._send_message(
                    chat_id,
                    "⛔ <b>未授权</b>（fail-closed：未配置白名单时拒绝所有操作）\n\n"
                    f"你的 Telegram <b>user_id</b> 是：<code>{user_id}</code>\n"
                    "把它填入 .env 的 <code>TELEGRAM_ALLOWED_USER_IDS</code> 并重启 bot 即可。\n"
                    "（也可用 @userinfobot 查询）",
                )
            else:
                await self._send_message(chat_id, "⛔ 未授权")
            return

        # 非命令文本 → 下单向导输入（无进行中的向导则忽略，避免群聊误扰）
        if not text.startswith("/"):
            await self._handle_wizard_text(chat_id, text)
            return

        # 解析命令和参数
        parts = text.split()
        cmd = parts[0].lower()
        args = parts[1:]

        try:
            handler = self._dispatch.get(cmd)
            logger.info("[tg][%s] 命令 %s args=%s", chat_id, cmd, args)
            if handler is None:
                await self._send_message(
                    chat_id,
                    f"❓ 未知命令: <code>{cmd}</code>\n发送 /menu 打开主菜单",
                )
                return
            await handler(self, chat_id, args)
        except Exception as exc:  # noqa: BLE001
            logger.error("[tg][%s] 命令执行异常 %s: %s", chat_id, cmd, exc, exc_info=True)
            await self._send_message(chat_id, f"❌ 执行失败: <code>{exc}</code>")

    # --- 命令实现 --------------------------------------------------------- #

    async def cmd_menu(self, chat_id: int | str, args: list[str]) -> None:
        """主菜单：内联键盘按钮集（/menu /start /help 统一入口）。"""
        await self._send_message(
            chat_id,
            "🎛 <b>Polymarket 交易 bot 主菜单</b>\n\n选择操作：",
            reply_markup={
                "inline_keyboard": [
                    [{"text": "📥 下单（向导）", "callback_data": "menu:order"}],
                    [
                        {"text": "📊 持仓状态", "callback_data": "menu:status"},
                        {"text": "📤 平仓", "callback_data": "menu:close"},
                    ],
                    [
                        {"text": "❌ 撤单", "callback_data": "menu:cancel"},
                        {"text": "⏹ 停止策略", "callback_data": "menu:stop"},
                    ],
                    [
                        {"text": "⚙️ 参数修改（命令）", "callback_data": "menu:params"},
                        {"text": "ℹ️ 帮助", "callback_data": "menu:help"},
                    ],
                ]
            },
        )

    async def _send_help_text(self, chat_id: int | str) -> None:
        text = (
            "<b>Polymarket 交易 bot 命令</b>\n\n"
            "<b>查询</b>\n"
            "/status — 所有策略快照\n"
            "/list — 列出所有 token_id\n"
            "/parse &lt;url|slug&gt; — 解析市场\n\n"
            "<b>下单与管理</b>\n"
            "/order — 引导式下单向导\n"
            "/order &lt;token_id&gt; &lt;entry&gt; &lt;exit&gt; &lt;size&gt; [tp] [sl]\n"
            "/cancel &lt;token_id&gt; — 撤所有挂单\n"
            "/close &lt;token_id&gt; — 平仓（带确认）\n"
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
        try:
            markets = await asyncio.wait_for(
                asyncio.to_thread(fetch_markets, slug), timeout=FETCH_TIMEOUT
            )
        except asyncio.TimeoutError:
            logger.error("[tg][%s] /parse 超时 slug=%s", chat_id, slug)
            await self._send_message(
                chat_id, "❌ 解析超时（网络或 DNS 异常），请稍后重试。"
            )
            return
        except MarketFetchError as exc:
            logger.error("[tg][%s] /parse 网络失败 slug=%s: %s", chat_id, slug, exc)
            await self._send_message(
                chat_id,
                "❌ 解析失败（Polymarket API 不可达，网络/代理问题），请检查网络后重试。",
            )
            return
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
        if not args:
            # 引导式下单向导
            session = self._sessions.setdefault(int(chat_id), _WizardSession())
            session.order = {
                "state": "wait_url",
                "markets": None,
                "market": None,
                "outcome_index": None,
                "outcome": None,
                "label": None,
                "entry": None,
                "exit_price": None,
                "size": None,
                "tp": None,
                "sl": None,
                "pending_field": None,
            }
            self._touch(session)
            await self._send_message(
                chat_id,
                "📎 <b>开始下单</b>\n"
                "请发送 Polymarket 市场链接或 slug（直接粘贴链接）。\n"
                "回复「取消」可随时中止。",
            )
            return
        if len(args) < 4:
            await self._send_message(
                chat_id,
                "用法: /order &lt;token_id&gt; &lt;entry&gt; &lt;exit&gt; &lt;size&gt; [tp] [sl]\n"
                "示例: /order 72123...abc 0.42 0.55 100 0.10 0.05\n"
                "不带参数进入引导式向导：/order",
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
            enabled=True,
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
            logger.info(
                "[tg][%s] 下单成功 %s entry=%s exit=%s size=%s",
                chat_id, sc.label, sc.entry_price, sc.exit_price, sc.share_amount,
            )

            await self._send_message(
                chat_id,
                f"✅ 已下单并启动策略: {label}\n"
                f"token: <code>{token_id[:16]}...</code>",
            )
            self._push_order_summary(sc)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "[tg][%s] /order 下单异常: %s", chat_id, exc, exc_info=True
            )
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
            # 无参数：列出有持仓的策略，按钮选择
            await self._send_close_candidates(chat_id)
            return
        token_id = args[0]
        rt = self._manager.find_runtime(token_id)
        if not rt:
            await self._send_message(chat_id, "❌ 未找到该 token 的策略")
            return
        bid, _ = await asyncio.to_thread(fetch_token_price, token_id)
        size = rt._position.size
        if size <= 0:
            await self._send_message(chat_id, "❌ 无持仓可平")
            return
        session = self._sessions.setdefault(int(chat_id), _WizardSession())
        session.close_list = [
            {"token_id": token_id, "label": rt.label, "size": size, "bid": bid}
        ]
        self._touch(session)
        await self._send_close_confirm(chat_id, session, 0)

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

    # ------------------------------------------------------------------ #
    # 内联键盘 + 下单向导（移动端主操作路径）
    # ------------------------------------------------------------------ #

    def _touch(self, session: _WizardSession) -> None:
        session.ts = time.time()

    async def _handle_callback(self, cb: dict[str, Any]) -> None:
        user_id = cb.get("from", {}).get("id")
        chat_id = cb.get("message", {}).get("chat", {}).get("id")
        data = cb.get("data", "")
        cb_id = cb.get("id")
        if not chat_id or not data:
            return
        logger.info("[tg][%s] 按钮 from=%s: %s", chat_id, user_id, data)
        if not self._is_allowed(user_id):
            await self._answer_callback(cb_id, "⛔ 未授权", alert=True)
            return

        session = self._sessions.get(int(chat_id))
        if session and time.time() - session.ts > self._draft_ttl:
            self._sessions.pop(int(chat_id), None)
            session = None
            await self._send_message(chat_id, "⏳ 会话已过期，请重新开始。")

        try:
            if data.startswith("menu:"):
                await self._cb_menu(cb, chat_id, data[5:])
            elif data.startswith("ord:"):
                await self._cb_order(cb, chat_id, data[4:])
            elif data.startswith("clx:"):
                await self._cb_close(cb, chat_id, data[4:])
            elif data.startswith("cnl:"):
                await self._cb_cancel(cb, chat_id, data[4:])
            elif data.startswith("stp:"):
                await self._cb_stop(cb, chat_id, data[4:])
            else:
                await self._answer_callback(cb_id, "未知操作", alert=True)
        except Exception as exc:  # noqa: BLE001
            logger.error("[tg][%s] 按钮操作异常 %s: %s", chat_id, data, exc, exc_info=True)
            await self._answer_callback(cb_id, "执行失败", alert=True)
            await self._send_message(chat_id, f"❌ 操作异常: <code>{exc}</code>")

    # ---- 主菜单回调 ----------------------------------------------------- #

    async def _cb_menu(self, cb: dict[str, Any], chat_id: int | str, action: str) -> None:
        if action == "order":
            await self._answer_callback(cb["id"], "", False)
            await self.cmd_order(chat_id, [])
            return
        if action == "status":
            await self._answer_callback(cb["id"], "", False)
            await self.cmd_status(chat_id, [])
            return
        if action == "close":
            await self._answer_callback(cb["id"], "", False)
            await self._send_close_candidates(chat_id)
            return
        if action == "cancel":
            await self._answer_callback(cb["id"], "", False)
            await self._send_cancel_candidates(chat_id)
            return
        if action == "stop":
            await self._answer_callback(cb["id"], "", False)
            await self._send_stop_candidates(chat_id)
            return
        if action == "params":
            await self._answer_callback(cb["id"], "", False)
            await self._send_message(
                chat_id,
                "⚙️ <b>参数修改（一行式命令）</b>\n\n"
                "/price &lt;token_id&gt; &lt;0-1&gt; — 买入价\n"
                "/exit_price &lt;token_id&gt; &lt;0-1&gt; — 卖出价\n"
                "/amount &lt;token_id&gt; &lt;n&gt; — 份额\n"
                "/takeprofit &lt;token_id&gt; &lt;pct|off&gt;\n"
                "/stoploss &lt;token_id&gt; &lt;pct|off&gt;\n\n"
                "token_id 用 /list 查看。",
            )
            return
        if action == "help":
            await self._answer_callback(cb["id"], "", False)
            await self._send_help_text(chat_id)
            return
        await self._answer_callback(cb["id"], "未知操作", alert=True)

    # ---- 平仓：候选人列表 + 确认 ---------------------------------------- #

    async def _send_close_candidates(self, chat_id: int | str) -> None:
        snaps = self._manager.status_snapshot()
        holding = [s for s in snaps if s.get("position_size", 0) > 0]
        if not holding:
            await self._send_message(chat_id, "📭 当前无持仓")
            return
        session = self._sessions.setdefault(int(chat_id), _WizardSession())
        session.close_list = [
            {
                "token_id": s["token_id"],
                "label": s.get("label", "?"),
                "size": s.get("position_size", 0),
                "bid": None,
            }
            for s in holding
        ]
        self._touch(session)
        buttons = [
            [
                {
                    "text": f"📤 {item['label'][:40]} ({item['size']:g} 份)",
                    "callback_data": f"clx:sel:{i}",
                }
            ]
            for i, item in enumerate(session.close_list)
        ]
        buttons.append([{"text": "❌ 取消", "callback_data": "clx:no"}])
        await self._send_message(
            chat_id,
            "📤 选择要平仓的持仓：",
            reply_markup={"inline_keyboard": buttons},
        )

    async def _send_close_confirm(
        self, chat_id: int | str, session: _WizardSession, i: int
    ) -> None:
        item = session.close_list[i]
        if item["bid"] is None:
            item["bid"], _ = await asyncio.to_thread(
                fetch_token_price, item["token_id"]
            )
        bid_txt = f"{item['bid']}" if item["bid"] is not None else "未知（按最新盘口）"
        await self._send_message(
            chat_id,
            f"⚠️ <b>确认平仓</b>\n"
            f"{_html_escape(item['label'])}\n"
            f"size = {item['size']:g} 份\n"
            f"best_bid = {bid_txt}\n\n"
            "确认后将先撤 SELL 挂单，再按 best_bid 市价卖出。",
            reply_markup={
                "inline_keyboard": [
                    [{"text": "✅ 确认平仓", "callback_data": f"clx:confirm:{i}"}],
                    [{"text": "❌ 取消", "callback_data": "clx:no"}],
                ]
            },
        )

    async def _cb_close(self, cb: dict[str, Any], chat_id: int | str, action: str) -> None:
        session = self._sessions.get(int(chat_id))
        if not session or not session.close_list:
            await self._answer_callback(cb["id"], "会话已失效", alert=True)
            return
        if action == "no":
            await self._answer_callback(cb["id"], "已取消", False)
            return
        if action.startswith("sel:"):
            try:
                i = int(action[4:])
            except ValueError:
                await self._answer_callback(cb["id"], "无效操作", alert=True)
                return
            if not (0 <= i < len(session.close_list)):
                await self._answer_callback(cb["id"], "列表已过期", alert=True)
                return
            await self._answer_callback(cb["id"], "", False)
            await self._send_close_confirm(chat_id, session, i)
            return
        if action.startswith("confirm:"):
            try:
                i = int(action[8:])
            except ValueError:
                await self._answer_callback(cb["id"], "无效操作", alert=True)
                return
            if not (0 <= i < len(session.close_list)):
                await self._answer_callback(cb["id"], "列表已过期", alert=True)
                return
            item = session.close_list[i]
            token_id = item["token_id"]
            rt = self._manager.find_runtime(token_id)
            if not rt:
                await self._answer_callback(cb["id"], "策略不存在", alert=True)
                return
            await self._answer_callback(cb["id"], "⏳ 正在平仓...", False)
            try:
                async with self._manager._order_lock:
                    await asyncio.to_thread(
                        trading.cancel_open_orders_for_token,
                        self._client, token_id, "SELL",
                    )
                    rt._has_open_sell_order = False
                    bid, _ = await asyncio.to_thread(fetch_token_price, token_id)
                    if bid is None:
                        await self._send_message(chat_id, "❌ 无法获取 best_bid")
                        return
                    size = rt._position.size
                    if size <= 0:
                        await self._send_message(chat_id, "❌ 无持仓可平")
                        return
                    await asyncio.to_thread(
                        trading.exit_position,
                        self._client, token_id, "SELL", size, bid,
                    )
                await self._send_message(
                    chat_id, f"✅ 已平仓: size={size:g} @ {bid}"
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("[tg][%s] 平仓失败: %s", chat_id, exc, exc_info=True)
                await self._send_message(chat_id, f"❌ 平仓失败: {exc}")
            return
        await self._answer_callback(cb["id"], "未知操作", alert=True)

    # ---- 撤单 / 停止：候选人列表 ---------------------------------------- #

    async def _send_cancel_candidates(self, chat_id: int | str) -> None:
        snaps = self._manager.status_snapshot()
        if not snaps:
            await self._send_message(chat_id, "📭 当前无策略")
            return
        session = self._sessions.setdefault(int(chat_id), _WizardSession())
        session.cancel_list = [
            {"token_id": s["token_id"], "label": s.get("label", "?")} for s in snaps
        ]
        self._touch(session)
        buttons = [
            [{"text": f"❌ {item['label'][:40]}", "callback_data": f"cnl:{i}"}]
            for i, item in enumerate(session.cancel_list)
        ]
        await self._send_message(
            chat_id,
            "❌ 选择要撤单的策略：",
            reply_markup={"inline_keyboard": buttons},
        )

    async def _send_stop_candidates(self, chat_id: int | str) -> None:
        snaps = self._manager.status_snapshot()
        if not snaps:
            await self._send_message(chat_id, "📭 当前无策略")
            return
        session = self._sessions.setdefault(int(chat_id), _WizardSession())
        session.stop_list = [
            {"token_id": s["token_id"], "label": s.get("label", "?")} for s in snaps
        ]
        self._touch(session)
        buttons = [
            [{"text": f"⏹ {item['label'][:40]}", "callback_data": f"stp:{i}"}]
            for i, item in enumerate(session.stop_list)
        ]
        await self._send_message(
            chat_id,
            "⏹ 选择要停止的策略：",
            reply_markup={"inline_keyboard": buttons},
        )

    async def _cb_cancel(self, cb: dict[str, Any], chat_id: int | str, action: str) -> None:
        session = self._sessions.get(int(chat_id))
        if not session:
            await self._answer_callback(cb["id"], "会话已失效", alert=True)
            return
        try:
            i = int(action)
        except ValueError:
            await self._answer_callback(cb["id"], "无效操作", alert=True)
            return
        if not (0 <= i < len(session.cancel_list)):
            await self._answer_callback(cb["id"], "列表已过期", alert=True)
            return
        item = session.cancel_list[i]
        token_id = item["token_id"]
        rt = self._manager.find_runtime(token_id)
        if not rt:
            await self._answer_callback(cb["id"], "策略不存在", alert=True)
            return
        await self._answer_callback(cb["id"], "", False)
        async with self._manager._order_lock:
            count = await asyncio.to_thread(
                trading.cancel_open_orders_for_token, self._client, token_id
            )
        rt._has_open_buy_order = False
        rt._has_open_sell_order = False
        rt._entry_attempted = False
        await self._send_message(
            chat_id, f"✅ 已撤单: {item['label']} (count={count})"
        )

    async def _cb_stop(self, cb: dict[str, Any], chat_id: int | str, action: str) -> None:
        session = self._sessions.get(int(chat_id))
        if not session:
            await self._answer_callback(cb["id"], "会话已失效", alert=True)
            return
        try:
            i = int(action)
        except ValueError:
            await self._answer_callback(cb["id"], "无效操作", alert=True)
            return
        if not (0 <= i < len(session.stop_list)):
            await self._answer_callback(cb["id"], "列表已过期", alert=True)
            return
        item = session.stop_list[i]
        token_id = item["token_id"]
        rt = self._manager.find_runtime(token_id)
        if not rt:
            await self._answer_callback(cb["id"], "策略不存在", alert=True)
            return
        await self._answer_callback(cb["id"], "", False)
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

    # ---- 下单向导 -------------------------------------------------------- #

    def _field_prompt(self, field: str) -> str:
        prompts = {
            "entry": (
                "💰 <b>买入价 entry</b> (0, 1)\n"
                f"配置默认: {self._config.default_entry_price}\n"
                "直接回复价格数字，或「取消」。"
            ),
            "exit_price": (
                "📤 <b>卖出价 exit_price</b> (0, 1)\n"
                f"配置默认: {self._config.default_exit_price}\n"
                "直接回复价格数字，或「取消」。"
            ),
            "size": (
                "🔢 <b>数量</b>（份数，&gt; 0）\n"
                f"配置默认: {self._config.default_share_amount:g}\n"
                "直接回复数字，或「取消」。"
            ),
            "tp": (
                "🎯 <b>止盈 TP</b> (0, 1)，如 0.10 = 10%\n"
                "先输入止盈，再输入止损；回复 off 表示不设置。"
            ),
            "sl": (
                "🛡️ <b>止损 SL</b> (0, 1)，如 0.05 = 5%\n"
                "回复 off 表示不设置。"
            ),
        }
        return prompts.get(field, "请输入：")

    async def _handle_wizard_text(self, chat_id: int | str, text: str) -> None:
        session = self._sessions.get(int(chat_id))
        if not session or not session.order:
            return
        if time.time() - session.ts > self._draft_ttl:
            self._sessions.pop(int(chat_id), None)
            await self._send_message(chat_id, "⏳ 下单草稿已过期，请重新 /order。")
            return
        draft = session.order
        lowered = text.strip().lower()
        if lowered in ("取消", "cancel", "exit"):
            self._sessions.pop(int(chat_id), None)
            await self._send_message(chat_id, "❌ 已取消下单")
            return

        if draft["state"] == "wait_url":
            await self._wizard_parse_url(chat_id, session, text)
            return
        if draft["state"] == "input":
            await self._wizard_input(chat_id, session, text)
            return
        await self._send_message(
            chat_id, "⏳ 请点击上一条消息里的按钮继续，或回复「取消」。"
        )

    async def _wizard_parse_url(
        self, chat_id: int | str, session: _WizardSession, text: str
    ) -> None:
        slug = extract_slug(text)
        if not slug:
            await self._send_message(chat_id, "❌ 无法解析市场链接/slug，请重新发送。")
            return
        await self._send_message(chat_id, f"⏳ 正在解析 <code>{slug}</code> ...")
        t0 = time.time()
        try:
            markets = await asyncio.wait_for(
                asyncio.to_thread(fetch_markets, slug), timeout=FETCH_TIMEOUT
            )
        except asyncio.TimeoutError:
            logger.error(
                "[tg][%s] 向导解析超时 slug=%s (%.1fs)", chat_id, slug, time.time() - t0
            )
            await self._send_message(
                chat_id, "❌ 解析超时（网络或 DNS 异常），请检查网络后重试。"
            )
            return
        except MarketFetchError as exc:
            logger.error(
                "[tg][%s] 向导解析网络失败 slug=%s: %s", chat_id, slug, exc
            )
            await self._send_message(
                chat_id,
                "❌ 解析失败（Polymarket API 不可达，网络/代理问题），请检查网络后重试。",
            )
            return
        logger.info(
            "[tg][%s] 向导解析完成 slug=%s → %d 市场 (%.1fs)",
            chat_id, slug, len(markets), time.time() - t0,
        )
        if not markets:
            await self._send_message(chat_id, "❌ 未找到市场，请检查链接后重试。")
            return
        draft = session.order
        draft["markets"] = markets
        draft["state"] = "pick_market"
        self._touch(session)
        buttons = []
        for i, m in enumerate(markets[:12]):
            status = (
                "🔴"
                if m.get("closed")
                else ("🟡" if not m.get("accepting") else "🟢")
            )
            buttons.append(
                [
                    {
                        "text": f"{status} [{i+1}] {_html_escape(m.get('question', '?'))[:56]}",
                        "callback_data": f"ord:mkt:{i}",
                    }
                ]
            )
        buttons.append([{"text": "❌ 取消", "callback_data": "ord:cancel"}])
        extra = (
            f"\n⚠️ 共 {len(markets)} 个市场，仅显示前 12 个。"
            if len(markets) > 12
            else ""
        )
        await self._send_message(
            chat_id,
            f"📊 找到 {len(markets)} 个市场，选择：{extra}",
            reply_markup={"inline_keyboard": buttons},
        )

    async def _wizard_input(
        self, chat_id: int | str, session: _WizardSession, text: str
    ) -> None:
        draft = session.order
        field = draft.get("pending_field")
        if field not in ("entry", "exit_price", "size", "tp", "sl"):
            await self._send_message(chat_id, "⏳ 状态异常，请重新 /order。")
            return
        raw = text.strip().lower()
        if raw == "off":
            value: float | None = None
        else:
            try:
                value = float(raw)
            except ValueError:
                await self._send_message(
                    chat_id, "❌ 格式不对，请重新输入或回复「取消」。"
                )
                return
        # 范围校验（None 表示 off，仅 tp/sl 允许）
        if field in ("entry", "exit_price") and (
            value is None or not (0 < value < 1)
        ):
            await self._send_message(chat_id, "❌ 价格需在 (0, 1)，请重新输入。")
            return
        if field == "size" and (value is None or not value > 0):
            await self._send_message(chat_id, "❌ 数量需 > 0，请重新输入。")
            return
        if field in ("tp", "sl") and value is not None and not (0 < value < 1):
            await self._send_message(
                chat_id, "❌ 百分比需在 (0, 1)（如 0.10 = 10%），或 off。"
            )
            return

        draft[field] = value
        logger.info("[tg][%s] 向导参数 %s=%s", chat_id, field, value)
        self._touch(session)

        if field == "entry":
            draft["pending_field"] = "exit_price"
            await self._send_message(chat_id, self._field_prompt("exit_price"))
            return
        if field == "exit_price":
            draft["pending_field"] = "size"
            await self._send_message(chat_id, self._field_prompt("size"))
            return
        if field == "size":
            draft["state"] = "tp_choice"
            tp = self._config.default_take_profit_pct
            sl = self._config.default_stop_loss_pct
            await self._send_message(
                chat_id,
                "🎯 <b>止盈止损</b>（出场保护，bot 会自动执行）\n选择：",
                reply_markup={
                    "inline_keyboard": [
                        [
                            {
                                "text": (
                                    f"📌 用配置默认 "
                                    f"(TP={self._fmt_pct(tp)} / SL={self._fmt_pct(sl)})"
                                ),
                                "callback_data": "ord:tp:default",
                            }
                        ],
                        [
                            {"text": "✏️ 自定义", "callback_data": "ord:tp:custom"},
                            {"text": "🚫 不设置", "callback_data": "ord:tp:off"},
                        ],
                    ]
                },
            )
            return
        if field == "tp":
            draft["pending_field"] = "sl"
            await self._send_message(chat_id, self._field_prompt("sl"))
            return
        if field == "sl":
            await self._show_review(chat_id, session)
            return

    async def _show_review(
        self, chat_id: int | str, session: _WizardSession
    ) -> None:
        draft = session.order
        m = draft["market"]
        cost = draft["size"] * draft["entry"]
        lines = [
            "📋 <b>订单确认</b>",
            f"市场: {_html_escape(m.get('question', '?'))}",
            f"结果: {_html_escape(draft['outcome'])}",
            f"买入价: {draft['entry']}",
            f"卖出价: {draft['exit_price']}",
            f"数量: {draft['size']:g} 份（≈ ${cost:.2f}）",
            f"TP: {self._fmt_pct(draft['tp'])}  SL: {self._fmt_pct(draft['sl'])}",
            "",
            "确认后将以限价单下单并启动策略监控。",
        ]
        buttons = [
            [{"text": "✅ 确认下单", "callback_data": "ord:confirm"}],
            [
                {"text": "✏️ 买入价", "callback_data": "ord:edit:entry"},
                {"text": "✏️ 卖出价", "callback_data": "ord:edit:exit"},
            ],
            [
                {"text": "✏️ 数量", "callback_data": "ord:edit:size"},
                {"text": "✏️ TP/SL", "callback_data": "ord:edit:tp"},
            ],
            [{"text": "❌ 取消", "callback_data": "ord:cancel"}],
        ]
        draft["state"] = "review"
        self._touch(session)
        await self._send_message(
            chat_id,
            "\n".join(lines),
            reply_markup={"inline_keyboard": buttons},
        )

    async def _cb_order(
        self, cb: dict[str, Any], chat_id: int | str, action: str
    ) -> None:
        session = self._sessions.get(int(chat_id))
        if action == "cancel":
            if session:
                self._sessions.pop(int(chat_id), None)
            await self._answer_callback(cb["id"], "已取消", False)
            return
        if not session or not session.order:
            await self._answer_callback(
                cb["id"], "草稿已失效，请重新 /order", alert=True
            )
            return
        draft = session.order
        self._touch(session)

        if action.startswith("mkt:"):
            try:
                i = int(action[4:])
            except ValueError:
                await self._answer_callback(cb["id"], "无效操作", alert=True)
                return
            markets = draft["markets"] or []
            if not (0 <= i < len(markets)):
                await self._answer_callback(cb["id"], "列表已过期", alert=True)
                return
            m = markets[i]
            draft["market"] = m
            outcomes = m.get("outcomes", [])
            if not outcomes:
                await self._answer_callback(cb["id"], "该市场无 outcome", alert=True)
                return
            draft["state"] = "pick_outcome"
            buttons = [
                [{"text": f"🎯 {oc}", "callback_data": f"ord:oc:{j}"}]
                for j, oc in enumerate(outcomes[:8])
            ]
            buttons.append([{"text": "❌ 取消", "callback_data": "ord:cancel"}])
            await self._answer_callback(cb["id"], "", False)
            await self._send_message(
                chat_id,
                f"🎯 <b>{_html_escape(m.get('question', '?'))}</b>\n选择结果：",
                reply_markup={"inline_keyboard": buttons},
            )
            return

        if action.startswith("oc:"):
            try:
                j = int(action[3:])
            except ValueError:
                await self._answer_callback(cb["id"], "无效操作", alert=True)
                return
            m = draft["market"]
            outcomes = m.get("outcomes", [])
            if not (0 <= j < len(outcomes)):
                await self._answer_callback(cb["id"], "列表已过期", alert=True)
                return
            draft["outcome_index"] = j
            draft["outcome"] = outcomes[j]
            draft["label"] = build_label(m, outcomes[j])
            draft["state"] = "input"
            draft["pending_field"] = "entry"
            await self._answer_callback(cb["id"], "", False)
            await self._send_message(chat_id, self._field_prompt("entry"))
            return

        if action.startswith("tp:"):
            mode = action[3:]
            if mode == "default":
                tp = self._config.default_take_profit_pct
                sl = self._config.default_stop_loss_pct
                if tp is None and sl is None:
                    await self._answer_callback(
                        cb["id"], "配置未设默认 TP/SL，按不设置处理", False
                    )
                draft["tp"] = tp
                draft["sl"] = sl
                await self._show_review(chat_id, session)
            elif mode == "custom":
                draft["state"] = "input"
                draft["pending_field"] = "tp"
                await self._answer_callback(cb["id"], "", False)
                await self._send_message(chat_id, self._field_prompt("tp"))
            elif mode == "off":
                draft["tp"] = None
                draft["sl"] = None
                await self._answer_callback(cb["id"], "", False)
                await self._show_review(chat_id, session)
            else:
                await self._answer_callback(cb["id"], "未知操作", alert=True)
            return

        if action == "confirm":
            await self._cb_confirm_order(cb, chat_id, session)
            return

        if action.startswith("edit:"):
            field = action[5:]
            if field not in ("entry", "exit_price", "size", "tp"):
                await self._answer_callback(cb["id"], "无效操作", alert=True)
                return
            draft["state"] = "input"
            draft["pending_field"] = field
            await self._answer_callback(cb["id"], "", False)
            await self._send_message(chat_id, self._field_prompt(field))
            return

        await self._answer_callback(cb["id"], "未知操作", alert=True)

    async def _cb_confirm_order(
        self, cb: dict[str, Any], chat_id: int | str, session: _WizardSession
    ) -> None:
        draft = session.order
        missing = [
            f
            for f in ("market", "outcome", "entry", "exit_price", "size")
            if draft.get(f) is None
        ]
        if missing:
            await self._answer_callback(
                cb["id"], "信息不完整，请重新 /order", alert=True
            )
            return
        m = draft["market"]
        token_ids = m.get("clob_token_ids", [])
        j = draft["outcome_index"] or 0
        if not (0 <= j < len(token_ids)):
            await self._answer_callback(cb["id"], "token 缺失", alert=True)
            return
        token_id = token_ids[j]
        if self._manager.find_runtime(token_id):
            await self._answer_callback(
                cb["id"], "该 token 已有策略，请先 /stop 或 /delete", alert=True
            )
            return
        await self._answer_callback(cb["id"], "⏳ 正在下单...", False)
        sc = StrategyConfig(
            token_id=token_id,
            label=draft["label"] or f"TG策略 [{token_id[:8]}]",
            entry_price=draft["entry"],
            exit_price=draft["exit_price"],
            share_amount=draft["size"],
            take_profit_pct=draft["tp"],
            stop_loss_pct=draft["sl"],
            enabled=True,
        )
        try:
            async with self._manager._order_lock:
                ok, msg = await self._check_balance_and_place(sc)
                if not ok:
                    await self._send_message(chat_id, f"❌ 下单失败: {msg}")
                    return
            rt = self._manager.add_strategy(sc)
            task = asyncio.create_task(rt.run(), name=f"strategy:{rt.label}")
            self._manager._tasks.append(task)
            logger.info(
                "[tg][%s] 向导下单成功 %s entry=%s exit=%s size=%s",
                chat_id, sc.label, sc.entry_price, sc.exit_price, sc.share_amount,
            )
            self._sessions.pop(int(chat_id), None)
            await self._send_message(
                chat_id,
                f"✅ 已下单并启动策略: {sc.label}\n"
                f"entry={sc.entry_price} exit={sc.exit_price} size={sc.share_amount:g}",
            )
            self._push_order_summary(sc)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "[tg][%s] 向导下单异常: %s", chat_id, exc, exc_info=True
            )
            await self._send_message(chat_id, f"❌ 下单异常: {exc}")

    @staticmethod
    def _fmt_pct(v: float | None) -> str:
        if v is None:
            return "不设置"
        return f"{v:.0%}"

    def _push_order_summary(self, sc: StrategyConfig) -> None:
        """下单成功后回显到推送频道（notify_order_summary 事件）。"""
        notifier = getattr(self._manager, "_notifier", None)
        if notifier and notifier.enabled:
            notifier.notify_order_summary(
                f"📥 <b>TG 下单成功</b>\n{_html_escape(sc.display())}\n"
                f"entry={sc.entry_price} exit={sc.exit_price} size={sc.share_amount:g}\n"
                f"TP={self._fmt_pct(sc.take_profit_pct)} SL={self._fmt_pct(sc.stop_loss_pct)}"
            )

    # --- 命令分发表 ------------------------------------------------------- #
    _dispatch: dict[str, Any] = {}  # 类加载后填充


# 填充命令分发表
TelegramCommandBot._dispatch = {
    "/menu": TelegramCommandBot.cmd_menu,
    "/help": TelegramCommandBot.cmd_menu,
    "/start": TelegramCommandBot.cmd_menu,
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
