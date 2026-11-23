from __future__ import annotations

import ast
import re
import sys
from pathlib import Path


def main() -> int:
    source_path = Path(sys.argv[1] if len(sys.argv) > 1 else "main.py")
    output_path = Path(sys.argv[2] if len(sys.argv) > 2 else "/tmp/pvpclubtiers-inline.js")
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    html = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == "PVPCLUBTIERS_PAGE" for target in node.targets):
                html = ast.literal_eval(node.value)
                break
    if not isinstance(html, str):
        raise SystemExit("PVPCLUBTIERS_PAGE string assignment was not found in main.py")

    scripts = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script\s*>", html, flags=re.IGNORECASE | re.DOTALL)
    if not scripts:
        raise SystemExit("No inline JavaScript blocks found in PVPCLUBTIERS_PAGE")
    output_path.write_text("\n;\n".join(scripts), encoding="utf-8")
    print(f"Extracted {len(scripts)} inline script block(s) to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
