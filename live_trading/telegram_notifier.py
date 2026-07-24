"""텔레그램 주문/체결 알림 모듈

환경변수:
    TELEGRAM_BOT_TOKEN : 텔레그램 봇 토큰 (BotFather에서 발급)
    TELEGRAM_CHAT_ID   : 알림 수신 채팅 또는 채널 ID

토큰/채팅ID가 미설정이면 is_enabled=False로 조용히 비활성화됩니다.
알림 발송 실패는 예외를 raise하지 않고 경고 메시지만 출력하므로
주문 흐름에 영향을 주지 않습니다.
"""

from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

try:
    import requests as _requests

    _REQUESTS_AVAILABLE = True
except ImportError:
    _REQUESTS_AVAILABLE = False

logger = logging.getLogger(__name__)

_TG_MAX_RETRIES = 3
_TG_RETRY_BACKOFF = (1, 2, 4)


class TelegramNotifier:
    _API_BASE = "https://api.telegram.org"

    def __init__(self) -> None:
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

        self.is_enabled = bool(token and chat_id and _REQUESTS_AVAILABLE)

        if not self.is_enabled:
            if not _REQUESTS_AVAILABLE:
                logger.warning("requests 라이브러리가 없어 알림이 비활성화됩니다.")
            else:
                logger.warning(
                    "TELEGRAM_BOT_TOKEN 또는 TELEGRAM_CHAT_ID 미설정 — 알림 비활성화."
                )
            return

        self._url = f"{self._API_BASE}/bot{token}/sendMessage"
        self._chat_id = chat_id
        # 주문 흐름 지연 방지를 위해 단일 백그라운드 스레드에서 발송
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tg-notify")

    # ──────────────────────────── 기본 발송 ────────────────────────────

    def send(self, message: str) -> bool:
        """동기 방식으로 텔레그램 메시지를 발송합니다.

        5xx/네트워크 에러 시 지수 백오프로 최대 3회 재시도합니다.
        4xx (BadRequest 등)은 즉시 포기합니다.
        """
        if not self.is_enabled:
            return False
        last_exc: Exception | None = None
        for attempt in range(_TG_MAX_RETRIES):
            try:
                resp = _requests.post(
                    self._url,
                    json={"chat_id": self._chat_id, "text": message, "parse_mode": "HTML"},
                    timeout=5,
                )
                resp.raise_for_status()
                return True
            except _requests.exceptions.HTTPError as exc:
                status = resp.status_code
                body = resp.text[:500]
                if 400 <= status < 500:
                    logger.warning(
                        "Telegram 4xx 에러 (재시도 불가): %d %s", status, body
                    )
                    return False
                logger.error(
                    "Telegram %d 에러 (attempt %d/%d): %s",
                    status,
                    attempt + 1,
                    _TG_MAX_RETRIES,
                    body,
                )
                last_exc = exc
            except _requests.exceptions.RequestException as exc:
                logger.error(
                    "Telegram 네트워크 에러 (attempt %d/%d): %s",
                    attempt + 1,
                    _TG_MAX_RETRIES,
                    exc,
                )
                last_exc = exc
            if attempt < _TG_MAX_RETRIES - 1:
                time.sleep(_TG_RETRY_BACKOFF[min(attempt, len(_TG_RETRY_BACKOFF) - 1)])
        logger.error(
            "Telegram 발송 최종 실패 (%d회 재시도 후): %s",
            _TG_MAX_RETRIES,
            last_exc,
        )
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
        risk_snapshot: dict[str, Any] | None = None,
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
                lines.append(f"  {ok} {r.get('display_name', r['ticker'])} {filled:,}/{total:,}주")

        if buy_results:
            lines.append("\n<b>[매수]</b>")
            for r in buy_results:
                filled = r.get("filled_qty", 0)
                total = r.get("requested_qty", r.get("qty", 0))
                ok = "\u2713" if r.get("is_filled") else "\u2717"
                lines.append(f"  {ok} {r.get('display_name', r['ticker'])} {filled:,}/{total:,}주")

        if not sell_results and not buy_results:
            lines.append("주문 없음")

        if risk_snapshot:
            lines.append("\n<b>[주문 전 위험 현황]</b>")
            if risk_snapshot.get("complete"):
                equity = float(risk_snapshot.get("current_equity") or 0.0)
                drawdown = float(risk_snapshot.get("current_drawdown") or 0.0)
                max_position = risk_snapshot.get("max_position")
                lines.append(f"평가액: {equity:,.0f}원 / 고점 대비 {drawdown:.1%}")
                if max_position:
                    lines.append(
                        f"최대 비중: {max_position.get('name', max_position.get('ticker', ''))} "
                        f"{float(max_position.get('weight') or 0.0):.1%}"
                    )
                if risk_snapshot.get("peak_initialized_now"):
                    lines.append("고점 기준: 오늘 평가액으로 초기화")
            else:
                lines.append("위험지표 불완전")
            for warning in risk_snapshot.get("warnings", []):
                lines.append(f"⚠️ {warning}")

        # 요약은 동기 발송 (실행 직후 즉시 수신 보장)
        self.send("\n".join(lines))
