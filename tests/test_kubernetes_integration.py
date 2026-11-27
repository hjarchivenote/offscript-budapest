"""Opt-in tests for the dedicated local/CI kind cluster, never a remote cluster."""

import copy
import hashlib
import json
import os
import secrets
import shutil
from pathlib import Path
from urllib.request import urlopen

import pytest

from scripts.check_kubernetes import check_documents, validate
from scripts.kubernetes_image import CONTEXT, URL, evidence_identity, setup, verify_running_images
from scripts.kubernetes_local import ROOT, Kubernetes, initialize_catalogue
from scripts.publish_image import validated_metadata


@pytest.fixture(scope="module")
def live_cluster(record_testsuite_property):
    config = os.environ.get("OFFSCRIPT_KUBERNETES_TEST_CONFIG")
    if not config:
        pytest.skip("Set OFFSCRIPT_KUBERNETES_TEST_CONFIG to opt into the dedicated kind checks")
    local_binary = ROOT / ".local/kubernetes/bin/kubectl"
    binary = os.environ.get("OFFSCRIPT_KUBECTL") or (str(local_binary) if local_binary.is_file() else shutil.which("kubectl"))
    directory = os.environ.get("OFFSCRIPT_KUBERNETES_IMAGE_DIR")
    commit = os.environ.get("CI_COMMIT_SHA")
    if not directory or not commit:
        pytest.fail("The Kubernetes suite requires the current container-stage image archive and commit; there is no older-image fallback.")
    output = Path(directory).resolve()
    metadata = validated_metadata(output, commit)
    kube = Kubernetes(binary, Path(config).resolve(), context=CONTEXT)
    documents = validate(kube)
    proof = setup(kube, output, commit)
    for key, value in evidence_identity(metadata).items():
        record_testsuite_property(key, value)
    return kube, documents, {**proof, "output": output, "metadata": metadata}


def test_service_routes_to_the_verified_ready_application(live_cluster):
    kube, documents, proof = live_cluster
    with urlopen(URL + "/ready", timeout=10) as response:
        assert response.status == 200
    with urlopen(URL + "/version", timeout=10) as response:
        assert json.load(response)["commit"] == proof["commit"]
    assert proof["ready_replicas"] == 2


def cluster_snapshot(kube):
    result = json.loads(kube.run("get", "deployment,statefulset,configmap,secret,service,pvc", "-o", "json"))
    # Hash sensitive data in memory; no credentials enter test output or artifacts.
    resources = [{"kind": item["kind"], "uid": item["metadata"]["uid"], "spec": item.get("spec"),
                  "data": item.get("data")} for item in result["items"]]
    return hashlib.sha256(json.dumps(resources, sort_keys=True).encode()).hexdigest()


def test_wrong_commit_is_blocked_before_changing_running_workloads(live_cluster):
    kube, _, proof = live_cluster
    before = cluster_snapshot(kube)
    with pytest.raises(ValueError, match="does not match"):
        setup(kube, proof["output"], "0" * 40)
    assert cluster_snapshot(kube) == before


def test_failed_initialization_retries_and_success_is_reused(live_cluster):
    kube, documents, proof = live_cluster
    base = "offscript-audit-" + secrets.token_hex(6)
    job = copy.deepcopy(documents["bootstrap-job.yaml"])
    job["metadata"] = {"name": base, "namespace": "offscript"}
    job["spec"].update(backoffLimit=0, activeDeadlineSeconds=45, ttlSecondsAfterFinished=300)
    pod = job["spec"]["template"]["spec"]
    pod.pop("volumes", None)
    container = pod["containers"][0]
    container["image"] = proof["image"]
    container["imagePullPolicy"] = "Never"
    container.pop("envFrom", None)
    container.pop("volumeMounts", None)
    container["env"] = [{"name": "READY_FLAG", "valueFrom": {"configMapKeyRef": {"name": base, "key": "ready"}}}]
    container["command"] = ["python", "-c", "import os,time; time.sleep(2); raise SystemExit(0 if os.environ['READY_FLAG']=='ok' else 1)"]
    flag = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": base, "namespace": "offscript"}, "data": {"ready": "not-ready"}}
    try:
        kube.apply(flag)
        with pytest.raises(RuntimeError, match="Fix the cause and rerun"):
            initialize_catalogue(kube, job, timeout=60)
        # Wait for terminal failure, so old Pods have finished before replacement.
        kube.run("wait", "--for=condition=failed", "job/" + base, "--timeout=45s", timeout=50)
        original = json.loads(kube.run("get", "job", base, "-o", "json"))
        flag["data"]["ready"] = "ok"
        kube.apply(flag)
        retry = initialize_catalogue(kube, job, timeout=60)
        assert retry.startswith(base + "-retry-")
        succeeded = json.loads(kube.run("get", "job", retry, "-o", "json"))
        assert initialize_catalogue(kube, job, timeout=60) == retry
        assert json.loads(kube.run("get", "job", retry, "-o", "json"))["metadata"]["uid"] == succeeded["metadata"]["uid"]
        assert json.loads(kube.run("get", "job", base, "-o", "json"))["metadata"]["uid"] == original["metadata"]["uid"]
    finally:
        jobs = json.loads(kube.run("get", "jobs", "-o", "json"))["items"]
        for item in jobs:
            name = item["metadata"]["name"]
            if name == base or name.startswith(base + "-retry-"):
                kube.run("delete", "job", name, "--wait=true", "--timeout=30s")
        kube.run("delete", "configmap", base, "--ignore-not-found")


@pytest.mark.parametrize("fault", ["missing-resource", "wrong-probe", "wrong-selector", "override-commit"])
def test_valid_yaml_cannot_remove_required_deployment_safeguards(live_cluster, fault):
    _, original, _ = live_cluster
    documents = copy.deepcopy(original)
    container = documents["app-deployment.yaml"]["spec"]["template"]["spec"]["containers"][0]
    if fault == "missing-resource":
        del container["resources"]["requests"]["memory"]
    elif fault == "wrong-probe":
        container["readinessProbe"]["httpGet"]["path"] = "/health"
    elif fault == "wrong-selector":
        documents["app-service.yaml"]["spec"]["selector"]["app.kubernetes.io/component"] = "wrong-component"
    else:
        documents["configmap.yaml"]["data"]["APP_COMMIT"] = "0" * 40
    with pytest.raises(RuntimeError, match="Kubernetes validation"):
        check_documents(documents)


def test_unknown_kubernetes_fields_fail_strict_server_validation(live_cluster):
    kube, documents, _ = live_cluster
    invalid = copy.deepcopy(documents["app-deployment.yaml"])
    invalid["spec"]["template"]["spec"]["offscriptUnknownField"] = True
    with pytest.raises(RuntimeError, match="unknown field|unknownField|Unknown field"):
        kube.run("apply", "--dry-run=server", "--validate=strict", "-f", "-", document=invalid)


def test_runtime_image_ids_cannot_be_substituted(live_cluster):
    kube, _, proof = live_cluster
    actual = verify_running_images(kube, proof["metadata"])
    assert actual == [proof["metadata"]["image_id"]] * 2
    substituted = {**proof["metadata"], "image_id": "sha256:" + "0" * 64}
    with pytest.raises(RuntimeError, match="image ID differs"):
        verify_running_images(kube, substituted)
