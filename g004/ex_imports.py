#!/data/data/com.termux/files/usr/bin/env python
from __future__ import annotations

from pathlib import Path

import tree_sitter_python as tsp
from dh import get_files, mpf, unique_path
from tree_sitter import Language, Parser

OUTPUT_DIR = Path.home() / "tmp" / "output"
parser = Parser()
parser.language = Language(tsp.language())
VALID = {"import_statement", "import_from_statement"}


def process_file(path):
    path = Path(path)
    src = path.read_bytes()
    tree = parser.parse(src)
    root = tree.root_node
    return [src[node.start_byte : node.end_byte].decode() for node in root.children if node.type in VALID]


def main() -> None:
    cwd = Path.cwd()
    outfile = OUTPUT_DIR / f"{cwd.name}_importz.py"
    if outfile.exists():
        outfile = unique_path(outfile)
    all_imports = []
    files = get_files(cwd, ext=[".py"])
    results = mpf(process_file, files)
    for imports in results:
        if imports:
            for k in imports:
                if not k.startswith("from .") and k not in all_imports:
                    all_imports.append(k)
    all_imports = sorted(set(all_imports))
    outfile.write_text("\n".join(all_imports), encoding="utf-8")
    print("done.")


if __name__ == "__main__":
    raise SystemExit(main())
