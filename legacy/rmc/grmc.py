#!/data/data/com.termux/files/usr/bin/env python
import argparse
import concurrent.futures
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

try:
    import tree_sitter
    import tree_sitter_python as ts_python

    HAS_TREESITTER = True
except ImportError:
    HAS_TREESITTER = False
try:
    import libcst as cst

    HAS_LIBCST = True
except ImportError:
    HAS_LIBCST = False
ANSI_GREEN = "\033[32m"
ANSI_DIM = "\033[2m"
ANSI_RESET = "\033[0m"


def is_comment_preserved(comment_text: str, is_shebang: bool = False) -> bool:
    if is_shebang:
        return True
    directives = ["noqa", "type:", "pylint:", "pragma:", "mypy:", "ruff:", "flake8:"]
    lower_text = comment_text.lower()
    if any(d in lower_text for d in directives):
        return True
    code_keywords = ["def ", "class ", "import ", "from ", "if ", "for ", "while ", "return ", "pass ", "="]
    if any(kw in comment_text for kw in code_keywords):
        return True
    return False


def process_file_treesitter(content: str, path: str, mode_c: bool, mode_d: bool, mode_a: bool) -> str:
    if not HAS_TREESITTER:
        raise RuntimeError(
            "tree-sitter and tree-sitter-python are required for 'ts' backend. Run: pip install tree-sitter tree-sitter-python"
        )
    parser = tree_sitter.Parser(tree_sitter.Language(ts_python.language()))
    tree = parser.parse(content.encode("utf-8"))
    root_node = tree.root_node
    source_bytes = content.encode("utf-8")
    edits = []

    def traverse(node, parent_is_body=False):
        if node.start_byte == 0 and node.type == "comment":
            text = source_bytes[node.start_byte : node.end_byte].decode("utf-8")
            if text.startswith("#!"):
                if not mode_a:
                    return
        if node.type == "comment":
            text = source_bytes[node.start_byte : node.end_byte].decode("utf-8")
            is_shebang = text.startswith("#!")
            line_start = source_bytes.rfind(b"\n", 0, node.start_byte) + 1
            preceding_text = source_bytes[line_start : node.start_byte].decode("utf-8")
            is_inline = len(preceding_text.strip()) > 0
            should_remove = False
            if mode_a:
                should_remove = not is_shebang
            elif mode_c:
                if not is_comment_preserved(text, is_shebang):
                    should_remove = True
            else:
                if is_inline and not is_comment_preserved(text, is_shebang):
                    should_remove = True
            if should_remove:
                start = node.start_byte
                if is_inline:
                    while start > line_start and source_bytes[start - 1] in (b" ", b"\t"):
                        start -= 1
                edits.append((start, node.end_byte, b""))
        elif node.type == "string":
            parent = node.parent
            if parent and parent.type in ("module", "class_definition", "function_definition"):
                body_node = parent.child_by_field_name("body")
                is_module_doc = parent.type == "module" and node == parent.children[0]
                is_body_doc = body_node and len(body_node.children) > 0 and body_node.children[0] == node
                if is_module_doc or is_body_doc:
                    if is_module_doc and not mode_a and not mode_d:
                        pass
                    elif mode_a or mode_d or (not is_module_doc):
                        if (
                            parent.type in ("class_definition", "function_definition")
                            and body_node
                            and len(body_node.children) == 1
                        ):
                            indent_match = re.search(
                                r"^[ \t]*",
                                source_bytes[node.start_byte - 20 : node.start_byte].decode("utf-8", errors="ignore"),
                            )
                            indent = "    "
                            edits.append((node.start_byte, node.end_byte, f"pass".encode("utf-8")))
                        else:
                            edits.append((node.start_byte, node.end_byte, b""))
        for child in node.children:
            traverse(child)

    traverse(root_node)
    edits.sort(key=lambda x: x[0], reverse=True)
    modified_bytes = source_bytes
    for start, end, replacement in edits:
        modified_bytes = modified_bytes[:start] + replacement + modified_bytes[end:]
    result_code = modified_bytes.decode("utf-8")
    result_code = re.sub(r"\n[ \t]*\n[ \t]*\n+", "\n\n", result_code)
    return result_code


class LibCSTRemover(cst.CSTTransformer):

    def __init__(self, mode_c: bool, mode_d: bool, mode_a: bool):
        super().__init__()
        self.mode_c = mode_c
        self.mode_d = mode_d
        self.mode_a = mode_a

    def leave_SimpleStatementLine(
        self, original_node: cst.SimpleStatementLine, updated_node: cst.SimpleStatementLine
    ) -> cst.SimpleStatementLine | cst.RemovalSentinel:
        new_whitespace = []
        for ws in updated_node.leading_lines:
            if ws.comment:
                text = ws.comment.value
                if self.mode_a:
                    continue
                if self.mode_c and not is_comment_preserved(text):
                    continue
            new_whitespace.append(ws)
        trailing = updated_node.trailing_whitespace
        if trailing and trailing.comment:
            text = trailing.comment.value
            is_inline = True
            if (
                self.mode_a
                or (self.mode_c and not is_comment_preserved(text))
                or (
                    not self.mode_c
                    and not self.mode_d
                    and not self.mode_a
                    and is_inline
                    and not is_comment_preserved(text)
                )
            ):
                updated_node = updated_node.with_changes(trailing_whitespace=trailing.with_changes(comment=None))
        return updated_node


def process_file_libcst(content: str, mode_c: bool, mode_d: bool, mode_a: bool) -> str:
    if not HAS_LIBCST:
        raise RuntimeError("libcst is required for '-b libcst'. Run: pip install libcst")
    module = cst.parse_module(content)
    transformer = LibCSTRemover(mode_c, mode_d, mode_a)
    modified_module = module.visit(transformer)
    return modified_module.code


def validate_python_code(code: str, filename: str) -> bool:
    try:
        compile(code, filename, "exec")
        return True
    except SyntaxError as e:
        print(f"Validation Error in {filename}: {e}", file=sys.stderr)
        return False


def atomic_write(filepath: Path, content: str):
    dir_name = filepath.parent
    dir_name.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(dir=dir_name, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(temp_path, filepath)
    except Exception as e:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
        raise e


def process_single_file(args_tuple):
    file_path, idx, total, mode_c, mode_d, mode_a, backend = args_tuple
    path = Path(file_path)
    try:
        original_content = path.read_text(encoding="utf-8")
        orig_bytes_len = len(original_content.encode("utf-8"))
        if backend == "libcst":
            new_content = process_file_libcst(original_content, mode_c, mode_d, mode_a)
        else:
            new_content = process_file_treesitter(original_content, str(path), mode_c, mode_d, mode_a)
        if not validate_python_code(new_content, str(path)):
            return file_path, 0, "Syntax validation failed, skipped."
        new_bytes_len = len(new_content.encode("utf-8"))
        reduced = orig_bytes_len - new_bytes_len
        atomic_write(path, new_content)
        return file_path, reduced, None
    except Exception as e:
        return file_path, 0, str(e)


def main():
    parser = argparse.ArgumentParser(description="Tree-sitter / LibCST Python Comment & Docstring Remover")
    parser.add_argument("paths", nargs="+", help="Python files or directories to process")
    parser.add_argument(
        "-c", action="store_true", help="Remove all comments (except shebang, noqa, commented-out code)"
    )
    parser.add_argument("-d", action="store_true", help="Remove all docstrings")
    parser.add_argument("-a", action="store_true", help="Remove all comments & docstrings including preserved ones")
    parser.add_argument("-b", choices=["ts", "libcst"], default="ts", help="Backend engine (default: ts)")
    cli_args = parser.parse_args()
    py_files = []
    for p in cli_args.paths:
        p_path = Path(p)
        if p_path.is_file() and p_path.suffix == ".py":
            py_files.append(p_path)
        elif p_path.is_dir():
            py_files.extend(p_path.rglob("*.py"))
    if not py_files:
        print("No Python files found.")
        sys.exit(0)
    total_files = len(py_files)
    tasks = [
        (str(f), idx, total_files, cli_args.c, cli_args.d, cli_args.a, cli_args.b) for idx, f in enumerate(py_files, 1)
    ]
    term_width = shutil.get_terminal_size((80, 20)).columns
    print(f"Processing {total_files} file(s) with 4 workers (backend: {cli_args.b})...\n")
    with concurrent.futures.ProcessPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(process_single_file, task): task[0] for task in tasks}
        for future in concurrent.futures.as_completed(futures):
            file_path, reduced, err = future.result()
            idx_str = f"[{tasks[[t[0] for t in tasks].index(file_path)][1]}/{total_files}]"
            filename_str = Path(file_path).name
            if err:
                print(f"{idx_str}  {filename_str:<25} Error: {err}")
            else:
                reduced_str = f"{reduced} bytes reduced"
                if reduced > 0:
                    colored_reduced = f"{ANSI_GREEN}{reduced_str}{ANSI_RESET}"
                else:
                    colored_reduced = f"{ANSI_DIM}{reduced_str}{ANSI_RESET}"
                line = f"{idx_str:<10} {filename_str:<30} {colored_reduced}"
                print(line[:term_width])


if __name__ == "__main__":
    main()
