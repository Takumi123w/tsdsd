"""PyInstaller entry point. Run ``python3 tsdm_app.py`` for the same behaviour."""

import sys

from tsdm.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
