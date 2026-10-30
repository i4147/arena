#!/data/data/com.termux/files/usr/bin/env python
import lzma
import os
import sys
from pathlib import Path

import py7zr


def main() -> int:
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <file>", file=sys.stderr)
        return 2
    src = Path(sys.argv[1])
    if not src.is_file():
        print(f"Error: '{src}' is not a regular file", file=sys.stderr)
        return 1
    dst = src.with_name(src.name + ".7z")
    if dst.exists():
        print(f"Error: '{dst}' already exists", file=sys.stderr)
        return 1
    filters = [
        {
            "id": py7zr.FILTER_LZMA2,
            "preset": 9 | lzma.PRESET_EXTREME,
        }
    ]
    tmp = dst.with_name(dst.name + ".tmp")
    try:
        with py7zr.SevenZipFile(tmp, "w", filters=filters) as z:
            z.write(src, arcname=src.name)
        os.replace(tmp, dst)
    except BaseException:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise
    src.unlink()
    print(f"OK: '{src}' -> '{dst}' (original removed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
