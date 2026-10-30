#!/data/data/com.termux/files/usr/bin/env python
from __future__ import annotations

import os
import shutil
import sys
import tarfile
import tempfile

import zstandard as zstd


def delete_from_tar_zst(archive_path, file_to_delete):
    if not os.path.exists(archive_path):
        print(f"Error: Archive '{archive_path}' not found", file=sys.stderr)
        sys.exit(1)
    base, ext = os.path.splitext(archive_path)
    if ext == ".zst":
        output_path = f"{base}_modified.tar.zst"
    else:
        output_path = f"{archive_path}_modified"
    print(f"Reading archive: {archive_path}")
    print(f"Deleting: {file_to_delete}")
    with open(archive_path, "rb") as f:
        compressed_data = f.read()
    decompressor = zstd.ZstdDecompressor()
    tar_data = decompressor.decompress(compressed_data)
    with tempfile.NamedTemporaryFile(delete=False, suffix=".tar") as tmp_tar:
        tmp_tar.write(tar_data)
        tmp_path = tmp_tar.name
    try:
        with tarfile.open(tmp_path, "r:") as tar:
            members = tar.getmembers()
            found = False
            for member in members:
                if member.name == file_to_delete:
                    found = True
                    break
            if not found:
                print(f"Warning: '{file_to_delete}' not found in archive", file=sys.stderr)
                shutil.copy2(archive_path, output_path)
                print(f"Original archive copied to: {output_path}")
                return
            with tempfile.NamedTemporaryFile(delete=False, suffix=".tar") as new_tar:
                with tarfile.open(new_tar.name, "w") as new_tarf:
                    for member in members:
                        if member.name != file_to_delete:
                            f = tar.extractfile(member)
                            if f is not None:
                                new_tarf.addfile(member, f)
                            else:
                                new_tarf.addfile(member)
                new_tar_path = new_tar.name
            with open(new_tar_path, "rb") as f:
                new_tar_data = f.read()
            os.unlink(new_tar_path)
        compressor = zstd.ZstdCompressor(level=3)
        compressed_output = compressor.compress(new_tar_data)
        with open(output_path, "wb") as f:
            f.write(compressed_output)
        print(f"✅ Success! New archive created: {output_path}")
        print(f"   Size: {os.path.getsize(output_path)} bytes")
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def main():
    if len(sys.argv) != 3:
        print("Usage: python script.py <archive.tar.zst> <file-to-delete>")
        print("Example: python script.py archive.tar.zst 0/document.pdf")
        sys.exit(1)
    archive_path = sys.argv[1]
    file_to_delete = sys.argv[2]
    delete_from_tar_zst(archive_path, file_to_delete)


if __name__ == "__main__":
    raise SystemExit(main())
