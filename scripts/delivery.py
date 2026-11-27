"""Pull successful main CI releases into the private local kind cluster."""

import argparse
import fcntl
import getpass
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import zipfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from scripts.check_kubernetes import validate
from scripts.kubernetes_local import ROOT, Kubernetes, deploy
from scripts.release_package import (
    MAX_PACKAGE_BYTES,
    REPOSITORY,
    REPOSITORY_ID,
    WORKFLOW_ID,
    unpack,
)

REQUIRED_JOBS = {
    "Lint and automated tests", "Build, security and real application checks",
    "Kubernetes configuration and recovery checks", "CI passed",
    "Publish and pull-verify the validated image",
}


class DeliveryError(RuntimeError):
    pass


class AuthenticationRequired(DeliveryError):
    pass


AUTHENTICATION_MESSAGE = (
    "GitHub read authentication is missing, expired or invalid. "
    "Update the configured credential; the worker will retry without changing the running application."
)


def approved_run(run, commit):
    return (
        re.fullmatch(r"[a-f0-9]{40}", commit) is not None
        and run.get("head_sha") == commit and run.get("head_branch") == "main"
        and run.get("event") == "push" and run.get("status") == "completed"
        and run.get("conclusion") == "success" and run.get("workflow_id") == WORKFLOW_ID
        and run.get("path") == ".github/workflows/ci.yml"
        and run.get("repository", {}).get("id") == REPOSITORY_ID
        and run.get("head_repository", {}).get("id") == REPOSITORY_ID
        and type(run.get("id")) is int and type(run.get("run_attempt")) is int
    )


def token_value(value):
    if not isinstance(value, str) or not re.fullmatch(r"[\x21-\x7e]{1,4096}", value):
        raise AuthenticationRequired(AUTHENTICATION_MESSAGE)
    return value


def token_path():
    return Path(os.environ.get("OFFSCRIPT_GITHUB_TOKEN_FILE", ROOT / ".local/delivery/github-token")).expanduser()


def file_read_token(path):
    try:
        # Do not follow a token symlink or block on a FIFO/device masquerading as a file.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "r", encoding="ascii") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_size > 4097):
                raise AuthenticationRequired(AUTHENTICATION_MESSAGE)
            return token_value(stream.read(4097).strip())
    except (OSError, UnicodeError):
        raise AuthenticationRequired(AUTHENTICATION_MESSAGE) from None


def git_read_token():
    if "OFFSCRIPT_GITHUB_TOKEN" in os.environ:
        return token_value(os.environ["OFFSCRIPT_GITHUB_TOKEN"])
    path = token_path()
    if "OFFSCRIPT_GITHUB_TOKEN_FILE" in os.environ or path.exists() or path.is_symlink():
        return file_read_token(path)
    try:
        result = subprocess.run(
            ["git", "credential", "fill"], input="protocol=https\nhost=github.com\n\n",
            text=True, capture_output=True, timeout=15, check=False,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.SubprocessError):
        raise AuthenticationRequired(AUTHENTICATION_MESSAGE) from None
    fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if result.returncode or not fields.get("password"):
        raise AuthenticationRequired(AUTHENTICATION_MESSAGE)
    return token_value(fields["password"])


class GitHub:
    def __init__(self, token=None, *, token_provider=None):
        if token is not None and token_provider is not None:
            raise ValueError("Choose one GitHub credential source.")
        self.token_provider = token_provider or ((lambda: token) if token is not None else git_read_token)
        self.api = httpx.Client(base_url="https://api.github.com", timeout=30, follow_redirects=False, headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "offscript-local-delivery",
        })
        self.download = httpx.Client(timeout=40, follow_redirects=False)

    def close(self):
        self.api.close()
        self.download.close()

    def read_token(self):
        # Providers can fail with sensitive subprocess/transport details. Never log them.
        try:
            return token_value(self.token_provider())
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
            raise AuthenticationRequired(AUTHENTICATION_MESSAGE) from None

    def request(self, path, **params):
        token = self.read_token()
        for attempt in range(2):
            try:
                response = self.api.get(f"/repos/{REPOSITORY}/" + path, params=params,
                                        headers={"Authorization": "Bearer " + token})
            except httpx.HTTPError:
                raise DeliveryError("GitHub could not be reached; the previous deployment is preserved.") from None
            if response.status_code != 401:
                return response
            response.close()
            if attempt == 0:
                refreshed = self.read_token()
                if refreshed != token:
                    token = refreshed
                    continue
            raise AuthenticationRequired(AUTHENTICATION_MESSAGE)
        raise AssertionError("Unreachable authentication retry state")

    def get(self, path, **params):
        response = self.request(path, **params)
        if response.status_code != 200:
            raise DeliveryError(f"GitHub read request failed (HTTP {response.status_code}).")
        return response.json()

    def head(self):
        commit = self.get("branches/main")["commit"]["sha"]
        if not re.fullmatch(r"[a-f0-9]{40}", commit):
            raise DeliveryError("GitHub returned an invalid main revision.")
        return commit

    def latest_run(self, commit):
        runs = self.get("actions/workflows/ci.yml/runs", branch="main", event="push", head_sha=commit, per_page=10)["workflow_runs"]
        return max(runs, key=lambda run: run["id"]) if runs else None

    def require_current(self, run):
        current = self.get(f"actions/runs/{run['id']}")
        if (self.head() != run["head_sha"] or not approved_run(current, run["head_sha"])
                or current["run_attempt"] != run["run_attempt"]):
            raise DeliveryError("Main or its CI attempt changed; this release was not applied.")

    def artifact(self, run):
        jobs = self.get(f"actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs", per_page=100)["jobs"]
        if not REQUIRED_JOBS <= {job["name"] for job in jobs if job["conclusion"] == "success"}:
            raise DeliveryError("Every required CI and publication job must succeed before deployment.")
        name = f"deployment-request-{run['head_sha']}-attempt-{run['run_attempt']}"
        artifacts = self.get(f"actions/runs/{run['id']}/artifacts", name=name, per_page=100)["artifacts"]
        if not artifacts:
            return None
        if len(artifacts) != 1:
            raise DeliveryError("Expected exactly one deployment request for this CI attempt.")
        artifact = artifacts[0]
        source = artifact.get("workflow_run", {})
        if (artifact.get("expired") or source.get("id") != run["id"]
                or source.get("head_sha") != run["head_sha"] or source.get("head_branch") != "main"
                or source.get("repository_id") != REPOSITORY_ID
                or source.get("head_repository_id") != REPOSITORY_ID
                or not re.fullmatch(r"sha256:[a-f0-9]{64}", artifact.get("digest", ""))):
            raise DeliveryError("Deployment request provenance is invalid or expired; rerun main CI for a fresh request.")
        return artifact

    def archive(self, artifact):
        try:
            response = self.request(f"actions/artifacts/{artifact['id']}/zip")
            if response.status_code != 302:
                raise DeliveryError(f"GitHub artifact read request failed (HTTP {response.status_code}).")
            location = response.headers.get("location", "")
            url = urlsplit(location)
            if (url.scheme != "https" or not url.hostname
                    or not (url.hostname.endswith(".blob.core.windows.net") or url.hostname.endswith(".actions.githubusercontent.com"))):
                raise DeliveryError("GitHub returned an unexpected artifact download location.")
            # A separate client prevents forwarding the GitHub credential to storage.
            with self.download.stream("GET", location) as download:
                if download.status_code != 200:
                    raise DeliveryError(f"Deployment artifact download failed (HTTP {download.status_code}).")
                data = bytearray()
                for chunk in download.iter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_PACKAGE_BYTES:
                        raise DeliveryError("Deployment artifact exceeds the allowed size.")
                return bytes(data)
        except httpx.HTTPError:
            raise DeliveryError("Deployment artifact download could not be completed.") from None


def application_matches(kube, release):
    try:
        app = json.loads(kube.run("get", "deployment", "offscript", "-o", "json"))
        pod = app["spec"]["template"]
        if (pod["spec"]["containers"][0]["image"] != release["image"]
                or pod["metadata"]["annotations"].get("offscript.local/source-commit") != release["commit"]
                or app["status"].get("readyReplicas") != 2 or app["status"].get("availableReplicas") != 2):
            return False
        with httpx.Client(timeout=10, trust_env=False) as client:
            ready = client.get("http://127.0.0.1:8768/ready")
            version = client.get("http://127.0.0.1:8768/version")
            return ready.status_code == 200 and version.status_code == 200 and version.json() == {
                "commit": release["commit"], "version": release["version"],
            }
    except (KeyError, ValueError, RuntimeError, OSError, httpx.HTTPError, subprocess.SubprocessError):
        return False


class Controller:
    def __init__(self, github, binary, kubeconfig, directory):
        self.github, self.binary, self.kubeconfig = github, binary, kubeconfig
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        self.path = self.directory / "state.json"
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {}

    def report(self, status, **values):
        old = (self.state.get("status"), self.state.get("desired_commit"), self.state.get("error"))
        self.state.update(values, status=status, updated_at=datetime.now(UTC).isoformat())
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.state, indent=2) + "\n")
        temporary.chmod(0o600)
        temporary.replace(self.path)
        if old != (status, self.state.get("desired_commit"), self.state.get("error")):
            event = {key: self.state.get(key) for key in ("updated_at", "status", "desired_commit", "run_url", "error")}
            with (self.directory / "events.jsonl").open("a") as stream:
                stream.write(json.dumps(event) + "\n")
            print(json.dumps(event), flush=True)
        return self.state

    def step(self):
        if self.state.get("next_retry_epoch", 0) > time.time():
            return self.state
        try:
            return self.reconcile()
        except AuthenticationRequired:
            return self.report("authentication-required", error=AUTHENTICATION_MESSAGE,
                               failure_count=self.state.get("failure_count", 0) + 1, next_retry_epoch=0)
        except (DeliveryError, ValueError, KeyError, TypeError, OSError, RuntimeError, subprocess.SubprocessError, zipfile.BadZipFile) as error:
            count = self.state.get("failure_count", 0) + 1
            return self.report("deployment-failed", error=str(error), failure_count=count,
                               next_retry_epoch=time.time() + min(300, 15 * 2 ** min(count - 1, 5)))

    def reconcile(self):
        commit = self.github.head()
        if self.state.get("desired_commit") != commit:
            self.report("waiting-ci", desired_commit=commit, run_url=None, error=None, failure_count=0, next_retry_epoch=0)
        run = self.github.latest_run(commit)
        if not run:
            return self.report("waiting-ci", run_url=None, error=None)
        run_url = f"https://github.com/{REPOSITORY}/actions/runs/{run['id']}"
        if run.get("status") != "completed":
            return self.report("waiting-ci", run_url=run_url, error=None)
        if run.get("conclusion") != "success":
            return self.report("blocked-ci", run_url=run_url, error="Latest main CI did not succeed; no release was applied.")
        if not approved_run(run, commit):
            raise DeliveryError("Only a successful push to this repository's current main may deploy.")
        kube = Kubernetes(self.binary, self.kubeconfig)
        previous = self.state.get("last_success", {})
        if (previous.get("commit") == commit and previous.get("run_id") == run["id"]
                and previous.get("run_attempt") == run["run_attempt"] and application_matches(kube, previous)):
            return self.report("up-to-date", run_url=run_url, error=None, failure_count=0)
        artifact = self.github.artifact(run)
        if artifact is None:
            return self.report("waiting-package", run_url=run_url, error="This CI run has no deployment request; waiting for a main release with delivery enabled.")
        payload = self.github.archive(artifact)
        self.report("verifying-release", run_url=run_url, error=None)
        with tempfile.TemporaryDirectory(prefix="release-", dir=self.directory) as temporary:
            source = Path(temporary)
            receipt = unpack(payload, artifact, run, source)
            kube = Kubernetes(self.binary, self.kubeconfig, root=source)
            validate(kube)
            proof_path = self.directory / f"rollout-{commit}-{run['run_attempt']}.json"
            self.report("deploying", error=None)
            proof = deploy(kube, receipt["registry_reference"], commit,
                           before_apply=lambda: self.github.require_current(run), proof_path=proof_path)
            release = {"commit": commit, "image": receipt["registry_reference"], "version": receipt["version"],
                       "run_id": run["id"], "run_attempt": run["run_attempt"], "run_url": run_url,
                       "artifact_id": artifact["id"], "artifact_digest": artifact["digest"],
                       "ready_replicas": proof["ready_replicas"], "proof_path": str(proof_path)}
            if not application_matches(kube, release):
                raise DeliveryError("Rollout completed but Service availability or the source version did not match.")
        return self.report("up-to-date", last_success=release, error=None, failure_count=0, next_retry_epoch=0)


@contextmanager
def worker_lock(directory):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    with (directory / "worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DeliveryError("A delivery worker is already running for this project.") from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def worker_running(directory):
    path = directory / "worker.lock"
    if not path.exists():
        return False
    with path.open("r+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(lock, fcntl.LOCK_UN)
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("watch", "once", "status", "configure-token"))
    parser.add_argument("--directory", type=Path, default=ROOT / ".local/delivery")
    parser.add_argument("--kubeconfig", type=Path, default=ROOT / ".local/kubernetes/kubeconfig")
    parser.add_argument("--interval", type=int, default=15)
    local_binary = ROOT / ".local/kubernetes/bin/kubectl"
    parser.add_argument("--kubectl", default=str(local_binary) if local_binary.is_file() else shutil.which("kubectl"))
    args = parser.parse_args()
    if args.action == "configure-token":
        try:
            configure_token()
        except DeliveryError as error:
            raise SystemExit(str(error)) from None
        except OSError:
            raise SystemExit("Token was not saved. Check the read permissions and private credential file location.") from None
        except (EOFError, KeyboardInterrupt):
            raise SystemExit("Token configuration cancelled; the credential is unchanged.") from None
        return
    if args.action == "status":
        path = args.directory / "state.json"
        state = json.loads(path.read_text()) if path.exists() else {"status": "not-started"}
        print(json.dumps({**state, "worker_running": worker_running(args.directory)}, indent=2))
        return
    if not args.kubectl or not args.kubeconfig.is_file() or args.interval < 5:
        parser.error("Provide the private local kubeconfig, kubectl, and an interval of at least five seconds.")
    with worker_lock(args.directory):
        github = GitHub()
        try:
            controller = Controller(github, args.kubectl, args.kubeconfig.resolve(), args.directory)
            while True:
                state = controller.step()
                if args.action == "once":
                    if state["status"] in {"deployment-failed", "authentication-required"}:
                        raise SystemExit(1)
                    return
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("Delivery worker stopped; the running application is preserved.", flush=True)
        finally:
            github.close()


def configure_token():
    """Accept a hidden token locally, verify read access, then replace its private file atomically."""
    if not sys.stdin.isatty() or "OFFSCRIPT_GITHUB_TOKEN" in os.environ:
        raise DeliveryError("Use an interactive terminal and remove the fixed token environment override first.")
    token = token_value(getpass.getpass("GitHub Actions/Contents read token (hidden): "))
    github = GitHub(token)
    try:
        commit = github.head()
        run = github.latest_run(commit)
        if run and approved_run(run, commit):
            artifact = github.artifact(run)
            if artifact:
                payload = github.archive(artifact)
                if "sha256:" + hashlib.sha256(payload).hexdigest() != artifact["digest"]:
                    raise DeliveryError("The read-access probe returned a different artifact.")
    finally:
        github.close()
    path = token_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=".github-token-", delete=False) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), 0o600)
            stream.write(token + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
    print("GitHub read credential saved privately. A running worker will read the replacement automatically.")


if __name__ == "__main__":
    main()
