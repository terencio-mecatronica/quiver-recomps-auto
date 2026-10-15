#!/usr/bin/env python3
"""Verifica formato e unicidade de todos os arquivos para importacao."""
from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[1]


def validate_file(path: Path) -> int:
    d = json.loads(path.read_text(encoding="utf-8"))
    apps = d.get("apps")
    assert isinstance(apps, list), f"{path}: apps nao e array"
    folders = set()
    for x in apps:
        assert isinstance(x, dict)
        for key in ("name", "folderName"):
            assert isinstance(x.get(key), str) and x[key].strip(), (path, key)
        assert isinstance(x.get("tags", []), list)
        assert x.get("appIconUrl") is None or str(x["appIconUrl"]).startswith("https://")
        if "repository" in x:
            assert isinstance(x["repository"], str) and "/" in x["repository"]
        assert x["folderName"].lower() not in folders, f"Pasta duplicada: {x['folderName']}"
        folders.add(x["folderName"].lower())
    print(f"OK: {path.name}: {len(apps)} apps")
    return len(apps)


if __name__ == "__main__":
    for name in ("windows-jogaveis.json", "em-desenvolvimento.json", "tudo-quiver.json"):
        validate_file(ROOT / "output" / name)
