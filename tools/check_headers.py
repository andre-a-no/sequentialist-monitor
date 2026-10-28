# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""Every Python source file of the repository starts with the SPDX header of the project (no city in it)."""
import sys
from pathlib import Path

HEADER = [
    "# SPDX-License-Identifier: AGPL-3.0-only",
    "# Copyright (C) 2026 Novikov Laboratories LLC",
    "# Commercial licenses for use outside the AGPL: see COMMERCIAL.md",
]


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    bad = []
    for path in sorted(root.glob("**/*.py")):
        if any(part.startswith(".") or part in ("build", "dist") for part in path.relative_to(root).parts):
            continue
        lines = path.read_text(encoding="utf-8").splitlines()[:3]
        if lines != HEADER:
            bad.append(str(path.relative_to(root)))
    for name in bad:
        print(f"{name}: missing or different SPDX header", file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
