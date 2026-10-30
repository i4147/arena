#!/data/data/com.termux/files/usr/bin/env python
from __future__ import annotations

import ast
import logging
import operator
from pathlib import Path

from joblib import Parallel, delayed
from xxhash import xxh64

OUTPUT_DIR = Path("output")
OUTPUT_FILE = OUTPUT_DIR / "const.py"
LOG_FILE = OUTPUT_DIR / "error.log"
OUTPUT_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.ERROR,
    format="%(asctime)s - %(levelname)s - %(message)s",
)


def get_file_hash(path):
    hasher = xxh64()
    with Path(path).open("rb") as f:
        while chunk := f.read(CHUNK_SIZE):
            hasher.update(chunk)
    return hasher.hexdigest()


def extract_constants(path):
    constants = []
    try:
        with Path(path).open("r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                is_simple_assign = all(isinstance(t, ast.Name) for t in node.targets)
                if is_simple_assign and isinstance(node.value, ast.Constant):
                    for target in node.targets:
                        const_name = target.id
                        if const_name.isupper():
                            const_value = ast.unparse(node.value)
                            const_type = type(node.value.value).__name__
                            constants.append((const_name, const_value, const_type))
            elif isinstance(node, ast.AnnAssign):
                if isinstance(node.target, ast.Name) and node.value is not None:
                    if node.target.id.isupper():
                        const_name = node.target.id
                        const_value = ast.unparse(node.value)
                        const_type = (
                            type(node.value.value).__name__ if isinstance(node.value, ast.Constant) else "unknown"
                        )
                        constants.append((const_name, const_value, const_type))
    except SyntaxError as e:
        logging.error(f"Syntax error in {path}: {e}")
    except Exception as e:
        logging.error(f"Error processing {path}: {e}")
    return constants


def process_file(path):
    file_hash = get_file_hash(path)
    Path(path)
    constants = extract_constants(path)
    return file_hash, constants


def main():
    cwd = Path.cwd()
    python_files = list(get_pyfiles(cwd))
    if not python_files:
        print("No Python files found in the current directory.")
        return
    print(f"Found {len(python_files)} Python files. Processing...")
    results = Parallel(n_jobs=-1)(delayed(process_file)(f) for f in python_files)
    processed_hashes = set()
    all_constants_by_hash = {}
    for file_hash, constants in results:
        if constants is None:
            continue
        if file_hash not in processed_hashes:
            processed_hashes.add(file_hash)
            for name, value, ctype in constants:
                if file_hash not in all_constants_by_hash:
                    all_constants_by_hash[file_hash] = []
                found = False
                for idx, (existing_name, existing_value, _existing_type) in enumerate(all_constants_by_hash[file_hash]):
                    if existing_name == name and existing_value == value:
                        all_constants_by_hash[file_hash][idx] = name, value, ctype
                        found = True
                        break
                if not found:
                    all_constants_by_hash[file_hash].append((name, value, ctype))
    final_constants = []
    for file_hash, const_list in all_constants_by_hash.items():
        final_constants.extend(const_list)
    final_constants.sort(key=operator.itemgetter(0))
    with Path(OUTPUT_FILE).open("w", encoding="utf-8") as f:
        f.write("# Automatically generated constants file\n")
        f.write("# Based on files in the current directory\n\n")
        written_consts = set()
        for name, value, ctype in final_constants:
            constant_line = f"{name} = {value}"
            if constant_line not in written_consts:
                f.write(f"# Type: {ctype}\n")
                f.write(f"{constant_line}\n\n")
                written_consts.add(constant_line)
    print(f"Successfully extracted {len(written_consts)} unique constants to {OUTPUT_FILE}")
    if LOG_FILE.exists():
        print(f"Errors logged to {LOG_FILE}")


if __name__ == "__main__":
    raise SystemExit(main())
