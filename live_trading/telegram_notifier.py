"""텔레그램 주문/체결 알림 모듈

환경변수:
    TELEGRAM_BOT_TOKEN : 텔레그램 봇 토큰 (BotFather에서 발급)
    TELEGRAM_CHAT_ID   : 알림 수신 채팅 또는 채널 ID

토큰/채팅ID가 미설정이면 is_enabled=False로 조용히 비활성화됩니다.
알림 발송 실패는 예외를 raise하지 않고 경고 메시지만 출력하므로
주문 흐름에 영향을 주지 않습니다.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any

try:
    import requests as _requests

    _REQUESTS_AVAILABLE = True
except ImportError:
    _REQUESTS_AVAILABLE = False


class TelegramNotifier:
    _API_BASE = "https://api.telegram.org"

    def __init__(self) -> None:
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

        self.is_enabled = bool(token and chat_id and _REQUESTS_AVAILABLE)

        if not self.is_enabled:
            if not _REQUESTS_AVAILABLE:
                print("[TelegramNotifier] requests 라이브러리가 없어 알림이 비활성화됩니다.")
            else:
                print(
                    "[TelegramNotifier] TELEGRAM_BOT_TOKEN 또는 TELEGRAM_CHAT_ID 미설정 — 알림 비활성화."
                )
            return

        self._url = f"{self._API_BASE}/bot{token}/sendMessage"
        self._chat_id = chat_id
        # 주문 흐름 지연 방지를 위해 단일 백그라운드 스레드에서 발송
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tg-notify")

    # ──────────────────────────── 기본 발송 ────────────────────────────

    def send(self, message: str) -> bool:
        """동기 방식으로 텔레그램 메시지를 발송합니다."""
        if not self.is_enabled:
            return False
        try:
            resp = _requests.post(
                self._url,
                json={"chat_id": self._chat_id, "text": message, "parse_mode": "HTML"},
                timeout=5,
            )
            resp.raise_for_status()
            return True
        except Exception as exc:
            print(f"[TelegramNotifier] 메시지 발송 실패: {exc}")
            return False

    def send_async(self, message: str) -> None:
        """백그라운드 스레드에서 비동기 발송합니다 (주문 처리 지연 없음)."""
        if not self.is_enabled:
            return
        self._executor.submit(self.send, message)

    # ──────────────────────────── 이벤트별 알림 ────────────────────────────

    def notify_order_submitted(
        self, side: str, ticker: str, qty: int, order_id: str, attempt: int = 1
    ) -> None:
        """주문 제출 성공 알림."""
        side_label = "매도" if side.upper() == "SELL" else "매수"
        retry_suffix = f" (재시도 #{attempt})" if attempt > 1 else ""
        order_suffix = f"\n주문번호: <code>{order_id}</code>" if order_id else ""
        self.send_async(
            f"📤 <b>[{side_label}]</b> {ticker} {qty:,}주 주문 제출{retry_suffix}{order_suffix}"
        )

    def notify_order_filled(
        self,
        side: str,
        ticker: str,
        filled_qty: int,
        total_qty: int,
        price: float | None = None,
    ) -> None:
        """체결 완료 알림."""
        side_label = "매도" if side.upper() == "SELL" else "매수"
        price_txt = f" @ <b>{price:,.0f}원</b>" if price else ""
        self.send_async(
            f"✅ <b>[{side_label}]</b> {ticker} {filled_qty:,}/{total_qty:,}주 체결 완료{price_txt}"
        )

    def notify_order_cancelled(self, side: str, ticker: str, remaining_qty: int) -> None:
        """미체결 주문 취소 알림."""
        side_label = "매도" if side.upper() == "SELL" else "매수"
        self.send_async(
            f"❌ <b>[{side_label}]</b> {ticker} 주문 취소 (잔량 {remaining_qty:,}주)"
        )

    def notify_order_timeout(
        self, side: str, ticker: str, filled_qty: int, total_qty: int
    ) -> None:
        """주문 타임아웃 알림 (취소 없이 잔량 방치)."""
        side_label = "매도" if side.upper() == "SELL" else "매수"
        self.send_async(
            f"⏰ <b>[{side_label}]</b> {ticker} 주문 타임아웃"
            f" (체결 {filled_qty:,}/{total_qty:,}주)"
        )

    def notify_order_error(self, side: str, ticker: str, qty: int, error: Any) -> None:
        """주문 제출 오류 알림."""
        side_label = "매도" if side.upper() == "SELL" else "매수"
        self.send_async(
            f"⚠️ <b>[{side_label} 오류]</b> {ticker} {qty:,}주\n{error}"
        )

    def notify_error(self, context: str, error: Any) -> None:
        """일반 오류 알림."""
        self.send_async(f"⚠️ <b>오류</b> [{context}]\n{error}")

    def notify_daily_summary(
        self,
        trading_date: str,
        run_status: str,
        sell_results: list[dict[str, Any]],
        buy_results: list[dict[str, Any]],
    ) -> None:
        """일일 실행 완료 요약 알림."""
        status_emoji = {
            "DONE": "✅",
            "DONE_DRY_RUN": "🔵",
            "NO_ACTION": "⚪",
            "PARTIAL_FILLED": "🟡",
            "BLOCKED_BUY_BY_UNFILLED_SELL": "🔴",
        }.get(run_status, "ℹ️")

        lines: list[str] = [
            f"{status_emoji} <b>ETF 일일 실행 완료</b> [{trading_date}]",
            f"상태: <b>{run_status}</b>",
        ]

        if sell_results:
            lines.append("\n<b>[매도]</b>")
            for r in sell_results:
                filled = r.get("filled_qty", 0)
                total = r.get("requested_qty", r.get("qty", 0))
                ok = "✓" if r.get("is_filled") else "✗"
                lines.append(f"  {ok} {r['ticker']} {filled:,}/{total:,}주")

        if buy_results:
            lines.append("\n<b>[매수]</b>")
            for r in buy_results:
                filled = r.get("filled_qty", 0)
                total = r.get("requested_qty", r.get("qty", 0))
                ok = "✓" if r.get("is_filled") else "✗"
                lines.append(f"  {ok} {r['ticker']} {filled:,}/{total:,}주")

        if not sell_results and not buy_results:
            lines.append("주문 없음")

        # 요약은 동기 발송 (실행 직후 즉시 수신 보장)
        self.send("\n".join(lines))
