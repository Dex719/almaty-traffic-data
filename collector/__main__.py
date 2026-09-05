"""One durable observation cycle. Use collector.shift for continuous operation."""
from pathlib import Path
import sys
from collector.shift import DATA_DIR, main, run_shift
from collector.ops import exclusive_collector


def collect_once(data_dir: Path = DATA_DIR) -> int:
    with exclusive_collector(data_dir):
        return run_shift(1, data_dir, once=True)


if __name__ == "__main__":
    sys.argv.insert(1, "--once")
    raise SystemExit(main())
