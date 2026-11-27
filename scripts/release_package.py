"""Bind a published image and deployment data to one successful main CI attempt."""

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import zipfile
from pathlib import Path

from scripts.check_kubernetes import FILES
from scripts.kubernetes_local import ROOT
from scripts.publish_image import CHECKS

REPOSITORY = "hjarchivenote/offscript-budapest"
REPOSITORY_ID = 1471994197
WORKFLOW_ID = 430530307
DATA_FILES = (*("k8s/" + name for name in FILES), "docker/init-app-db.sql", "published-image.json")
ARCHIVE_FILES = (*DATA_FILES, "delivery.json")
MAX_PACKAGE_BYTES = 1024 * 1024


def receipt_matches(receipt, commit, run_id):
    if (not re.fullmatch(r"[a-f0-9]{40}", commit)
            or receipt.get("commit") != commit or receipt.get("repository") != REPOSITORY
            or receipt.get("run_url") != f"https://github.com/{REPOSITORY}/actions/runs/{run_id}"
            or receipt.get("pull_verified") is not True
            or not isinstance(receipt.get("version"), str) or not receipt["version"]
            or receipt.get("platform") != "linux/amd64"
            or not CHECKS <= set(receipt.get("checks", []))
            or not re.fullmatch(r"sha256:[a-f0-9]{64}", receipt.get("image_id", ""))
            or not re.fullmatch(r"ghcr\.io/hjarchivenote/offscript-budapest@sha256:[a-f0-9]{64}", receipt.get("registry_reference", ""))):
        raise ValueError("Publication receipt does not match the approved release.")


def prepare(output, commit, run_id, attempt, *, root=ROOT):
    receipt_bytes = (output / "published-image.json").read_bytes()
    receipt = json.loads(receipt_bytes)
    receipt_matches(receipt, commit, run_id)
    target = output / "delivery"
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    hashes = {}
    for name in DATA_FILES:
        data = receipt_bytes if name == "published-image.json" else (root / name).read_bytes()
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        hashes[name] = hashlib.sha256(data).hexdigest()
    descriptor = {"schema": 1, "repository": REPOSITORY, "commit": commit,
                  "run_id": run_id, "run_attempt": attempt, "files": hashes}
    (target / "delivery.json").write_text(json.dumps(descriptor, indent=2) + "\n")
    print("Prepared deployment data for the local delivery worker.", flush=True)
    return target


def unpack(data, artifact, run, destination):
    """Allow only known data files; never extract or execute repository scripts."""
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    if digest != artifact.get("digest") or len(data) > MAX_PACKAGE_BYTES:
        raise ValueError("Deployment artifact checksum or size is invalid.")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        allowed_dirs = {"k8s/", "docker/"}
        names = [entry.filename for entry in entries if not entry.is_dir()]
        if (set(names) != set(ARCHIVE_FILES) or len(names) != len(ARCHIVE_FILES)
                or sum(entry.file_size for entry in entries) > MAX_PACKAGE_BYTES
                or any(entry.is_dir() and entry.filename not in allowed_dirs for entry in entries)
                or any((entry.external_attr >> 16) & 0o170000 == 0o120000 for entry in entries)):
            raise ValueError("Deployment artifact must contain only the expected deployment data.")
        contents = {name: archive.read(name) for name in ARCHIVE_FILES}
    descriptor = json.loads(contents["delivery.json"])
    commit, run_id, attempt = run["head_sha"], run["id"], run["run_attempt"]
    if (descriptor.get("schema") != 1 or descriptor.get("repository") != REPOSITORY
            or descriptor.get("commit") != commit or descriptor.get("run_id") != run_id
            or descriptor.get("run_attempt") != attempt
            or descriptor.get("files") != {name: hashlib.sha256(contents[name]).hexdigest() for name in DATA_FILES}):
        raise ValueError("Deployment data does not match this CI run and attempt.")
    receipt = json.loads(contents["published-image.json"])
    receipt_matches(receipt, commit, run_id)
    # Fixed allowlisted paths and no extractall prevent traversal and script files.
    for name, payload in contents.items():
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / ".local/ci")
    args = parser.parse_args()
    if os.environ.get("GITHUB_EVENT_NAME") != "push" or os.environ.get("GITHUB_REF") != "refs/heads/main":
        raise RuntimeError("Only the validated main-branch publication may request delivery.")
    prepare(args.output, os.environ["GITHUB_SHA"], int(os.environ["GITHUB_RUN_ID"]), int(os.environ["GITHUB_RUN_ATTEMPT"]))


if __name__ == "__main__":
    main()
