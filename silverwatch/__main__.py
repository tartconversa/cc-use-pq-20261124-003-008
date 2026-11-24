from __future__ import annotations

import argparse
import json

from silverwatch.demo import run_fixture


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a local Silverwatch workflow fixture")
    parser.add_argument("--fixture", required=True)
    args = parser.parse_args()
    result = run_fixture(args.fixture)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
