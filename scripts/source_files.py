"""Select exactly the non-ignored source files intended for Git upload."""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def source_files(root=ROOT):
    result = subprocess.check_output(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=root,
    )
    files = sorted({Path(name.decode()) for name in result.split(b"\0") if name})
    for relative in files:
        path = root / relative
        if relative.is_absolute() or ".." in relative.parts or path.is_symlink() or not path.is_file():
            raise RuntimeError("Upload source must contain regular files within the project.")
        if not path.resolve().is_relative_to(root.resolve()):
            raise RuntimeError("Upload source points outside the project.")
        private_env = relative.name == ".env" or (relative.name.startswith(".env.") and relative.name != ".env.example")
        database_file = any(relative.name.endswith(suffix) for suffix in (".db", ".sqlite", ".sqlite3", ".db-wal", ".db-shm", ".sqlite-wal", ".sqlite-shm", ".sqlite3-wal", ".sqlite3-shm"))
        if any(part in {".git", ".local", ".venv", "node_modules", "__pycache__"} for part in relative.parts) or private_env or database_file or relative.suffix == ".pyc":
            raise RuntimeError(f"Private or generated file must be removed from upload: {relative}")
    if not files:
        raise RuntimeError("There are no source files to validate.")
    return files
