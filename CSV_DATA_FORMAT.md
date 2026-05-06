# CSV 대체 입력 포맷 (ETF 기준)

기본 실행(run_etf_backtest.py)은 pykrx를 직접 호출합니다.
아래 포맷은 데이터 소스를 커스텀할 때 맞춰야 하는 최소 스키마입니다.

## 1. ETF 가격 데이터 (price.csv)

필수 컬럼:

```csv
date,ticker,open,close,volume,trading_value
2024-01-02,069500,34100,34450,1234567,42000000000
```

설명:

- date: 거래일
- ticker: ETF 티커(문자열)
- open, close: 체결/평가 가격
- volume: 거래량
- trading_value: 거래대금

참고: run_etf_backtest.py 내부 정규화 로직은 high, low 없이도 동작합니다.

## 2. 지수 데이터 (index.csv)

시장 필터를 외부 데이터로 대체하려면 아래 컬럼이 필요합니다.

```csv
date,close
2024-01-02,2650.21
```

이후 로직에서 이동평균/기울기 기반 risk_on 신호를 계산합니다.

## 3. ETF 유니버스 목록 (선택)

기본값은 run_etf_backtest.py의 ETF_LIST를 사용합니다.
운영 중 CSV로 관리하려면 ticker 한 컬럼만 두는 형식을 권장합니다.

```csv
ticker
069500
229200
091160
```

## 4. 데이터 품질 체크

- date 형식은 YYYY-MM-DD 또는 YYYYMMDD를 일관되게 사용
- ticker 앞자리 0이 사라지지 않도록 문자열로 처리
- open/close가 0 이하인 레코드는 제외
- 거래정지/결측 구간은 사전에 정제
