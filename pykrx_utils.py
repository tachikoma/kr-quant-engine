"""pykrx 호출 관련 유틸리티

- fd 수준(stdout/stderr) 캡처와 주말 범위 스킵 판별 함수를 제공합니다.
- 원래 `run_etf_backtest.py`에 있던 로직을 공용 모듈로 추출했습니다.

모든 주석은 한국어로 작성되어 있으며, 함수 이름은 기존 코드와 호환되도록 언더스코어 접두사를 유지합니다.
"""
from __future__ import annotations

import io
import os
import contextlib
import sys
import pandas as pd


def _call_capture_stderr(func, *args, **kwargs):
    """pykrx 호출 시 Python 레벨 출력과 OS fd(1/2) 레벨 출력을 함께 캡처합니다.

    - Python 레벨 출력은 `contextlib.redirect_stdout/redirect_stderr`로 캡처합니다.
    - C/확장 모듈이 직접 쓰는 fd(1/2)는 `os.pipe()` + `os.dup2()`로 임시 리다이렉트해 캡처합니다.

    캡처된 출력은 각각 `[pykrx-stdout]` / `[pykrx-stderr]`로 재로그됩니다.
    """
    buf_out = io.StringIO()
    buf_err = io.StringIO()

    # 파이프 생성 (read, write)
    out_r, out_w = os.pipe()
    err_r, err_w = os.pipe()

    # 현재 stdout/stderr 백업
    saved_stdout = os.dup(1)
    saved_stderr = os.dup(2)

    res = None
    exc = None
    try:
        # fd-level 리다이렉트: fd(1)->out_w, fd(2)->err_w
        os.dup2(out_w, 1)
        os.dup2(err_w, 2)
        # 로컬 복사 닫기
        os.close(out_w)
        os.close(err_w)

        try:
            with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                res = func(*args, **kwargs)
        except Exception as e:
            exc = e
    finally:
        # Python-level 스트림 flush
        try:
            sys.stdout.flush()
        except Exception:
            pass
        try:
            sys.stderr.flush()
        except Exception:
            pass

        # 원래 fd 복원
        try:
            os.dup2(saved_stdout, 1)
            os.dup2(saved_stderr, 2)
        except Exception:
            pass
        try:
            os.close(saved_stdout)
        except Exception:
            pass
        try:
            os.close(saved_stderr)
        except Exception:
            pass

        # 파이프에서 읽기
        out_bytes = b""
        err_bytes = b""
        try:
            def _read_all(fd):
                chunks = []
                while True:
                    chunk = os.read(fd, 8192)
                    if not chunk:
                        break
                    chunks.append(chunk)
                return b"".join(chunks)

            out_bytes = _read_all(out_r)
            err_bytes = _read_all(err_r)
        except Exception:
            pass
        finally:
            try:
                os.close(out_r)
            except Exception:
                pass
            try:
                os.close(err_r)
            except Exception:
                pass

    # 버퍼 결합 및 출력
    py_out = buf_out.getvalue().strip()
    py_err = buf_err.getvalue().strip()
    fd_out = out_bytes.decode(errors="ignore").strip()
    fd_err = err_bytes.decode(errors="ignore").strip()

    combined_out = "\n".join([s for s in (py_out, fd_out) if s])
    combined_err = "\n".join([s for s in (py_err, fd_err) if s])

    if combined_out:
        print(f"[pykrx-stdout] {combined_out}")
    if combined_err:
        print(f"[pykrx-stderr] {combined_err}")

    if exc:
        raise exc
    return res


def _range_has_weekday(start_ymd: str, end_ymd: str) -> bool:
    """주어진 YYYYMMDD 범위에 평일(Mon-Fri)이 하나라도 있는지 확인합니다.

    - 파싱 실패 또는 역구간인 경우 보수적으로 `True`를 반환하여 조회를 허용합니다.
    - 공휴일 판별은 하지 않으므로 주말만 가득한 경우에만 조회를 건너뜁니다.
    """
    try:
        s = pd.to_datetime(start_ymd, errors="coerce")
        e = pd.to_datetime(end_ymd, errors="coerce")
    except Exception:
        return True
    if pd.isna(s) or pd.isna(e) or s > e:
        return True
    try:
        dr = pd.date_range(s, e, freq="D")
        return any(d.weekday() < 5 for d in dr)
    except Exception:
        return True
