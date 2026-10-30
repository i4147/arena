#!/data/data/com.termux/files/usr/bin/env python
from __future__ import annotations

import argparse
import difflib
import multiprocessing
import os
import shutil
import sys
from pathlib import Path
from typing import Iterator

import tree_sitter_lua as ts_lua
from tree_sitter import Language, Parser, Query, QueryCursor

try:
    import luaparse
except ImportError:
    luaparse = None
_WORKERS = os.cpu_count() or 1
_SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".idea", ".vscode", "dist", "build"}


def process_file_tree_sitter(raw_bytes: bytes) -> tuple[bytes, bool]:
    orig_size = len(raw_bytes)
    if orig_size == 0:
        return raw_bytes, True
    language = Language(ts_lua.language())
    parser = Parser(language)
    tree = parser.parse(raw_bytes)
    if tree.root_node.has_error:
        return b"", False
    query = Query(language, "(comment) @comment")
    cursor = QueryCursor(query)
    captures = cursor.captures(tree.root_node)
    comment_nodes = captures.get("comment", [])
    if not comment_nodes:
        return raw_bytes, True
    comment_ranges = []
    for node in comment_nodes:
        comment_bytes = raw_bytes[node.start_byte : node.end_byte]
        if comment_bytes.lstrip().startswith(b"---"):
            continue
        comment_ranges.append((node.start_byte, node.end_byte))
    if not comment_ranges:
        return raw_bytes, True
    comment_ranges.sort()
    merged_ranges = []
    for start, end in comment_ranges:
        if merged_ranges and start <= merged_ranges[-1][1]:
            merged_ranges[-1] = (merged_ranges[-1][0], max(merged_ranges[-1][1], end))
        else:
            merged_ranges.append((start, end))
    new_bytes_parts = []
    last_idx = 0
    for start, end in merged_ranges:
        if start > last_idx:
            new_bytes_parts.append(raw_bytes[last_idx:start])
        last_idx = end
    if last_idx < orig_size:
        new_bytes_parts.append(raw_bytes[last_idx:])
    new_bytes = b"".join(new_bytes_parts)
    new_tree = parser.parse(new_bytes)
    if new_tree.root_node.has_error:
        return b"", False
    return new_bytes, True


def process_file_luaparse(raw_bytes: bytes) -> tuple[bytes, bool]:
    if luaparse is None:
        return b"", False
    try:
        text = raw_bytes.decode("utf-8")
        luaparse.parse(text)
        return raw_bytes, True
    except Exception:
        return b"", False


def process_lua_file(
    args_tuple: tuple[str, str, str, bool, bool],
) -> tuple[str, int, int, int, str | None, str, str]:
    file_path_str, base_dir_str, backend, dry_run, backup = args_tuple
    path = Path(file_path_str)
    try:
        if not path.is_file():
            return (file_path_str, 0, 0, 0, "Not a valid file", "", "")
        try:
            raw_bytes = path.read_bytes()
        except Exception as e:
            return (file_path_str, 0, 0, 0, f"Read error: {e}", "", "")
        orig_size = len(raw_bytes)
        if orig_size == 0:
            rel_path = os.path.relpath(file_path_str, base_dir_str)
            return (rel_path, 0, 0, 0, None, "", "")
        try:
            old_text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return (file_path_str, 0, 0, 0, "Invalid UTF-8 encoding", "", "")
        if backend == "luaparse":
            new_bytes, success = process_file_luaparse(raw_bytes)
        else:
            new_bytes, success = process_file_tree_sitter(raw_bytes)
        if not success:
            return (file_path_str, 0, 0, 0, "Invalid Lua or validation failed", "", "")
        try:
            new_text = new_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return (file_path_str, 0, 0, 0, "Invalid UTF-8 encoding in result", "", "")
        new_size = len(new_bytes)
        saved = orig_size - new_size
        if not dry_run and saved > 0:
            if backup:
                backup_path = path.with_suffix(path.suffix + ".bak")
                shutil.copy2(path, backup_path)
            try:
                path.write_bytes(new_bytes)
            except Exception as e:
                return (file_path_str, 0, 0, 0, f"Write error: {e}", "", "")
        rel_path = os.path.relpath(file_path_str, base_dir_str)
        return (rel_path, orig_size, new_size, saved, None, old_text, new_text)
    except Exception as e:
        return (file_path_str, 0, 0, 0, str(e), "", "")


def iter_lua_files(paths: list[str]) -> Iterator[str]:
    for p_str in paths:
        path = Path(p_str)
        if not path.exists() or path.is_symlink():
            continue
        if path.is_file():
            if not path.name.startswith(".") and path.suffix.lower() == ".lua":
                yield str(path.resolve())
        elif path.is_dir():
            for root, dirs, files in os.walk(path, followlinks=False):
                dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
                for file in files:
                    if file.startswith("."):
                        continue
                    if file.lower().endswith(".lua"):
                        full_path = Path(root) / file
                        if not full_path.is_symlink():
                            yield str(full_path.resolve())


def main() -> None:
    parser_arg = argparse.ArgumentParser()
    parser_arg.add_argument("paths", nargs="*", default=[])
    parser_arg.add_argument("-b", "--backend", choices=["tree-sitter", "luaparse"], default="tree-sitter")
    parser_arg.add_argument("--diff", action="store_true")
    parser_arg.add_argument("--dry-run", action="store_true")
    parser_arg.add_argument("--backup", action="store_true")
    parsed_args = parser_arg.parse_args()
    input_paths = parsed_args.paths
    if not input_paths:
        input_paths = ["."]
    base_dir = os.getcwd()
    files_list = list(iter_lua_files(input_paths))
    if not files_list:
        sys.exit(0)
    total_saved = 0
    error_count = 0
    tasks = [(f, base_dir, parsed_args.backend, parsed_args.dry_run, parsed_args.backup) for f in files_list]
    if len(files_list) == 1:
        for task in tasks:
            res = process_lua_file(task)
            rel_path, orig, new, saved, err, old_text, new_text = res
            if err:
                error_count += 1
            else:
                total_saved += saved
                green_saved = f"\033[32m{saved}\033[0m"
                print(f"{rel_path}  {green_saved}")
                if parsed_args.diff and saved > 0:
                    diff = difflib.unified_diff(
                        old_text.splitlines(keepends=True),
                        new_text.splitlines(keepends=True),
                        fromfile=f"a/{rel_path}",
                        tofile=f"b/{rel_path}",
                    )
                    sys.stdout.writelines(diff)
    else:
        with multiprocessing.Pool(_WORKERS) as pool:
            for res in pool.imap_unordered(process_lua_file, tasks):
                rel_path, _orig, _new, saved, err, old_text, new_text = res
                if err:
                    error_count += 1
                else:
                    total_saved += saved
                    green_saved = f"\033[32m{saved}\033[0m"
                    print(f"{rel_path}  {green_saved}")
                    if parsed_args.diff and saved > 0:
                        diff = difflib.unified_diff(
                            old_text.splitlines(keepends=True),
                            new_text.splitlines(keepends=True),
                            fromfile=f"a/{rel_path}",
                            tofile=f"b/{rel_path}",
                        )
                        sys.stdout.writelines(diff)
    print(f"Total space freed: {total_saved} bytes")


if __name__ == "__main__":
    main()
