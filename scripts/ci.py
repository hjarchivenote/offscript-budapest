"""Run the same fail-fast checks locally and in GitHub Actions."""

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

from app import __version__
from scripts.build_context import private_context_probes
from scripts.prepare_local import prepare
from scripts.scan_image import scan_image
from scripts.scan_source import scan_source

ROOT = Path(__file__).resolve().parents[1]


def run(*args, env=None):
    print("+ " + shlex.join(map(str, args)), flush=True)
    return subprocess.run(list(map(str, args)), cwd=ROOT, env=env, check=True)


def quality(output):
    output.mkdir(parents=True, exist_ok=True)
    (output / "python-tests.xml").unlink(missing_ok=True)
    run(sys.executable, "-m", "ruff", "check", "app", "tests", "scripts", "run.py")
    run(sys.executable, "-m", "pytest", "-q", f"--junitxml={output / 'python-tests.xml'}")
    run("npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund")
    run("node", "--check", "app/static/app.js")
    run("npm", "test")


def container(image, commit, output):
    output.mkdir(parents=True, exist_ok=True)
    # A failed rerun must not leave an old success manifest available to publish.
    for name in ("validated-image.json", "image.tar.sha256"):
        (output / name).unlink(missing_ok=True)
    for name in ("security.json", "source-secrets.json", "browser-desktop.png", "browser-mobile.png", "browser-failure.png", "browser-trace.zip"):
        (output / "reports" / name).unlink(missing_ok=True)
    scan_source(output)
    prepare(ROOT / ".local" / "container-secrets")
    run("docker", "compose", "config", "--quiet")
    run("docker", "build", "--check", ".")
    with private_context_probes(ROOT) as probes:
        run("docker", "build", "--platform", "linux/amd64", "--build-arg", f"APP_COMMIT={commit}",
            "--label", f"org.opencontainers.image.revision={commit}",
            "--label", f"org.opencontainers.image.version={__version__}", "--tag", image, ".")
    image_id = subprocess.check_output(["docker", "image", "inspect", "--format", "{{.Id}}", image], text=True).strip()
    archive = scan_image(image, output)
    run("npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund")
    environment = {
        **os.environ, "OFFSCRIPT_IMAGE": image, "CI_EXPECTED_COMMIT": commit,
        "CI_BROWSER_TEST": "1", "CI_OUTPUT_DIR": str(output),
        "CI_CONTEXT_PROBES": json.dumps(probes),
        "PLAYWRIGHT_BROWSERS_PATH": str(ROOT / ".local" / "ci-browsers"),
    }
    run("npx", "--no-install", "playwright", "install", "--with-deps", "chromium", env=environment)
    run(sys.executable, "-u", "-m", "scripts.check_container", env=environment)
    final_id = subprocess.check_output(["docker", "image", "inspect", "--format", "{{.Id}}", image], text=True).strip()
    if final_id != image_id:
        raise RuntimeError("The image tag changed during validation.")
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    (output / "image.tar.sha256").write_text(f"{digest}  image.tar\n")
    (output / "validated-image.json").write_text(json.dumps({
        "image": image, "image_id": image_id, "archive_sha256": digest,
        "version": __version__, "commit": commit, "platform": "linux/amd64",
        "checks": ["source-secret-scan", "image-build", "private-build-context", "high-critical-vulnerability-scan", "postgres-integration", "two-replica-concurrency", "browser-desktop-mobile"],
    }, indent=2) + "\n")
    print("Container validation passed; the checked image is ready for a later release step.", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("quality", "container", "all"))
    parser.add_argument("--image", default=os.environ.get("OFFSCRIPT_IMAGE", "offscript:ci-local"))
    parser.add_argument("--commit", default=os.environ.get("CI_COMMIT_SHA", "local"))
    parser.add_argument("--output", type=Path, default=ROOT / ".local" / "ci")
    args = parser.parse_args()
    output = args.output.resolve()
    if args.stage in {"container", "all"}:
        for name in ("validated-image.json", "image.tar.sha256"):
            (output / name).unlink(missing_ok=True)
    if args.stage in {"quality", "all"}:
        quality(output)
    if args.stage in {"container", "all"}:
        container(args.image, args.commit, output)


if __name__ == "__main__":
    main()
