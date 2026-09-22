"""Allow `python -m prism` to run the CLI (serve/setup)."""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
