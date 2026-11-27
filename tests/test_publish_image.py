import hashlib
import io
import json
import subprocess
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import publish_image as release

COMMIT = "a" * 40
DIGEST = "sha256:" + "2" * 64
REPOSITORY = "hjarchivenote/offscript-budapest"
CONFIG = {"os": "linux", "architecture": "amd64", "rootfs": {"diff_ids": ["sha256:" + "3" * 64]},
          "config": {"Labels": {"org.opencontainers.image.revision": COMMIT,
                                "org.opencontainers.image.version": release.__version__,
                                "org.opencontainers.image.source": f"https://github.com/{REPOSITORY}"}}}
CONFIG_BYTES = json.dumps(CONFIG).encode()
IMAGE_ID = "sha256:" + hashlib.sha256(CONFIG_BYTES).hexdigest()


def write_image_archive(path):
    with tarfile.open(path, "w") as archive:
        for name, payload in (("config.json", CONFIG_BYTES), ("manifest.json", json.dumps([
            {"Config": "config.json", "RepoTags": [f"offscript:ci-{COMMIT}"], "Layers": []},
        ]).encode())):
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))


@pytest.fixture
def artifact(tmp_path, monkeypatch):
    archive = tmp_path / "image.tar"
    write_image_archive(archive)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    metadata = {
        "image": f"offscript:ci-{COMMIT}", "image_id": IMAGE_ID,
        "archive_sha256": digest, "version": release.__version__, "commit": COMMIT,
        "platform": "linux/amd64", "checks": sorted(release.CHECKS),
    }
    (tmp_path / "validated-image.json").write_text(json.dumps(metadata))
    (tmp_path / "image.tar.sha256").write_text(f"{digest}  image.tar\n")
    kubernetes = {key: metadata[key] for key in ("image", "commit", "version", "image_id", "archive_sha256")}
    kubernetes.update(schema=1, run_id="12345", run_attempt=1, context="kind-offscript-ci", ready_replicas=2,
                      tested_cases=9, failures=0, skipped=0, pod_image_ids=[IMAGE_ID, IMAGE_ID], checks=sorted(release.KUBERNETES_CHECKS))
    (tmp_path / "kubernetes-image.json").write_text(json.dumps(kubernetes))
    # An interrupted rerun must not advertise an old successful publication.
    (tmp_path / "published-image.json").write_text('{"pull_verified": true}')
    monkeypatch.setenv("GITHUB_RUN_ID", "12345")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.setenv("GHCR_TOKEN", "private-test-token")
    monkeypatch.setenv("GITHUB_TOKEN", "another-private-test-token")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    return tmp_path


def docker_runner(monkeypatch, *, loaded_change=None, pulled_id=IMAGE_ID, push_digest=True, login_fails=False):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        image = {
            "Id": IMAGE_ID, "Os": "linux", "Architecture": "amd64",
            "RootFS": {"Layers": list(CONFIG["rootfs"]["diff_ids"])},
            "Config": {"Labels": {
                "org.opencontainers.image.revision": COMMIT,
                "org.opencontainers.image.version": release.__version__,
                "org.opencontainers.image.source": f"https://github.com/{REPOSITORY}",
            }},
        }
        stdout = ""
        if command[1:3] == ["image", "inspect"]:
            if command[-1].startswith("offscript:"):
                if loaded_change == "source":
                    image["Config"]["Labels"]["org.opencontainers.image.source"] = "https://github.com/other/repo"
                elif loaded_change == "store-descriptor":
                    image["Id"] = "sha256:" + "f" * 64
                elif loaded_change == "RootFS":
                    image["RootFS"]["Layers"] = ["sha256:" + "0" * 64]
                    image["Id"] = "sha256:" + "0" * 64
                elif loaded_change:
                    image[loaded_change] = "wrong"
            else:
                image["Id"] = pulled_id
                if pulled_id != IMAGE_ID:
                    image["RootFS"]["Layers"] = [pulled_id]
            stdout = json.dumps([image])
        elif command[1] == "login" and login_fails:
            raise subprocess.CalledProcessError(1, command)
        elif command[1:3] == ["image", "push"] and push_digest:
            stdout = f"sha-{COMMIT}: digest: {DIGEST} size: 1234\n"
        return SimpleNamespace(stdout=stdout)

    monkeypatch.setattr(release.subprocess, "run", run)
    return calls


def test_publishes_exact_archive_and_checks_pulled_image_without_rebuilding(artifact, monkeypatch, capsys):
    calls = docker_runner(monkeypatch)
    result = release.publish(artifact, COMMIT, REPOSITORY, "publisher", "private-test-token")
    commands = [command for command, _ in calls]
    reference = f"ghcr.io/{REPOSITORY}@{DIGEST}"
    assert not any("build" in command for command in commands)
    assert [command[1:3] for command in commands[:2]] == [["image", "load"], ["image", "inspect"]]
    assert commands[2][1] == "login"
    assert [command[1:3] for command in commands[3:8]] == [
        ["image", "tag"], ["image", "push"], ["image", "rm"], ["image", "pull"], ["image", "inspect"],
    ]
    assert reference in commands[6] and reference in commands[-1]
    assert "--platform" not in commands[4]  # Classic store already has one platform.
    assert commands[-1][1] == "run" and "--read-only" in commands[-1]
    for command, kwargs in calls:
        assert "private-test-token" not in " ".join(command)
        assert "GHCR_TOKEN" not in kwargs["env"] and "GITHUB_TOKEN" not in kwargs["env"]
        assert kwargs.get("input") == ("private-test-token" if command[1] == "login" else None)
        assert not Path(kwargs["env"]["DOCKER_CONFIG"]).exists()
    assert result["image_id"] == IMAGE_ID and result["pull_verified"] is True
    assert result["registry_reference"] == reference
    assert json.loads((artifact / "published-image.json").read_text()) == result
    assert reference in (artifact / "summary.md").read_text()
    assert "private-test-token" not in capsys.readouterr().out


@pytest.mark.parametrize("fault", ["archive", "commit", "missing-scan", "checksum-file", "image-id"])
def test_invalid_proof_blocks_authentication_and_publication(artifact, monkeypatch, fault):
    calls = docker_runner(monkeypatch)
    path = artifact / "validated-image.json"
    metadata = json.loads(path.read_text())
    if fault == "archive":
        (artifact / "image.tar").write_bytes(b"modified after validation")
    elif fault == "checksum-file":
        (artifact / "image.tar.sha256").write_text("old checksum\n")
    elif fault == "missing-scan":
        metadata["checks"].remove("high-critical-vulnerability-scan")
    elif fault == "commit":
        metadata["commit"] = "b" * 40
    else:
        metadata["image_id"] = "not-an-image-id"
    path.write_text(json.dumps(metadata))
    with pytest.raises(ValueError):
        release.publish(artifact, COMMIT, REPOSITORY, "publisher", "private-test-token")
    assert not calls
    assert not (artifact / "published-image.json").exists()


@pytest.mark.parametrize("fault", ["RootFS", "source", "Architecture"])
def test_substituted_image_blocks_registry_login(artifact, monkeypatch, fault):
    calls = docker_runner(monkeypatch, loaded_change=fault)
    with pytest.raises(ValueError, match="loaded image"):
        release.publish(artifact, COMMIT, REPOSITORY, "publisher", "private-test-token")
    assert len(calls) == 2
    assert not (artifact / "published-image.json").exists()


def test_different_registry_image_cannot_produce_a_success_receipt(artifact, monkeypatch):
    calls = docker_runner(monkeypatch, pulled_id="sha256:" + "0" * 64)
    with pytest.raises(RuntimeError, match="differs"):
        release.publish(artifact, COMMIT, REPOSITORY, "publisher", "private-test-token")
    assert any(command[1:3] == ["image", "pull"] for command, _ in calls)
    assert not (artifact / "published-image.json").exists()


def test_docker_store_descriptor_can_differ_while_canonical_runtime_image_matches(artifact, monkeypatch):
    calls = docker_runner(monkeypatch, loaded_change="store-descriptor")
    result = release.publish(artifact, COMMIT, REPOSITORY, "publisher", "synthetic-token")
    assert result["image_id"] == IMAGE_ID
    assert "--platform" in next(command for command, _ in calls if command[1:3] == ["image", "push"])


@pytest.mark.parametrize(("event", "ref"), [("push", "refs/heads/development"), ("pull_request", "refs/heads/main"), ("workflow_dispatch", "refs/heads/main")])
def test_only_main_push_may_publish(monkeypatch, event, ref):
    monkeypatch.setenv("GITHUB_EVENT_NAME", event)
    monkeypatch.setenv("GITHUB_REF", ref)
    monkeypatch.setattr(release, "publish", lambda *args: pytest.fail("Publication must be blocked"))
    with pytest.raises(RuntimeError, match="main-branch push"):
        release.main()


def test_missing_registry_digest_never_claims_success(artifact, monkeypatch):
    docker_runner(monkeypatch, push_digest=False)
    with pytest.raises(RuntimeError, match="digest"):
        release.publish(artifact, COMMIT, REPOSITORY, "publisher", "private-test-token")
    assert not (artifact / "published-image.json").exists()


def test_registry_error_removes_credentials_and_stale_receipt(artifact, monkeypatch):
    calls = docker_runner(monkeypatch, login_fails=True)
    with pytest.raises(subprocess.CalledProcessError):
        release.publish(artifact, COMMIT, REPOSITORY, "publisher", "private-test-token")
    assert not (artifact / "published-image.json").exists()
    assert not Path(calls[-1][1]["env"]["DOCKER_CONFIG"]).exists()


@pytest.mark.parametrize("fault", ["missing", "different-image", "different-archive", "different-attempt", "old-commit", "skipped", "failed", "wrong-runtime-image"])
def test_missing_or_mismatched_kubernetes_proof_blocks_publication_before_login(artifact, monkeypatch, fault):
    path = artifact / "kubernetes-image.json"
    proof = json.loads(path.read_text())
    if fault == "missing":
        path.unlink()
    else:
        if fault == "different-image":
            proof["image_id"] = "sha256:" + "0" * 64
        elif fault == "different-archive":
            proof["archive_sha256"] = "0" * 64
        elif fault == "different-attempt":
            proof["run_attempt"] = 2
        elif fault == "old-commit":
            proof["commit"] = "b" * 40
        elif fault == "skipped":
            proof["skipped"] = 1
        elif fault == "failed":
            proof["failures"] = 1
        else:
            proof["pod_image_ids"][1] = "sha256:" + "0" * 64
        path.write_text(json.dumps(proof))
    calls = docker_runner(monkeypatch)
    with pytest.raises(ValueError, match="Kubernetes"):
        release.publish(artifact, COMMIT, REPOSITORY, "publisher", "synthetic-token")
    assert not calls
    assert not (artifact / "published-image.json").exists()
