#!/data/data/com.termux/files/usr/bin/env python
from __future__ import annotations

import multiprocessing as mp
import sys
import time
from pathlib import Path

try:
    import tree_sitter_rust
    from tree_sitter import Language, Parser

    TREE_SITTER_AVAILABLE = True
except ImportError:
    TREE_SITTER_AVAILABLE = False
    print("Error: tree-sitter and tree-sitter-rust are required.")
    print("Install with: pip install tree-sitter tree-sitter-rust")
    sys.exit(1)
NUM_WORKERS = 8
RUST_EXTENSIONS = {".rs"}


class RustCommentStripper:
    def __init__(self):
        self.language = Language(tree_sitter_rust.language())
        self.parser = Parser(self.language)

    def strip_comments(self, source_code: str) -> str:
        tree = self.parser.parse(source_code.encode("utf-8"))
        comments = []
        self._collect_comments(tree.root_node, comments)
        if not comments:
            return source_code
        comments.sort(key=lambda node: (node.start_point[0], node.start_point[1]))
        result = []
        last_end = 0
        for comment in comments:
            start_byte = comment.start_byte
            end_byte = comment.end_byte
            result.append(source_code[last_end:start_byte])
            if comment.type == "line_comment":
                line_end = source_code.find("\n", end_byte)
                if line_end == -1:
                    line_end = len(source_code)
                else:
                    line_end += 1
                last_end = line_end
            else:
                last_end = end_byte
        result.append(source_code[last_end:])
        return "".join(result)

    def _collect_comments(self, node, comments: list):
        if node.type in ("line_comment", "block_comment"):
            comments.append(node)
        for child in node.children:
            self._collect_comments(child, comments)


def find_rust_files(paths: list[str]) -> set[Path]:
    rust_files = set()
    if not paths:
        paths = ["."]
    for path_str in paths:
        path = Path(path_str)
        if path.is_file():
            if path.suffix in RUST_EXTENSIONS:
                rust_files.add(path.resolve())
        elif path.is_dir():
            for path in path.rglob("*"):
                if path.is_file() and path.suffix in RUST_EXTENSIONS:
                    rust_files.add(path.resolve())
        else:
            print(f"Warning: Path '{path_str}' does not exist", file=sys.stderr)
    return rust_files


def process_file(path: Path) -> tuple[Path, bool, str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            original_content = f.read()
        if not original_content.strip():
            return (path, True, "")
        stripper = RustCommentStripper()
        stripped_content = stripper.strip_comments(original_content)
        if stripped_content != original_content:
            temp_path = path.with_suffix(path.suffix + ".tmp")
            with open(temp_path, "w", encoding="utf-8") as f:
                f.write(stripped_content)
            temp_path.replace(path)
        return (path, True, "")
    except Exception as e:
        return (path, False, str(e))


def process_files_parallel(files: set[Path]):
    files_list = list(files)
    total_files = len(files_list)
    if total_files == 0:
        print("No .rs files found to process.")
        return
    print(f"Processing {total_files} Rust file(s) using {NUM_WORKERS} workers...")
    start_time = time.time()
    with mp.Pool(processes=NUM_WORKERS) as pool:
        results = []
        for path in files_list:
            result = pool.apply_async(process_file, (path,))
            results.append(result)
        processed = 0
        success_count = 0
        error_count = 0
        for result in results:
            try:
                path, success, error_msg = result.get(timeout=30)
                processed += 1
                if success:
                    success_count += 1
                else:
                    error_count += 1
                    print(f"Error processing {path}: {error_msg}", file=sys.stderr)
                if processed % 100 == 0 or processed == total_files:
                    print(f"Progress: {processed}/{total_files} files processed")
            except Exception as e:
                error_count += 1
                processed += 1
                print(f"Error getting result: {e}", file=sys.stderr)
    elapsed_time = time.time() - start_time
    print(f"\nProcessing complete:")
    print(f"  Total files: {total_files}")
    print(f"  Successful: {success_count}")
    print(f"  Failed: {error_count}")
    print(f"  Time elapsed: {elapsed_time:.2f} seconds")


def main():
    input_paths = sys.argv[1:]
    try:
        rust_files = find_rust_files(input_paths)
    except Exception as e:
        print(f"Error finding Rust files: {e}", file=sys.stderr)
        sys.exit(1)
    try:
        process_files_parallel(rust_files)
    except KeyboardInterrupt:
        print("\nInterrupted by user. Exiting...")
        sys.exit(130)
    except Exception as e:
        print(f"Error during processing: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
