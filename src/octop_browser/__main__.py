"""Package entry point so ``python -m octop_browser`` works.

The console script (``octop-browser``) and ``python -m octop_browser.cli``
already worked, but the README quick start and ``make run`` document
``python -m octop_browser``, which failed with
``No module named octop_browser.__main__``.
"""

from __future__ import annotations

from octop_browser.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
