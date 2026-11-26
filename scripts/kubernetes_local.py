"""Apply the local Kubernetes environment without changing global kubeconfig."""

import argparse
import base64
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from scripts.prepare_local import prepare

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_IMAGE = "ghcr.io/hjarchivenote/offscript-budapest@sha256:eea0242073351d2ccbb5c2690970f1b2fa47ee01899df821ef35115f62d14969"
DEFAULT_COMMIT = "77ca2ecb4654d0526ec216aefc0bc598a4f5b903"


def require_local_server(server):
    parsed = urlsplit(server)
    if parsed.scheme != "https" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("This local setup requires the dedicated loopback kind-offscript cluster.")


class Kubernetes:
    def __init__(self, binary, kubeconfig):
        self.command = [str(binary), "--kubeconfig", str(kubeconfig), "--context", "kind-offscript", "--namespace", "offscript"]

    def run(self, *args, document=None, sensitive=False, timeout=40):
        result = subprocess.run(
            [*self.command, *map(str, args)], cwd=ROOT, text=True, capture_output=True,
            input=json.dumps(document) if document is not None else None, timeout=timeout, check=False,
        )
        if result.returncode:
            message = "Kubernetes credential operation failed." if sensitive else result.stderr.strip()
            raise RuntimeError(message or "Kubernetes operation failed.")
        return result.stdout

    def apply(self, document, *, sensitive=False):
        return self.run("apply", "-f", "-", document=document, sensitive=sensitive)

    def manifest(self, name):
        return json.loads(self.run("create", "--dry-run=client", "--validate=false", "-f", ROOT / "k8s" / name, "-o", "json"))


def ensure_credentials(kube, directory):
    prepare(directory)
    data = {
        key: base64.b64encode((directory / filename).read_bytes()).decode()
        for key, filename in (("db_password", "db-password"), ("db_admin_password", "admin-password"))
    }
    existing = kube.run("get", "secret", "offscript-db-auth", "--ignore-not-found", "-o", "json", sensitive=True)
    if existing:
        secret = json.loads(existing)
        if secret.get("data") != data or secret.get("immutable") is not True:
            raise RuntimeError("Existing cluster credentials do not match the local files. Preserve the database and restore its original credentials.")
        return
    kube.apply({
        "apiVersion": "v1", "kind": "Secret", "metadata": {"name": "offscript-db-auth", "namespace": "offscript"},
        "type": "Opaque", "immutable": True, "data": data,
    }, sensitive=True)


def deploy(kube, image, commit):
    if not re.fullmatch(r"ghcr\.io/hjarchivenote/offscript-budapest@sha256:[a-f0-9]{64}", image):
        raise ValueError("Use a verified GHCR image digest, not a mutable tag.")
    if not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise ValueError("Provide the image's full source commit.")
    server = kube.run("config", "view", "--minify", "-o", "jsonpath={.clusters[0].cluster.server}").strip()
    require_local_server(server)
    kube.run("apply", "-f", ROOT / "k8s/namespace.yaml")
    kube.run("apply", "-f", ROOT / "k8s/configmap.yaml")
    ensure_credentials(kube, ROOT / ".local/kubernetes/secrets")
    init = json.loads(kube.run("create", "configmap", "offscript-db-init", "--from-file=10-offscript.sql=" + str(ROOT / "docker/init-app-db.sql"), "--dry-run=client", "-o", "json"))
    kube.apply(init)
    kube.run("apply", "-f", ROOT / "k8s/postgres-service.yaml")
    kube.run("apply", "-f", ROOT / "k8s/postgres-statefulset.yaml")
    print("Waiting for PostgreSQL and its persistent volume…", flush=True)
    kube.run("rollout", "status", "statefulset/offscript-db", "--timeout=180s", timeout=200)
    config_hash = hashlib.sha256((ROOT / "k8s/configmap.yaml").read_bytes()).hexdigest()
    job = kube.manifest("bootstrap-job.yaml")
    job["spec"]["template"]["spec"]["containers"][0]["image"] = image
    suffix = hashlib.sha256((json.dumps(job["spec"], sort_keys=True) + config_hash).encode()).hexdigest()[:12]
    job["metadata"]["name"] = "offscript-bootstrap-" + suffix
    kube.apply(job)
    print("Waiting for one-shot catalogue initialization…", flush=True)
    kube.run("wait", "--for=condition=complete", "job/" + job["metadata"]["name"], "--timeout=180s", timeout=200)
    app = kube.manifest("app-deployment.yaml")
    app["spec"]["template"]["spec"]["containers"][0]["image"] = image
    app["spec"]["template"]["metadata"]["annotations"] = {
        "offscript.local/source-image": image, "offscript.local/source-commit": commit,
        "offscript.local/config-sha256": config_hash,
    }
    kube.apply(app)
    kube.run("apply", "-f", ROOT / "k8s/app-service.yaml")
    print("Waiting for both application replicas…", flush=True)
    kube.run("rollout", "status", "deployment/offscript", "--timeout=180s", timeout=200)
    deployment = json.loads(kube.run("get", "deployment", "offscript", "-o", "json"))
    if deployment["status"].get("readyReplicas") != 2 or deployment["status"].get("availableReplicas") != 2:
        raise RuntimeError("Both application replicas must be ready and available.")
    pods = json.loads(kube.run("get", "pods", "-l", "app.kubernetes.io/component=web", "-o", "json"))["items"]
    versions = []
    for pod in pods:
        if pod.get("metadata", {}).get("deletionTimestamp"):
            continue
        version = json.loads(kube.run("exec", pod["metadata"]["name"], "--", "python", "-c", "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/version',timeout=5).read().decode())"))
        if version.get("commit") != commit:
            raise RuntimeError("An application replica is running a different source commit.")
        versions.append({"pod": pod["metadata"]["name"], **version})
    if len(versions) != 2:
        raise RuntimeError("Expected two verified application replicas.")
    proof = {"namespace": "offscript", "context": "kind-offscript", "image": image, "commit": commit,
             "bootstrap_job": job["metadata"]["name"], "ready_replicas": 2, "versions": versions}
    output = ROOT / ".local/kubernetes/validation-state.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(proof, indent=2) + "\n")
    print("Kubernetes is ready: PostgreSQL and two verified application replicas.", flush=True)
    return proof


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    local_binary = ROOT / ".local/kubernetes/bin/kubectl"
    parser.add_argument("--kubectl", default=str(local_binary) if local_binary.is_file() else shutil.which("kubectl"))
    parser.add_argument("--kubeconfig", type=Path, default=ROOT / ".local/kubernetes/kubeconfig")
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument("--commit", default=DEFAULT_COMMIT)
    args = parser.parse_args()
    if not args.kubectl or not args.kubeconfig.is_file():
        parser.error("Install a compatible kubectl and create the dedicated kind-offscript cluster first.")
    deploy(Kubernetes(args.kubectl, args.kubeconfig.resolve()), args.image, args.commit)


if __name__ == "__main__":
    main()
