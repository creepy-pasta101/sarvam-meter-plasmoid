# SPDX-License-Identifier: MIT
"""Entry point for ``python -m sarvam_meter``."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
