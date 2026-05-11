#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
간단 파일 모니터: outputs_grid 디렉터리의 새 파일을 감시합니다.
환경변수:
 - WATCH_DIR: 감시할 디렉터리 (기본 'outputs_grid')
 - WATCH_PATTERN: 발견시 특별 알림을 할 파일명 패턴 (기본 'filtered_etf_list.json')
 - WATCH_INTERVAL: 폴링 간격(초, 기본 5)

사용법: python3 -u scripts/monitor_outputs.py
"""

from pathlib import Path
import os
import time
import sys

WATCH_DIR = Path(os.environ.get("WATCH_DIR", "outputs_grid"))
PATTERN = os.environ.get("WATCH_PATTERN", "filtered_etf_list.json")
try:
    INTERVAL = float(os.environ.get("WATCH_INTERVAL", "5"))
except Exception:
    INTERVAL = 5.0

if not WATCH_DIR.exists():
    print(f"경고: 감시 디렉터리 {WATCH_DIR}가 존재하지 않습니다. 생성 대기 중...")

seen = set()
if WATCH_DIR.exists():
    try:
        seen = set(WATCH_DIR.iterdir())
    except Exception as e:
        print("초기 파일 읽기 실패:", e)

print(f"모니터 시작: {WATCH_DIR} (패턴={PATTERN}) — 폴링 간격={INTERVAL}s")
sys.stdout.flush()

try:
    while True:
        if not WATCH_DIR.exists():
            print(f"{WATCH_DIR} 존재하지 않음. 대기...")
            sys.stdout.flush()
            time.sleep(INTERVAL)
            continue

        try:
            current = set(WATCH_DIR.iterdir())
        except Exception as e:
            print("디렉터리 읽기 실패:", e)
            time.sleep(INTERVAL)
            continue

        added = current - seen
        if added:
            for p in sorted(added):
                print("새 파일:", p.name)
                if PATTERN in p.name:
                    print("=== 패턴 발견 ===", p.name)
                    try:
                        print("--- 파일 내용(최대 50줄) ---")
                        with p.open(encoding='utf-8') as f:
                            for i, line in enumerate(f):
                                if i >= 50:
                                    print("... (출력생략)")
                                    break
                                print(line.rstrip())
                        print("--- 파일 내용 끝 ---")
                    except Exception as e:
                        print("파일 읽기 실패:", e)
        seen = current
        sys.stdout.flush()
        time.sleep(INTERVAL)
except KeyboardInterrupt:
    print("모니터 종료 (사용자 중단)")
    sys.exit(0)
