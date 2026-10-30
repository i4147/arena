#!/data/data/com.termux/files/usr/bin/env python
from __future__ import annotations

import os
import shutil
import subprocess
import sys


def pdf_to_png(pdf_path, output_dir=None, dpi=200):
    if not os.path.isfile(pdf_path):
        print(f"Error: file not found: {pdf_path}", file=sys.stderr)
        sys.exit(1)
    if shutil.which("pdftoppm") is None:
        print(
            "Error: 'pdftoppm' not found. Install with: pkg install poppler",
            file=sys.stderr,
        )
        sys.exit(1)
    base_name = os.path.splitext(os.path.basename(pdf_path))[0]
    if output_dir is None:
        output_dir = base_name
    os.makedirs(output_dir, exist_ok=True)
    prefix = os.path.join(output_dir, f"{base_name}_page")
    cmd = [
        "pdftoppm",
        "-png",
        "-r",
        str(dpi),
        pdf_path,
        prefix,
    ]
    print(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print("pdftoppm failed:", file=sys.stderr)
        print(result.stderr, file=sys.stderr)
        sys.exit(1)
    generated = sorted(f for f in os.listdir(output_dir) if f.startswith(f"{base_name}_page-"))
    for f in generated:
        num_part = f.rsplit("-", 1)[-1].split(".")[0]
        try:
            num = int(num_part)
        except ValueError:
            continue
        new_name = f"{base_name}_page_{num:03d}.png"
        os.rename(os.path.join(output_dir, f), os.path.join(output_dir, new_name))
        print(f"  {new_name}")
    print(f"Done. {len(generated)} image(s) saved to '{output_dir}/'")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    input_pdf = sys.argv[1]
    output_directory = sys.argv[2] if len(sys.argv) > 2 else None
    dpi_value = int(sys.argv[3]) if len(sys.argv) > 3 else 200
    pdf_to_png(input_pdf, output_directory, dpi_value)
