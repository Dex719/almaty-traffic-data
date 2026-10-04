"""One durable observation cycle (``python -m collector``). Use collector.shift for continuous operation."""
import sys
from collector.shift import main

if __name__ == "__main__":
    sys.argv.insert(1, "--once")
    raise SystemExit(main())
