"""정규화 ETF 분배금 파일의 범위와 종목별 건수를 점검한다."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import etf_shared
from etf_distributions import distributions_file_sha256, distributions_path, load_distributions


def main() -> None:
    if etf_shared.UNIVERSE_MODE == "auto":
        etf_shared.ensure_universe_initialized()
    universe = list(etf_shared.ETF_LIST)
    path = distributions_path()
    data = load_distributions(path, required=False)
    if data.empty:
        print(
            json.dumps(
                {
                    "path": str(path),
                    "sha256": distributions_file_sha256(path),
                    "ready_for_total_return": False,
                    "event_count": 0,
                    "message": "분배금 템플릿이 비어 있습니다.",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    counts = data.groupby("ticker").size().to_dict()
    report = {
        "path": str(path),
        "sha256": distributions_file_sha256(path),
        "ready_for_total_return": True,
        "event_count": len(data),
        "start": str(data["ex_date"].min().date()),
        "end": str(data["ex_date"].max().date()),
        "events_by_ticker": counts,
        "universe_without_events": sorted(set(universe) - set(counts)),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
