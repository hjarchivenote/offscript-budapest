"""Check real image exclusion rules using temporary, non-secret sentinel files."""

import secrets
from contextlib import contextmanager
from pathlib import Path

DATABASE_SUFFIXES = (".db", ".db-shm", ".db-wal", ".sqlite", ".sqlite-shm", ".sqlite-wal", ".sqlite3", ".sqlite3-shm", ".sqlite3-wal")
PRIVATE_DIRECTORIES = (".local", ".venv", ".git", "node_modules", ".pytest_cache", ".ruff_cache")


@contextmanager
def private_context_probes(root: Path):
    root = root.resolve()
    name = "__offscript_ci_probe_" + secrets.token_hex(12)
    files, directories = [], []
    probes = []
    try:
        for base in (root / "app", root / "app/data"):
            candidates = [base / (name + suffix) for suffix in DATABASE_SUFFIXES]
            candidates.append(base / (".env." + name))
            candidates.extend(base / folder / name / "sentinel.txt" for folder in PRIVATE_DIRECTORIES)
            for path in candidates:
                missing, parent = [], path.parent
                while parent != root:
                    if parent.is_symlink():
                        raise RuntimeError("Build-context probes require real directories inside the project.")
                    if not parent.exists():
                        missing.append(parent)
                    parent = parent.parent
                for directory in reversed(missing):
                    directory.mkdir()
                    directories.append(directory)
                # Exclusive creation never overwrites an existing developer file.
                with path.open("x", encoding="utf-8") as stream:
                    stream.write("OFFSCRIPT_BUILD_CONTEXT_SENTINEL: synthetic test data only\n")
                files.append(path)
                probes.append("/app/" + path.relative_to(root).as_posix())
        yield probes
    finally:
        for path in reversed(files):
            path.unlink(missing_ok=True)
        for directory in reversed(directories):
            try:
                directory.rmdir()
            except OSError:
                # Retain a directory if another process added its own files.
                pass
