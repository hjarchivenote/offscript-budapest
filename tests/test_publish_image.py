import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import publish_image as release

COMMIT = "a" * 40
IMAGE_ID = "sha256:" + "1" * 64
DIGEST = "sha256:" + "2" * 64
REPOSITORY = "hjarchivenote/offscript-budapest"


@pytest.fixture
def artifact(tmp_path, monkeypatch):
    archive = tmp_path / "image.tar"
    archive.write_bytes(b"synthetic validated image archive")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    metadata = {
        "image": f"offscript:ci-{COMMIT}", "image_id": IMAGE_ID,
        "archive_sha256": digest, "version": release.__version__, "commit": COMMIT,
        "platform": "linux/amd64", "checks": sorted(release.CHECKS),
    }
    (tmp_path / "validated-image.json").write_text(json.dumps(metadata))
    (tmp_path / "image.tar.sha256").write_text(f"{digest}  image.tar\n")
    # An interrupted rerun must not advertise an old successful publication.
    (tmp_path / "published-image.json").write_text('{"pull_verified": true}')
    monkeypatch.setenv("GITHUB_RUN_ID", "12345")
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
                elif loaded_change:
                    image[loaded_change] = "wrong"
            else:
                image["Id"] = pulled_id
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


@pytest.mark.parametrize("fault", ["Id", "source", "Architecture"])
def test_substituted_image_blocks_registry_login(artifact, monkeypatch, fault):
    calls = docker_runner(monkeypatch, loaded_change=fault)
    with pytest.raises(ValueError, match="loaded image"):
        release.publish(artifact, COMMIT, REPOSITORY, "publisher", "private-test-token")
    assert len(calls) == 2
    assert not (artifact / "published-image.json").exists()


def test_different_registry_image_cannot_produce_a_success_receipt(artifact, monkeypatch):
    calls = docker_runner(monkeypatch, pulled_id="sha256:" + "3" * 64)
    with pytest.raises(RuntimeError, match="differs"):
        release.publish(artifact, COMMIT, REPOSITORY, "publisher", "private-test-token")
    assert any(command[1:3] == ["image", "pull"] for command, _ in calls)
    assert not (artifact / "published-image.json").exists()


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
