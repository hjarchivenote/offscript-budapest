"""Canonical image identity across Docker classic/containerd stores and Kubernetes CRI."""

import hashlib
import json
import tarfile


def archive_identity(path, tag):
    with tarfile.open(path) as archive:
        manifest = json.load(archive.extractfile("manifest.json"))
        selected = [entry for entry in manifest if tag in entry.get("RepoTags", [])]
        if len(selected) != 1:
            raise ValueError("The image archive must contain exactly the expected source image.")
        member = archive.getmember(selected[0]["Config"])
        if not member.isfile() or member.size > 1024 * 1024:
            raise ValueError("The image configuration is invalid.")
        payload = archive.extractfile(member).read()
    config = json.loads(payload)
    if config.get("os") != "linux" or config.get("architecture") != "amd64":
        raise ValueError("The image archive must contain the validated linux/amd64 platform.")
    return "sha256:" + hashlib.sha256(payload).hexdigest(), config


def matches_loaded(image, config, canonical_id=None):
    # Docker's Id may be an OCI index/manifest digest, while CRI uses the config
    # digest. Match the archived runtime configuration and uncompressed layers.
    expected = config.get("config", {})
    actual = image.get("Config") or {}
    if canonical_id is not None and image.get("Id") == canonical_id:
        # The classic store already exposes the exact config digest. Its API
        # may omit legacy Config fields that are still present in the archive.
        return True
    return (
        image.get("Os") == config.get("os") and image.get("Architecture") == config.get("architecture")
        and image.get("RootFS", {}).get("Layers") == config.get("rootfs", {}).get("diff_ids")
        and all(actual.get(key) == value for key, value in expected.items())
        and all(expected.get(key) == value for key, value in actual.items() if value is not None)
    )
