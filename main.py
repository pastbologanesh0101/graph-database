#!/usr/bin/env python3
"""Entry point: `python main.py` for the REPL, `python main.py file.gqs` for script mode."""

import sys

from graphdb.cli import main

if __name__ == "__main__":
    sys.exit(main())
