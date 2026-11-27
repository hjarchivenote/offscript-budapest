"""Test the container-stage image in an isolated kind cluster before publication."""

import argparse
import json
import os
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.request import urlopen

import yaml

from scripts.image_identity import archive_identity, matches_loaded
from scripts.kubernetes_local import (
    ROOT,
    Kubernetes,
    _deploy_verified,
    require_local_server,
    verify_image,
)
from scripts.publish_image import KUBERNETES_CHECKS, KUBERNETES_MIN_CASES, validated_metadata

CONTEXT = "kind-offscript-ci"
NODE = "offscript-ci-control-plane"
URL = "http://127.0.0.1:8769"


def command(*args):
    result = subprocess.run(list(map(str, args)), text=True, capture_output=True, timeout=240, check=False)
    if result.returncode:
        raise RuntimeError("Exact-image check command failed: " + result.stderr.strip()[-1000:])
    return result.stdout


def kind_config(output):
    config = yaml.safe_load((ROOT / "k8s/kind.yaml").read_text())
    nodes = config.get("nodes", [])
    if (config.get("kind") != "Cluster" or config.get("apiVersion") != "kind.x-k8s.io/v1alpha4"
            or config.get("networking", {}).get("apiServerAddress") != "127.0.0.1"
            or len(nodes) != 1 or nodes[0].get("role") != "control-plane"
            or nodes[0].get("extraPortMappings") != [
                {"containerPort": 30080, "hostPort": 8768, "listenAddress": "127.0.0.1", "protocol": "TCP"}]):
        raise RuntimeError("The checked-in kind configuration must retain the expected local-only Service mapping.")
    nodes[0]["extraPortMappings"][0]["hostPort"] = 8769
    output.mkdir(parents=True, exist_ok=True)
    path = output / "kind-ci.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False))
    return path


def require_ci_cluster(kube):
    if kube.context != CONTEXT:
        raise RuntimeError("Candidate images may run only in the isolated kind-offscript-ci context.")
    require_local_server(kube.run("config", "view", "--minify", "-o", "jsonpath={.clusters[0].cluster.server}").strip())
    nodes = json.loads(kube.run("get", "nodes", "-o", "json"))["items"]
    if {node["metadata"]["name"] for node in nodes} != {NODE}:
        raise RuntimeError("Refusing to test a candidate image in the application cluster.")


def inspect_loaded(metadata, output):
    image = json.loads(command("docker", "image", "inspect", metadata["image"]))[0]
    canonical_id, config = archive_identity(output / "image.tar", metadata["image"])
    labels = image.get("Config", {}).get("Labels") or {}
    if (canonical_id != metadata["image_id"] or not matches_loaded(image, config, canonical_id)
            or image.get("Os") != "linux" or image.get("Architecture") != "amd64"
            or labels.get("org.opencontainers.image.revision") != metadata["commit"]
            or labels.get("org.opencontainers.image.version") != metadata["version"]
            or labels.get("org.opencontainers.image.source") != "https://github.com/hjarchivenote/offscript-budapest"):
        raise RuntimeError("The loaded candidate is not the exact image validated by the container job.")
    return verify_image(metadata["image"], metadata["commit"])


def runtime_image_id(reference):
    # The runtime may expose a manifest reference or config digest. Resolve the
    # actual Pod image through CRI instead of comparing unlike digest formats.
    image = json.loads(command("docker", "exec", NODE, "crictl", "inspecti", reference))
    return image["status"]["id"]


def load(kube, output, commit, kind):
    (output / "kubernetes-image.json").unlink(missing_ok=True)
    metadata = validated_metadata(output, commit)
    require_ci_cluster(kube)
    command("docker", "image", "load", "--input", output / "image.tar")
    inspect_loaded(metadata, output)
    command(kind, "load", "image-archive", output / "image.tar", "--name", "offscript-ci")
    if runtime_image_id(metadata["image"]) != metadata["image_id"]:
        raise RuntimeError("The image imported into kind differs from the validated archive.")
    print("Imported the exact container-stage image into the isolated CI cluster.", flush=True)


def setup(kube, output, commit):
    metadata = validated_metadata(output, commit)
    require_ci_cluster(kube)
    verified = inspect_loaded(metadata, output)
    if runtime_image_id(metadata["image"]) != metadata["image_id"]:
        raise RuntimeError("The isolated cluster does not contain the validated candidate image.")
    return _deploy_verified(
        kube, metadata["image"], commit, verified, image_pull_policy="Never",
        secret_directory=output / "kubernetes-secrets", proof_path=output / "kubernetes-runtime.json",
    )


def verify_running_images(kube, metadata):
    require_ci_cluster(kube)
    pods = json.loads(kube.run("get", "pods", "-l", "app.kubernetes.io/component=web", "-o", "json"))["items"]
    pods = [pod for pod in pods if not pod["metadata"].get("deletionTimestamp")]
    if len(pods) != 2:
        raise RuntimeError("Exactly two candidate replicas must be present.")
    identities = []
    for pod in pods:
        container = pod["spec"]["containers"][0]
        status = pod["status"].get("containerStatuses", [])
        if (container["image"] != metadata["image"] or container.get("imagePullPolicy") != "Never"
                or len(status) != 1 or not status[0].get("ready")):
            raise RuntimeError("A replica is not running the ready, locally loaded candidate.")
        actual = runtime_image_id(status[0]["imageID"])
        if actual != metadata["image_id"]:
            raise RuntimeError("A running Pod's image ID differs from the container-stage image.")
        identities.append(actual)
    return identities


def evidence_identity(metadata):
    return {"source_commit": metadata["commit"], "image_id": metadata["image_id"],
            "archive_sha256": metadata["archive_sha256"], "context": CONTEXT,
            "run_id": os.environ.get("GITHUB_RUN_ID", "local"),
            "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", "0")}


def receipt(kube, output, commit):
    destination = output / "kubernetes-image.json"
    destination.unlink(missing_ok=True)
    metadata = validated_metadata(output, commit)
    inspect_loaded(metadata, output)
    identities = verify_running_images(kube, metadata)
    xml_path = output / "kubernetes-tests.xml"
    root = ET.parse(xml_path).getroot()
    suites = root.findall(".//testsuite") if root.tag != "testsuite" else [root]
    properties = {item.attrib["name"]: item.attrib["value"] for item in root.findall(".//property")}
    tested = sum(int(s.get("tests", "0")) for s in suites)
    failures = sum(int(s.get("failures", "0")) + int(s.get("errors", "0")) for s in suites)
    skipped = sum(int(s.get("skipped", "0")) for s in suites)
    if tested < KUBERNETES_MIN_CASES or failures or skipped or any(properties.get(k) != v for k, v in evidence_identity(metadata).items()):
        raise RuntimeError("Successful, unskipped Kubernetes tests bound to this image and CI attempt are required.")
    with urlopen(URL + "/ready", timeout=10) as response:
        if response.status != 200:
            raise RuntimeError("The candidate Service is unavailable.")
    with urlopen(URL + "/version", timeout=10) as response:
        if json.load(response) != {"commit": commit, "version": metadata["version"]}:
            raise RuntimeError("The candidate Service is running another source revision.")
    proof = {key: metadata[key] for key in ("image", "commit", "version", "image_id", "archive_sha256")}
    proof.update(schema=1, context=CONTEXT, run_id=os.environ.get("GITHUB_RUN_ID", "local"),
                 run_attempt=int(os.environ.get("GITHUB_RUN_ATTEMPT", "0")), ready_replicas=2,
                 tested_cases=tested, failures=failures, skipped=skipped,
                 pod_image_ids=identities, checks=sorted(KUBERNETES_CHECKS))
    destination.write_text(json.dumps(proof, indent=2) + "\n")
    print("Kubernetes receipt: both running image IDs match the exact container-stage image.", flush=True)
    return proof


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("config", "load", "receipt"))
    parser.add_argument("--output", type=Path, default=ROOT / ".local/ci")
    parser.add_argument("--commit", default=os.environ.get("CI_COMMIT_SHA"))
    parser.add_argument("--kubeconfig", type=Path, default=ROOT / ".local/ci/kubeconfig")
    local = ROOT / ".local/kubernetes/bin"
    parser.add_argument("--kubectl", default=str(local / "kubectl") if (local / "kubectl").is_file() else shutil.which("kubectl"))
    parser.add_argument("--kind", default=str(local / "kind") if (local / "kind").is_file() else shutil.which("kind"))
    args = parser.parse_args()
    output = args.output.resolve()
    if args.action == "config":
        print(kind_config(output))
        return
    if not args.commit or not args.kubectl or not args.kubeconfig.is_file():
        parser.error("Provide the container-stage commit, kubectl and the private isolated-CI kubeconfig.")
    kube = Kubernetes(args.kubectl, args.kubeconfig.resolve(), context=CONTEXT)
    if args.action == "load":
        if not args.kind:
            parser.error("Install kind before importing the image archive.")
        load(kube, output, args.commit, args.kind)
    else:
        receipt(kube, output, args.commit)


if __name__ == "__main__":
    main()
