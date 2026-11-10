"""E5 capstone CLI: build the Generic 60k-Seat Stadium as real stored objects
(RF project + labor estimate + vendor quotes) and write the morning bid
package to dist/stadium-package/.

Usage:
  .venv/bin/python scripts/build_stadium_package.py [--no-package]
      [--package-dir DIR] [--actor NAME]

Idempotent: existing project/estimate/quote rows are reused, never duplicated.
Defaults: package dir = $ROMTOOL_STADIUM_PACKAGE or <repo>/dist/stadium-package;
database = $ROMTOOL_DB or the repo's data/romtool.db (same store the app runs).
"""

from romtool.stadium_package import main

if __name__ == "__main__":
    main()
