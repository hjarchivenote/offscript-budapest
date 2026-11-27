import hashlib
import io
import json
import xml.etree.ElementTree as ET
from types import SimpleNamespace

import pytest
import yaml

from app import __version__
from scripts import kubernetes_image as candidate
from scripts.image_identity import matches_loaded
from scripts.publish_image import CHECKS, validated_kubernetes
from tests.test_publish_image import COMMIT, IMAGE_ID, write_image_archive


@pytest.fixture
def image_artifact(tmp_path, monkeypatch):
    import hashlib

    archive = tmp_path / "image.tar"
    write_image_archive(archive)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    metadata = {"image": f"offscript:ci-{COMMIT}", "commit": COMMIT, "version": __version__,
                "platform": "linux/amd64", "image_id": IMAGE_ID,
                "archive_sha256": digest, "checks": sorted(CHECKS)}
    (tmp_path / "validated-image.json").write_text(json.dumps(metadata))
    (tmp_path / "image.tar.sha256").write_text(f"{digest}  image.tar\n")
    monkeypatch.setenv("GITHUB_RUN_ID", "12345")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    return tmp_path, metadata


def test_ci_config_preserves_source_but_separates_local_ports(tmp_path):
    original = yaml.safe_load((candidate.ROOT / "k8s/kind.yaml").read_text())
    path = candidate.kind_config(tmp_path)
    generated = yaml.safe_load(path.read_text())
    assert original["nodes"][0]["extraPortMappings"][0]["hostPort"] == 8768
    assert generated["nodes"][0]["extraPortMappings"][0]["hostPort"] == 8769
    generated["nodes"][0]["extraPortMappings"][0]["hostPort"] = 8768
    assert generated == original


def test_unsafe_kind_mapping_is_rejected(tmp_path, monkeypatch):
    root = tmp_path / "source"
    (root / "k8s").mkdir(parents=True)
    config = yaml.safe_load((candidate.ROOT / "k8s/kind.yaml").read_text())
    config["nodes"][0]["extraPortMappings"][0]["listenAddress"] = "0.0.0.0"
    (root / "k8s/kind.yaml").write_text(yaml.safe_dump(config))
    monkeypatch.setattr(candidate, "ROOT", root)
    with pytest.raises(RuntimeError, match="local-only"):
        candidate.kind_config(tmp_path / "output")
    assert not (tmp_path / "output").exists()


def test_candidate_loader_refuses_application_cluster_before_docker(image_artifact, monkeypatch):
    output, _ = image_artifact
    monkeypatch.setattr(candidate, "command", lambda *args: pytest.fail("Must not touch Docker or load an image"))
    with pytest.raises(RuntimeError, match="isolated"):
        candidate.load(SimpleNamespace(context="kind-offscript"), output, COMMIT, "kind")


def test_archive_corruption_stops_before_cluster_or_docker(image_artifact, monkeypatch):
    output, _ = image_artifact
    (output / "image.tar").write_bytes(b"tampered")
    monkeypatch.setattr(candidate, "command", lambda *args: pytest.fail("Must not load a corrupted image"))
    with pytest.raises(ValueError, match="checksum"):
        candidate.load(SimpleNamespace(context=candidate.CONTEXT), output, COMMIT, "kind")


def test_wrong_node_name_cannot_bypass_ci_context_guard():
    class Cluster:
        context = candidate.CONTEXT

        def run(self, *args):
            if args[0] == "config":
                return "https://127.0.0.1:6443"
            return json.dumps({"items": [{"metadata": {"name": "offscript-control-plane"}}]})

    with pytest.raises(RuntimeError, match="application cluster"):
        candidate.require_ci_cluster(Cluster())


def test_loaded_image_substitution_is_rejected(image_artifact, monkeypatch):
    _, metadata = image_artifact
    image = {"Id": "sha256:" + "0" * 64, "Os": "linux", "Architecture": "amd64",
             "Config": {"Labels": {}}}
    monkeypatch.setattr(candidate, "command", lambda *args: json.dumps([image]))
    monkeypatch.setattr(candidate, "verify_image", lambda *args: pytest.fail("Do not execute a substituted image"))
    with pytest.raises(RuntimeError, match="not the exact image"):
        candidate.inspect_loaded(metadata, image_artifact[0])


def test_setup_uses_local_only_image_and_separate_credentials(image_artifact, monkeypatch):
    output, metadata = image_artifact
    monkeypatch.setattr(candidate, "require_ci_cluster", lambda *args: None)
    monkeypatch.setattr(candidate, "inspect_loaded", lambda *args: {"version": __version__})
    monkeypatch.setattr(candidate, "runtime_image_id", lambda *args: IMAGE_ID)
    calls = []

    def deploy(kube, image, commit, verified, **kwargs):
        calls.append((image, commit, kwargs))
        return {"ready_replicas": 2}

    monkeypatch.setattr(candidate, "_deploy_verified", deploy)
    assert candidate.setup(SimpleNamespace(), output, COMMIT)["ready_replicas"] == 2
    image, commit, options = calls[0]
    assert image == metadata["image"] and commit == COMMIT
    assert options["image_pull_policy"] == "Never"
    assert options["secret_directory"] == output / "kubernetes-secrets"
    assert options["proof_path"] == output / "kubernetes-runtime.json"


def junit(output, metadata, *, failures=0, skipped=0, wrong_identity=False):
    root = ET.Element("testsuites")
    suite = ET.SubElement(root, "testsuite", tests="9", failures=str(failures), errors="0", skipped=str(skipped))
    properties = ET.SubElement(suite, "properties")
    values = candidate.evidence_identity(metadata)
    if wrong_identity:
        values["archive_sha256"] = "0" * 64
    for name, value in values.items():
        ET.SubElement(properties, "property", name=name, value=value)
    ET.ElementTree(root).write(output / "kubernetes-tests.xml")


@pytest.mark.parametrize("fault", ["failures", "skipped", "identity"])
def test_failed_skipped_or_stale_tests_cannot_leave_a_success_receipt(image_artifact, monkeypatch, fault):
    output, metadata = image_artifact
    junit(output, metadata, failures=int(fault == "failures"), skipped=int(fault == "skipped"), wrong_identity=fault == "identity")
    target = output / "kubernetes-image.json"
    target.write_text('{"old_success":true}')
    monkeypatch.setattr(candidate, "inspect_loaded", lambda *args: None)
    monkeypatch.setattr(candidate, "verify_running_images", lambda *args: [IMAGE_ID, IMAGE_ID])
    monkeypatch.setattr(candidate, "urlopen", lambda *args, **kwargs: pytest.fail("Invalid results must be rejected first"))
    with pytest.raises(RuntimeError, match="bound to this image"):
        candidate.receipt(SimpleNamespace(), output, COMMIT)
    assert not target.exists()


def test_receipt_binds_archive_running_ids_and_test_attempt(image_artifact, monkeypatch):
    output, metadata = image_artifact
    junit(output, metadata)
    monkeypatch.setattr(candidate, "inspect_loaded", lambda *args: None)
    monkeypatch.setattr(candidate, "verify_running_images", lambda *args: [IMAGE_ID, IMAGE_ID])

    class Response(io.BytesIO):
        status = 200

    monkeypatch.setattr(candidate, "urlopen", lambda *args, **kwargs: Response(json.dumps({"commit": COMMIT, "version": __version__}).encode()))
    proof = candidate.receipt(SimpleNamespace(), output, COMMIT)
    assert proof["pod_image_ids"] == [IMAGE_ID, IMAGE_ID]
    assert proof["archive_sha256"] == metadata["archive_sha256"]
    assert proof["run_id"] == "12345" and proof["run_attempt"] == 1
    assert validated_kubernetes(output, metadata) == proof


def test_classic_docker_config_digest_is_authoritative_when_api_omits_legacy_fields():
    config = {"os": "linux", "architecture": "amd64", "config": {"ArgsEscaped": True},
              "rootfs": {"diff_ids": ["sha256:" + "3" * 64]}}
    identity = "sha256:" + hashlib.sha256(json.dumps(config).encode()).hexdigest()
    inspected = {"Id": identity, "Os": "linux", "Architecture": "amd64", "Config": {},
                 "RootFS": {"Layers": config["rootfs"]["diff_ids"]}}
    assert matches_loaded(inspected, config, identity)
    assert not matches_loaded(inspected, config, "sha256:" + "0" * 64)


def test_running_container_identity_does_not_depend_on_synthetic_import_names(monkeypatch):
    container_id = "a" * 64
    calls = []

    def inspect(*args):
        calls.append(args)
        return json.dumps({"status": {"id": container_id, "state": "CONTAINER_RUNNING", "imageId": IMAGE_ID,
                                      "imageRef": "docker.io/library/import-old@sha256:" + "b" * 64}})

    monkeypatch.setattr(candidate, "command", inspect)
    assert candidate.running_container_image_id("containerd://" + container_id) == IMAGE_ID
    assert calls == [("docker", "exec", candidate.NODE, "crictl", "inspect", container_id)]


@pytest.mark.parametrize("fault", ["id", "state", "imageId"])
def test_wrong_stopped_or_missing_runtime_identity_is_rejected(monkeypatch, fault):
    container_id = "a" * 64
    status = {"id": container_id, "state": "CONTAINER_RUNNING", "imageId": IMAGE_ID}
    status[fault] = "incorrect"
    monkeypatch.setattr(candidate, "command", lambda *args: json.dumps({"status": status}))
    with pytest.raises(RuntimeError, match="expected running container"):
        candidate.running_container_image_id("containerd://" + container_id)


def test_missing_container_id_is_rejected_before_runtime_query(monkeypatch):
    monkeypatch.setattr(candidate, "command", lambda *args: pytest.fail("Missing container ID must not query CRI"))
    with pytest.raises(RuntimeError, match="containerd container ID"):
        candidate.running_container_image_id("")
