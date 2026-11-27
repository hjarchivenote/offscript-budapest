"""Publish the exact validated image, then verify a registry pull by digest."""

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from app import __version__
from scripts.image_identity import archive_identity, matches_loaded

CHECKS = {
    "source-secret-scan", "image-build", "private-build-context",
    "high-critical-vulnerability-scan", "postgres-integration",
    "two-replica-concurrency", "browser-desktop-mobile",
}
KUBERNETES_CHECKS = {"same-image-archive", "two-runtime-image-ids", "service-ready", "integration-cases"}
KUBERNETES_MIN_CASES = 9


def validated_metadata(output, commit):
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Publication requires a full Git commit SHA.")
    metadata = json.loads((output / "validated-image.json").read_text())
    if (metadata.get("commit") != commit or metadata.get("version") != __version__
            or metadata.get("platform") != "linux/amd64"
            or metadata.get("image") != f"offscript:ci-{commit}"
            or not CHECKS <= set(metadata.get("checks", []))
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", metadata.get("image_id", ""))):
        raise ValueError("The image validation metadata does not match this release.")
    with (output / "image.tar").open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if (metadata.get("archive_sha256") != digest
            or (output / "image.tar.sha256").read_text() != f"{digest}  image.tar\n"):
        raise ValueError("The validated image archive checksum does not match.")
    canonical_id, _ = archive_identity(output / "image.tar", metadata["image"])
    if canonical_id != metadata["image_id"]:
        raise ValueError("The archive image configuration does not match the validated image ID.")
    return metadata


def validated_kubernetes(output, metadata):
    try:
        proof = json.loads((output / "kubernetes-image.json").read_text())
    except (OSError, ValueError):
        raise ValueError("A successful exact-image Kubernetes receipt is required before publication.") from None
    verify_kubernetes_receipt(proof, metadata, os.environ.get("GITHUB_RUN_ID"), int(os.environ.get("GITHUB_RUN_ATTEMPT", "0")))
    return proof


def verify_kubernetes_receipt(proof, metadata, run_id, attempt):
    if (not isinstance(proof, dict) or proof.get("schema") != 1
            or any(proof.get(key) != metadata.get(key) for key in ("image", "commit", "version", "image_id", "archive_sha256"))
            or proof.get("run_id") != str(run_id) or proof.get("run_attempt") != attempt
            or proof.get("context") != "kind-offscript-ci" or proof.get("ready_replicas") != 2
            or proof.get("tested_cases", 0) < KUBERNETES_MIN_CASES or proof.get("failures") != 0 or proof.get("skipped") != 0
            or not KUBERNETES_CHECKS <= set(proof.get("checks", []))
            or len(proof.get("pod_image_ids", [])) != 2
            or any(value != metadata["image_id"] for value in proof.get("pod_image_ids", []))):
        raise ValueError("Kubernetes verification does not match this image, archive or CI attempt.")


def publish(output, commit, repository, actor, token):
    receipt = output / "published-image.json"
    receipt.unlink(missing_ok=True)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Invalid repository name.")
    if not actor or not token:
        raise ValueError("Registry authentication is missing.")
    metadata = validated_metadata(output, commit)
    _, archived_config = archive_identity(output / "image.tar", metadata["image"])
    kubernetes = validated_kubernetes(output, metadata)
    registry_image = "ghcr.io/" + repository.lower()
    tag = f"{registry_image}:sha-{commit}"
    source = f"https://github.com/{repository}"
    with TemporaryDirectory(prefix="offscript-registry-") as config:
        env = {k: v for k, v in os.environ.items() if k not in {"GHCR_TOKEN", "GITHUB_TOKEN", "DOCKER_AUTH_CONFIG"}}
        env["DOCKER_CONFIG"] = config

        def docker(*args, **kwargs):
            return subprocess.run(["docker", *args], env=env, check=True, text=True, capture_output=True, **kwargs).stdout

        docker("image", "load", "--input", str(output / "image.tar"))
        image = json.loads(docker("image", "inspect", metadata["image"]))[0]
        labels = image.get("Config", {}).get("Labels") or {}
        if (not matches_loaded(image, archived_config, metadata["image_id"]) or image.get("Os") != "linux" or image.get("Architecture") != "amd64"
                or labels.get("org.opencontainers.image.revision") != commit
                or labels.get("org.opencontainers.image.version") != __version__
                or labels.get("org.opencontainers.image.source") != source):
            raise ValueError("The loaded image is not the validated source revision.")
        docker("login", "ghcr.io", "--username", actor, "--password-stdin", input=token)
        docker("image", "tag", metadata["image"], tag)
        # The classic store is already single-platform. Containerd stores may
        # expose an index, so select the tested manifest for the ARM/emulation host.
        platform = ("--platform", "linux/amd64") if image["Id"] != metadata["image_id"] else ()
        pushed = docker("image", "push", *platform, tag)
        digests = set(re.findall(r"digest: (sha256:[0-9a-f]{64})", pushed))
        if len(digests) != 1:
            raise RuntimeError("Registry publication did not return one image digest.")
        reference = f"{registry_image}@{digests.pop()}"
        # Remove only this job's app tags so the next check retrieves from GHCR.
        docker("image", "rm", metadata["image"], tag)
        docker("image", "pull", "--platform", "linux/amd64", reference)
        pulled = json.loads(docker("image", "inspect", reference))[0]
        if not matches_loaded(pulled, archived_config, metadata["image_id"]):
            raise RuntimeError("The published image differs from the tested image.")
        docker("run", "--rm", "--network", "none", "--read-only", "--cap-drop", "ALL",
               "--security-opt", "no-new-privileges:true", "--entrypoint", "python", reference,
               "-c", "import os,sys; from app import __version__; assert os.getuid()!=0; "
               "assert __version__==sys.argv[1]; assert os.environ['APP_COMMIT']==sys.argv[2]",
               __version__, commit)
    result = {
        **metadata, "kubernetes": kubernetes, "registry_tag": tag, "registry_reference": reference,
        "repository": repository, "run_url": f"https://github.com/{repository}/actions/runs/{os.environ['GITHUB_RUN_ID']}",
        "pull_verified": True,
    }
    receipt.write_text(json.dumps(result, indent=2) + "\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a") as stream:
            stream.write(f"### Published and pull-verified image\n\nVersion: `{__version__}` · Commit: `{commit}`\n\n"
                         f"Tag: `{tag}`\n\nDeploy by digest: `{reference}`\n\n"
                         f"Validated image ID: `{metadata['image_id']}`\n")
    print(f"Published and verified: {reference}", flush=True)
    return result


def main():
    if os.environ.get("GITHUB_EVENT_NAME") != "push" or os.environ.get("GITHUB_REF") != "refs/heads/main":
        raise RuntimeError("Only a successful main-branch push workflow may publish.")
    publish(Path(".local/ci"), os.environ["GITHUB_SHA"], os.environ["GITHUB_REPOSITORY"],
            os.environ["GITHUB_ACTOR"], os.environ["GHCR_TOKEN"])


if __name__ == "__main__":
    main()
