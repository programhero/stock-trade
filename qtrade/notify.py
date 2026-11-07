"""成交消息推送：把每笔成交按模板渲染成 JSON，异步 POST 到配置的 URL。

设计约束：
- 只依赖标准库（urllib.request + queue + threading）；
- 推送在后台线程发送，网络延迟/失败不阻塞撮合；
- 推送异常只记 warning，绝不影响交易主流程；
- 模板占位符形如 ``{{price}}``，字符串值会自动做 JSON 转义。
"""

from __future__ import annotations

import json
import logging
import queue
import re
import threading
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .config import NotifyConfig
from .types import Fill, Side

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


class TemplateError(ValueError):
    """推送模板错误（空模板、未知占位符或渲染后不是合法 JSON）。"""


@dataclass(slots=True)
class _Pending:
    payload: str
    label: str


def build_template_context(fill: Fill, symbol_name: str, mode: str) -> dict[str, object]:
    """把成交转换为模板占位符 -> 值的映射。"""
    return {
        "symbol": fill.symbol,
        "symbol_name": symbol_name,
        "side": fill.side.value,
        "side_label": fill.side.label,
        "qty": fill.qty,
        "price": fill.price,
        "notional": fill.notional,
        "fee": fill.fee,
        "commission": fill.commission,
        "stamp_duty": fill.stamp_duty,
        "transfer_fee": fill.transfer_fee,
        "position_after": fill.position_after,
        "cash_after": fill.cash_after,
        "dt": fill.dt.strftime("%Y-%m-%d %H:%M"),
        "date": fill.dt.strftime("%Y-%m-%d"),
        "time": fill.dt.strftime("%H:%M"),
        "order_id": fill.order_id,
        "reason": fill.reason,
        "mode": mode,
    }


def _format_value(value: object) -> str:
    """格式化占位符值：字符串做 JSON 转义，数值直接输出。"""
    if isinstance(value, str):
        # 去掉 json.dumps 自带的引号，由模板自己决定是否加引号
        return json.dumps(value, ensure_ascii=False)[1:-1]
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def render_template(template: str, context: dict[str, object]) -> str:
    """替换模板中的 ``{{placeholder}}``；未知占位符抛 TemplateError。"""

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in context:
            raise TemplateError(f"notify.template 存在未知占位符: {{{{{name}}}}}")
        return _format_value(context[name])

    return _PLACEHOLDER_RE.sub(replace, template)


def validate_template(template: str) -> None:
    """用样例成交渲染模板并校验其为合法 JSON；失败抛 TemplateError。"""
    if not template.strip():
        raise TemplateError("notify.template 不能为空")
    sample = Fill(
        order_id=1,
        dt=datetime(2026, 1, 5, 9, 31),
        symbol="000000",
        side=Side.BUY,
        qty=100,
        price=10.0,
        commission=5.0,
        stamp_duty=0.0,
        transfer_fee=0.01,
        reason='模板样例 "reason"\n第二行',
        position_after=100,
        cash_after=1000.0,
    )
    rendered = render_template(
        template, build_template_context(sample, "样例股票", "paper")
    )
    try:
        json.loads(rendered)
    except json.JSONDecodeError as exc:
        raise TemplateError(f"notify.template 渲染后不是合法 JSON: {exc}") from exc


class TradeNotifier:
    """成交推送器：按模板渲染 JSON，后台线程逐条 POST。"""

    def __init__(
        self,
        cfg: NotifyConfig,
        symbol_name: str,
        logger: logging.Logger | None = None,
    ):
        self.cfg = cfg
        self.url = cfg.url.strip()
        self.symbol_name = symbol_name
        self.log = logger or logging.getLogger("qtrade.notify")
        self._queue: queue.Queue = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._closed = False

    # ------------------------------------------------------------------ 对外

    def notify_fill(self, fill: Fill, mode: str) -> bool:
        """渲染并入队一笔成交推送；未启用/回测跳过/已关闭时返回 False。"""
        if not self.cfg.enabled:
            return False
        if mode == "backtest" and not self.cfg.send_in_backtest:
            return False

        try:
            payload = render_template(
                self.cfg.template,
                build_template_context(fill, self.symbol_name, mode),
            )
        except TemplateError as exc:
            self.log.warning("成交推送模板渲染失败: %s", exc)
            return False

        label = (
            f"[{mode}] {fill.dt:%m-%d %H:%M} {fill.side.label} "
            f"{fill.qty} 股 @ {fill.price:.2f}"
        )
        with self._lock:
            if self._closed:
                self.log.warning("推送器已关闭，忽略成交推送 | %s", label)
                return False
            if self._thread is None:
                try:
                    thread = threading.Thread(
                        target=self._worker, name="qtrade-notify", daemon=True
                    )
                    thread.start()
                    self._thread = thread
                except RuntimeError as exc:
                    self.log.warning("成交推送线程启动失败: %s", exc)
                    return False
            self._queue.put(_Pending(payload=payload, label=label))
        return True

    def close(self, timeout: float = 10.0) -> None:
        """停止接收新推送，并等待队列中的请求发送完毕。"""
        with self._lock:
            if not self._closed:
                self._closed = True
                if self._thread is not None:
                    self._queue.put(None)  # 哨兵：排在所有待发送消息之后
            thread = self._thread
        if thread is not None:
            thread.join(timeout)
            if thread.is_alive():
                self.log.warning(
                    "成交推送队列在 %.1f 秒内未清空，仍有请求待发送", timeout
                )

    # ------------------------------------------------------------------ 内部

    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                self._send(item)
            finally:
                self._queue.task_done()

    def _send(self, item: _Pending) -> None:
        request = urllib.request.Request(
            self.url,
            data=item.payload.encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.cfg.timeout_seconds
            ) as response:
                status = getattr(response, "status", 0)
                response.read(2048)  # 排空响应体，释放连接
            self.log.info("成交推送成功 HTTP %s | %s", status, item.label)
        except Exception as exc:  # noqa: BLE001 - 后台线程兜底，绝不能抛出
            self.log.warning("成交推送失败 | %s | %s", item.label, exc)
