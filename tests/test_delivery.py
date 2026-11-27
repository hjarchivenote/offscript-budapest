import copy
import hashlib
import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from scripts import delivery, release_package
from scripts.kubernetes_local import ROOT

COMMIT = "a" * 40
REFERENCE = "ghcr.io/hjarchivenote/offscript-budapest@sha256:" + "1" * 64
RUN_ID = 12345


def approved():
    return {"id": RUN_ID, "head_sha": COMMIT, "head_branch": "main", "event": "push",
            "status": "completed", "conclusion": "success", "workflow_id": release_package.WORKFLOW_ID,
            "path": ".github/workflows/ci.yml", "run_attempt": 1,
            "repository": {"id": release_package.REPOSITORY_ID}, "head_repository": {"id": release_package.REPOSITORY_ID}}


def receipt():
    return {"commit": COMMIT, "repository": release_package.REPOSITORY,
            "run_url": f"https://github.com/{release_package.REPOSITORY}/actions/runs/{RUN_ID}",
            "pull_verified": True, "version": "0.1.0", "platform": "linux/amd64",
            "checks": sorted(release_package.CHECKS), "image_id": "sha256:" + "2" * 64,
            "registry_reference": REFERENCE}


def archive(entries):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as output:
        for name, data in entries.items():
            output.writestr(name, data)
    payload = stream.getvalue()
    return payload, {"id": 5, "digest": "sha256:" + hashlib.sha256(payload).hexdigest()}


@pytest.fixture
def package(tmp_path):
    (tmp_path / "published-image.json").write_text(json.dumps(receipt()))
    source = release_package.prepare(tmp_path, COMMIT, RUN_ID, 1)
    return {str(path.relative_to(source)): path.read_bytes() for path in source.rglob("*") if path.is_file()}


def test_deployment_package_binds_only_data_to_the_published_revision(package, tmp_path):
    payload, artifact = archive(package)
    target = tmp_path / "extracted"
    assert release_package.unpack(payload, artifact, approved(), target) == receipt()
    assert {str(path.relative_to(target)) for path in target.rglob("*") if path.is_file()} == set(release_package.ARCHIVE_FILES)
    assert (target / "k8s/configmap.yaml").read_bytes() == (ROOT / "k8s/configmap.yaml").read_bytes()


@pytest.mark.parametrize("fault", ["checksum", "changed-file", "wrong-attempt", "wrong-commit", "extra-script", "traversal"])
def test_bad_package_is_rejected_before_writing(package, tmp_path, fault):
    if fault == "changed-file":
        package["k8s/configmap.yaml"] += b"\n# changed after publication\n"
    elif fault in {"wrong-attempt", "wrong-commit"}:
        metadata = json.loads(package["delivery.json"])
        metadata["run_attempt" if fault == "wrong-attempt" else "commit"] = 2 if fault == "wrong-attempt" else "b" * 40
        package["delivery.json"] = json.dumps(metadata).encode()
    elif fault == "extra-script":
        package["scripts/run_on_host.py"] = b"raise SystemExit(1)"
    elif fault == "traversal":
        package["../outside.txt"] = b"unexpected"
    payload, artifact = archive(package)
    if fault == "checksum":
        artifact["digest"] = "sha256:" + "0" * 64
    target = tmp_path / "not-written"
    with pytest.raises(ValueError):
        release_package.unpack(payload, artifact, approved(), target)
    assert not target.exists() and not (tmp_path / "outside.txt").exists()


def test_symbolic_links_are_rejected(package, tmp_path):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as output:
        for name, data in package.items():
            info = zipfile.ZipInfo(name)
            info.external_attr = (0o120777 if name == "k8s/configmap.yaml" else 0o100600) << 16
            output.writestr(info, data)
    data = stream.getvalue()
    with pytest.raises(ValueError, match="only the expected"):
        release_package.unpack(data, {"digest": "sha256:" + hashlib.sha256(data).hexdigest()}, approved(), tmp_path / "out")


@pytest.mark.parametrize("field,value", [("commit", "b" * 40), ("pull_verified", False), ("checks", []), ("platform", "linux/arm64"), ("run_url", "https://example.com/not-the-run")])
def test_invalid_receipt_cannot_request_delivery(tmp_path, field, value):
    invalid = receipt()
    invalid[field] = value
    (tmp_path / "published-image.json").write_text(json.dumps(invalid))
    with pytest.raises(ValueError, match="receipt"):
        release_package.prepare(tmp_path, COMMIT, RUN_ID, 1)
    assert not (tmp_path / "delivery").exists()


@pytest.mark.parametrize("field,value", [
    ("event", "pull_request"), ("event", "workflow_dispatch"), ("head_branch", "feature"),
    ("head_sha", "b" * 40), ("conclusion", "failure"), ("conclusion", "cancelled"),
    ("status", "in_progress"), ("workflow_id", 1), ("path", ".github/workflows/untrusted.yml"),
    ("repository", {"id": 1}), ("head_repository", {"id": 1}),
])
def test_only_successful_main_push_is_approved(field, value):
    run = approved()
    assert delivery.approved_run(run, COMMIT)
    run[field] = value
    assert not delivery.approved_run(run, COMMIT)


def test_storage_download_never_receives_github_credentials():
    github = delivery.GitHub("synthetic-private-test-token")
    github.close()
    seen = []

    def api(request):
        assert request.headers["authorization"] == "Bearer synthetic-private-test-token"
        return httpx.Response(302, headers={"location": "https://productionresults.blob.core.windows.net/artifact"})

    def storage(request):
        seen.append(request)
        assert "authorization" not in request.headers
        return httpx.Response(200, content=b"ZIP")

    github.api = httpx.Client(base_url="https://api.github.com", headers={"Authorization": "Bearer synthetic-private-test-token"}, transport=httpx.MockTransport(api))
    github.download = httpx.Client(transport=httpx.MockTransport(storage))
    try:
        assert github.archive({"id": 5}) == b"ZIP" and len(seen) == 1
    finally:
        github.close()


@pytest.mark.parametrize("change", ["main", "attempt", "status"])
def test_changed_main_or_ci_attempt_invalidates_release(monkeypatch, change):
    github = delivery.GitHub("synthetic-private-test-token")
    current = approved()
    if change == "attempt":
        current["run_attempt"] = 2
    elif change == "status":
        current["status"] = "in_progress"
    monkeypatch.setattr(github, "get", lambda *args, **kwargs: current)
    monkeypatch.setattr(github, "head", lambda: "b" * 40 if change == "main" else COMMIT)
    try:
        with pytest.raises(delivery.DeliveryError, match="changed"):
            github.require_current(approved())
    finally:
        github.close()


def test_missing_kubernetes_success_blocks_delivery(monkeypatch):
    github = delivery.GitHub("synthetic-private-test-token")
    jobs = [{"name": name, "conclusion": "success"} for name in delivery.REQUIRED_JOBS if not name.startswith("Kubernetes")]
    monkeypatch.setattr(github, "get", lambda *args, **kwargs: {"jobs": jobs})
    try:
        with pytest.raises(delivery.DeliveryError, match="Every required"):
            github.artifact(approved())
    finally:
        github.close()


class API:
    def __init__(self, payload=None, artifact=None):
        self.run = approved()
        self.payload, self.artifact_value = payload, artifact
        self.artifact_reads = self.current_checks = 0
        self.stale = False

    def head(self):
        return COMMIT

    def latest_run(self, commit):
        return copy.deepcopy(self.run)

    def artifact(self, run):
        self.artifact_reads += 1
        return self.artifact_value

    def archive(self, artifact):
        return self.payload

    def require_current(self, run):
        self.current_checks += 1
        if self.stale:
            raise delivery.DeliveryError("Main changed; this release was not applied.")


def test_failed_ci_preserves_previous_release_without_fetching_or_applying(monkeypatch, tmp_path):
    api = API()
    api.run["conclusion"] = "failure"
    controller = delivery.Controller(api, "kubectl", tmp_path / "kubeconfig", tmp_path / "worker")
    previous = {"commit": "b" * 40, "image": "previous-pinned-image"}
    controller.state["last_success"] = previous
    monkeypatch.setattr(delivery, "deploy", lambda *a, **k: pytest.fail("Failed CI must not deploy"))
    result = controller.step()
    assert result["status"] == "blocked-ci" and result["last_success"] == previous
    assert api.artifact_reads == 0


@pytest.mark.parametrize("stale", [False, True])
def test_controller_uses_snapshot_rechecks_main_and_resumes_idempotently(package, monkeypatch, tmp_path, stale):
    payload, artifact = archive(package)
    api = API(payload, artifact)
    api.stale = stale
    roots, applied = [], []

    def cluster(*args, root=ROOT):
        return SimpleNamespace(root=Path(root))

    def validate(kube):
        roots.append(kube.root)
        assert kube.root != ROOT and kube.root.is_relative_to(tmp_path)
        assert (kube.root / "k8s/configmap.yaml").read_bytes() == package["k8s/configmap.yaml"]

    def deploy(kube, image, commit, *, before_apply, proof_path):
        assert roots == [kube.root]
        before_apply()
        applied.append((image, commit))
        return {"ready_replicas": 2}

    monkeypatch.setattr(delivery, "Kubernetes", cluster)
    monkeypatch.setattr(delivery, "validate", validate)
    monkeypatch.setattr(delivery, "deploy", deploy)
    monkeypatch.setattr(delivery, "application_matches", lambda *args: True)
    folder = tmp_path / "worker"
    controller = delivery.Controller(api, "kubectl", tmp_path / "private-config", folder)
    result = controller.step()
    assert api.current_checks == 1
    if stale:
        assert result["status"] == "deployment-failed" and not applied
        assert "last_success" not in result
        assert controller.step()["status"] == "deployment-failed" and api.artifact_reads == 1
    else:
        assert result["status"] == "up-to-date" and applied == [(REFERENCE, COMMIT)]
        assert result["last_success"]["artifact_digest"] == artifact["digest"]
        resumed = delivery.Controller(api, "kubectl", tmp_path / "private-config", folder)
        assert resumed.step()["status"] == "up-to-date" and api.artifact_reads == 1
    assert not roots[0].exists()


def test_only_one_worker_can_hold_local_lock(tmp_path):
    with delivery.worker_lock(tmp_path):
        assert delivery.worker_running(tmp_path)
        with pytest.raises(delivery.DeliveryError, match="already running"), delivery.worker_lock(tmp_path):
            pytest.fail("The second worker must not acquire the lock")
    assert not delivery.worker_running(tmp_path)
