"""Cho phép chạy ``python -m finagent``."""
import sys

from finagent.cli import main

if __name__ == "__main__":
    sys.exit(main())
