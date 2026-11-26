"""Validate Kubernetes API schemas and the local deployment's required safeguards."""

import argparse
import re
import shutil
from decimal import Decimal
from pathlib import Path

from scripts.kubernetes_local import ROOT, Kubernetes, require_local_server

FILES = (
    "namespace.yaml", "configmap.yaml", "postgres-service.yaml", "postgres-statefulset.yaml",
    "bootstrap-job.yaml", "app-deployment.yaml", "app-service.yaml",
)


def require(value, message):
    if not value:
        raise RuntimeError("Kubernetes validation: " + message)


def positive_quantity(value):
    number = re.match(r"^([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)", str(value))
    return number is not None and Decimal(number[1]) > 0


def check_documents(documents):
    """Catch valid-YAML changes which would remove course/runtime safeguards."""
    for filename in FILES:
        require(filename in documents, f"missing {filename}")
        document = documents[filename]
        if document["kind"] != "Namespace":
            require(document["metadata"].get("namespace") == "offscript", f"{filename} must use the offscript namespace")
    namespace = documents["namespace.yaml"]
    require(namespace["metadata"]["name"] == "offscript", "unexpected namespace")
    require(namespace["metadata"].get("labels", {}).get("pod-security.kubernetes.io/enforce") == "restricted", "restricted Pod Security is required")
    app = documents["app-deployment.yaml"]
    database = documents["postgres-statefulset.yaml"]
    bootstrap = documents["bootstrap-job.yaml"]
    require(app["spec"].get("replicas", 0) >= 2, "at least two app replicas are required")
    require(app["spec"].get("strategy") == {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 1, "maxUnavailable": 0}}, "RollingUpdate must preserve availability")
    for name, workload in (("app", app), ("database", database), ("bootstrap", bootstrap)):
        pod = workload["spec"]["template"]["spec"]
        security = pod.get("securityContext", {})
        require(pod.get("automountServiceAccountToken") is False, f"{name} must not mount an API token")
        require(security.get("runAsNonRoot") is True and security.get("runAsUser", 0) > 0, f"{name} must run as non-root")
        for container in pod["containers"]:
            security = container.get("securityContext", {})
            require(security.get("readOnlyRootFilesystem") is True and security.get("allowPrivilegeEscalation") is False, f"{name} must use a read-only, unprivileged container")
            require("ALL" in security.get("capabilities", {}).get("drop", []), f"{name} must drop capabilities")
            resources = container.get("resources", {})
            for bound in ("requests", "limits"):
                require(all(positive_quantity(resources.get(bound, {}).get(resource)) for resource in ("cpu", "memory")), f"{name} needs positive CPU and memory {bound}")
    container = app["spec"]["template"]["spec"]["containers"][0]
    for probe, path in (("startupProbe", "/health"), ("readinessProbe", "/ready"), ("livenessProbe", "/health")):
        require(container.get(probe, {}).get("httpGet") == {"path": path, "port": "http"}, f"app {probe} must use {path}")
    image = container["image"]
    require(re.fullmatch(r"ghcr\.io/hjarchivenote/offscript-budapest@sha256:[a-f0-9]{64}", image), "pin the app image by digest")
    require(bootstrap["spec"]["template"]["spec"]["containers"][0]["image"] == image, "bootstrap and app images must match")
    for filename, workload in (("app-service.yaml", app), ("postgres-service.yaml", database)):
        service = documents[filename]
        selector = service["spec"].get("selector", {})
        labels = workload["spec"]["template"]["metadata"]["labels"]
        require(selector and all(labels.get(key) == value for key, value in selector.items()), f"{filename} selector must match its workload")
        named_ports = {p["name"] for c in workload["spec"]["template"]["spec"]["containers"] for p in c.get("ports", [])}
        require(all(p["targetPort"] in named_ports for p in service["spec"]["ports"]), f"{filename} targetPort must match a container port")
    settings = documents["configmap.yaml"]["data"]
    require(settings.get("DATABASE_HOST") == documents["postgres-service.yaml"]["metadata"]["name"], "database host must match its Service")
    require("APP_COMMIT" not in settings, "configuration must not override the image's source identity")
    require(database["spec"].get("volumeClaimTemplates"), "PostgreSQL needs a persistent volume claim")
    for workload in (app, bootstrap):
        volumes = workload["spec"]["template"]["spec"].get("volumes", [])
        auth = next((v.get("secret") for v in volumes if v["name"] == "auth"), None)
        require(auth and auth.get("secretName") == "offscript-db-auth" and auth.get("items") == [{"key": "db_password", "path": "db_password"}], "app and bootstrap may mount only their restricted database password")


def validate(kube):
    server = kube.run("config", "view", "--minify", "-o", "jsonpath={.clusters[0].cluster.server}").strip()
    require_local_server(server)
    documents = {}
    files = sorted(path.name for path in (ROOT / "k8s").glob("*.yaml") if path.name != "kind.yaml")
    for filename in files:
        documents[filename] = kube.manifest(filename)
        kube.run("apply", "--dry-run=server", "--validate=strict", "-f", ROOT / "k8s" / filename)
    check_documents(documents)
    print(f"Kubernetes schema and safeguard checks passed for {len(files)} manifests.", flush=True)
    return documents


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    local_binary = ROOT / ".local/kubernetes/bin/kubectl"
    parser.add_argument("--kubectl", default=str(local_binary) if local_binary.is_file() else shutil.which("kubectl"))
    parser.add_argument("--kubeconfig", type=Path, default=ROOT / ".local/kubernetes/kubeconfig")
    args = parser.parse_args()
    if not args.kubectl or not args.kubeconfig.is_file():
        parser.error("Install kubectl and provide the private kind-offscript kubeconfig.")
    validate(Kubernetes(args.kubectl, args.kubeconfig.resolve()))


if __name__ == "__main__":
    main()
