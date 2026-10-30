#!/data/data/com.termux/files/usr/bin/env python
from __future__ import annotations

import multiprocessing as mp
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import tree_sitter_vim
from tree_sitter import Language, Node, Parser

PathLike = str | Path


@dataclass
class ProcessResult:
    path: Path
    success: bool
    comments_removed: int = 0
    error_message: str = ""
    processing_time: float = 0.0


class VimCommentRemover:
    def __init__(self):
        self.parser = Parser()
        language = Language(tree_sitter_vim.language())
        self.parser.language = language

    def _is_comment_node(self, node: Node) -> bool:
        return node.type == "comment"

    def _get_comment_ranges(self, root_node: Node) -> list[tuple[int, int]]:
        comment_ranges = []

        def visit_node(node: Node):
            if self._is_comment_node(node):
                comment_ranges.append((node.start_byte, node.end_byte))
                return
            for child in node.children:
                visit_node(child)

        visit_node(root_node)
        return comment_ranges

    def remove_comments(self, content: bytes) -> tuple[bytes, int]:
        tree = self.parser.parse(content)
        comment_ranges = self._get_comment_ranges(tree.root_node)
        if not comment_ranges:
            return content, 0
        result_parts = []
        last_end = 0
        comments_removed = 0
        for start, end in comment_ranges:
            result_parts.append(content[last_end:start])
            line_start = content.rfind(b"\n", 0, start) + 1
            before_comment = content[line_start:start]
            if before_comment.strip() == b"":
                line_end = content.find(b"\n", end)
                if line_end == -1:
                    line_end = len(content)
                else:
                    line_end += 1
                last_end = max(last_end, line_end)
            else:
                last_end = end
            comments_removed += 1
        result_parts.append(content[last_end:])
        processed_content = b"".join(result_parts)
        while b"\n\n\n" in processed_content:
            processed_content = processed_content.replace(b"\n\n\n", b"\n\n")
        return processed_content, comments_removed


def collect_vim_files(inputs: list[str]) -> list[Path]:
    vim_files = []
    if not inputs:
        inputs = ["."]
    for input_path in inputs:
        path = Path(input_path)
        if path.is_file():
            if path.suffix == ".vim":
                vim_files.append(path)
            else:
                print(f"Warning: {path} is not a .vim file, skipping")
        elif path.is_dir():
            vim_files.extend(path.rglob("*.vim"))
        else:
            print(f"Warning: {path} does not exist, skipping")
    seen = set()
    unique_files = []
    for f in vim_files:
        resolved = f.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique_files.append(resolved)
    return unique_files


def process_file(path: Path) -> ProcessResult:
    start_time = time.perf_counter()
    try:
        remover = VimCommentRemover()
        with open(path, "rb") as f:
            content = f.read()
        processed_content, comments_removed = remover.remove_comments(content)
        if comments_removed > 0:
            temp_path = path.with_suffix(path.suffix + ".tmp")
            with open(temp_path, "wb") as f:
                f.write(processed_content)
            import os

            original_mode = os.stat(path).st_mode
            os.chmod(temp_path, original_mode)
            temp_path.replace(path)
        else:
            comments_removed = 0
        processing_time = time.perf_counter() - start_time
        return ProcessResult(
            path=path,
            success=True,
            comments_removed=comments_removed,
            processing_time=processing_time,
        )
    except Exception as e:
        processing_time = time.perf_counter() - start_time
        return ProcessResult(
            path=path,
            success=False,
            error_message=str(e),
            processing_time=processing_time,
        )


def process_files_parallel(files: list[Path], num_workers: int = 8) -> list[ProcessResult]:
    results = []
    with mp.Pool(processes=num_workers) as pool:
        async_results = []
        for path in files:
            async_result = pool.apply_async(process_file, (path,))
            async_results.append(async_result)
        total_files = len(async_results)
        completed = 0
        for async_result in async_results:
            try:
                result = async_result.get(timeout=30)
                results.append(result)
                completed += 1
                if result.success:
                    if result.comments_removed > 0:
                        print(
                            f"✓ {result.path}: removed {result.comments_removed} comments "
                            f"({result.processing_time:.3f}s)"
                        )
                    else:
                        print(f"• {result.path}: no comments found ({result.processing_time:.3f}s)")
                else:
                    print(f"✗ {result.path}: ERROR - {result.error_message}")
                if completed % 10 == 0:
                    print(f"Progress: {completed}/{total_files} files processed")
            except mp.TimeoutError:
                print(f"✗ Timeout processing file (30s limit)")
                results.append(
                    ProcessResult(
                        path=Path("unknown"),
                        success=False,
                        error_message="Timeout exceeded 30 seconds",
                    )
                )
    return results


def print_summary(results: list[ProcessResult], total_files: int, start_time: float):
    total_time = time.perf_counter() - start_time
    successful = sum(1 for r in results if r.success)
    failed = sum(1 for r in results if not r.success)
    total_comments_removed = sum(r.comments_removed for r in results if r.success)
    files_with_comments = sum(1 for r in results if r.success and r.comments_removed > 0)
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"Total files processed:     {total_files}")
    print(f"Successful:                {successful}")
    print(f"Failed:                    {failed}")
    print(f"Files with comments:       {files_with_comments}")
    print(f"Files without comments:    {successful - files_with_comments}")
    print(f"Total comments removed:    {total_comments_removed}")
    print(f"Total processing time:     {total_time:.2f}s")
    if successful > 0:
        avg_time = total_time / successful
        print(f"Average time per file:     {avg_time:.3f}s")
    print("=" * 60)


def main():
    inputs = sys.argv[1:]
    print("Collecting .vim files...")
    vim_files = collect_vim_files(inputs)
    if not vim_files:
        print("No .vim files found to process.")
        return
    print(f"Found {len(vim_files)} .vim file(s) to process")
    print(f"Using 8 worker processes\n")
    start_time = time.perf_counter()
    results = process_files_parallel(vim_files, num_workers=8)
    print_summary(results, len(vim_files), start_time)


if __name__ == "__main__":
    mp.freeze_support()
    main()
