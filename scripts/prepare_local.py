"""Create local-only Compose secrets, retaining existing database passwords."""

import os
import secrets
from pathlib import Path


def prepare(directory: Path):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    for name in ("db-password", "admin-password"):
        path = directory / name
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            if not path.is_file() or not path.read_text().strip():
                raise RuntimeError(f"Existing secret {name} is not a nonempty file.") from None
        else:
            with os.fdopen(fd, "w") as stream:
                stream.write(secrets.token_urlsafe(48) + "\n")
        # File-backed Compose secrets retain host permissions. The private
        # parent directory protects them on the host; containers can read their
        # read-only secret mounts without starting the app as root.
        path.chmod(0o444)


if __name__ == "__main__":
    prepare(Path(__file__).resolve().parents[1] / ".local" / "container-secrets")
    print("Local database secrets are ready. Start with: docker compose up --build --wait")
