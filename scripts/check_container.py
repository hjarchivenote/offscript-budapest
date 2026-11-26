"""Exercise the built image against a disposable, isolated PostgreSQL volume.

Run after building the image: .venv/bin/python -m scripts.check_container
Only this check's randomly named Compose project is stopped or removed.
"""

import json
import os
import socket
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import httpx

from app import __version__
from scripts.prepare_local import prepare

ROOT = Path(__file__).resolve().parents[1]


def main():
    prepare(ROOT / ".local" / "container-secrets")
    project = "offscript-check-" + uuid.uuid4().hex[:8]
    with socket.socket() as available:
        available.bind(("127.0.0.1", 0))
        port = available.getsockname()[1]
    environment = {**os.environ, "APP_PORT": str(port)}

    def docker(*args, check=True):
        result = subprocess.run(
            ["docker", *args], cwd=ROOT, env=environment,
            text=True, capture_output=True, timeout=180, check=False,
        )
        if check and result.returncode:
            raise RuntimeError(f"Docker check failed: {result.stderr.strip()}")
        return result.stdout.strip()

    def compose(*args):
        return docker("compose", "--project-name", project, *args)

    def sql(query):
        return compose("exec", "-T", "db", "psql", "-U", "postgres", "-d", "offscript", "-Atc", query)

    def inspect(service):
        ids = compose("ps", "--quiet", service).splitlines()
        return next(c for c in json.loads(docker("inspect", *ids)) if c["Config"]["Labels"].get("com.docker.compose.oneoff", "False").lower() == "false")

    def wait_for(predicate, timeout=50):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.5)
        raise AssertionError("The expected container state did not arrive in time.")

    def passed(message):
        print("PASS " + message, flush=True)

    peer_name = project + "-peer"
    try:
        compose("up", "--detach", "--wait", "--wait-timeout", "90", "db")
        bootstrap_check = '''import concurrent.futures,json,threading
from sqlalchemy import inspect
from app.database import make_database
from app.bootstrap import initialize_database,require_initialized_database
engine,_=make_database()
assert inspect(engine).get_table_names()==[]
try:require_initialized_database(engine)
except RuntimeError:pass
else:raise AssertionError('Uninitialized replica was accepted')
assert inspect(engine).get_table_names()==[]
engine.dispose()
barrier=threading.Barrier(2)
def initialize(_):
 engine,_=make_database()
 try:
  barrier.wait(timeout=10)
  return initialize_database(engine)
 finally:engine.dispose()
with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
 assert sorted(pool.map(initialize,range(2)))==[False,True]
print('ok')
'''
        assert compose("run", "--rm", "--no-deps", "-T", "app", "python", "-c", bootstrap_check) == "ok"
        passed("concurrent initialization of an empty PostgreSQL database commits once; uninitialized replicas fail without writes")
        compose("up", "--detach", "--no-build", "--wait", "--wait-timeout", "120")
        root = f"http://127.0.0.1:{port}"
        with httpx.Client(base_url=root, timeout=15) as host, httpx.Client(base_url=root, timeout=15) as friend:
            assert host.get("/health").json() == {"status": "ok"}
            assert host.get("/ready").json() == {"status": "ready"}
            version = host.get("/version").json()
            assert version["version"] == __version__
            if os.environ.get("CI_EXPECTED_COMMIT"):
                assert version["commit"] == os.environ["CI_EXPECTED_COMMIT"]
            config = host.get("/api/config").json()
            assert config["catalogue_stats"] == {
                "places": 152, "activities": 172, "basic_places": 72, "enriched_places": 28,
            }
            passed("fresh PostgreSQL initialization, catalogue, health and version endpoints")

            app = inspect("app")
            expected_image = os.environ.get("OFFSCRIPT_IMAGE", "offscript:0.1.0-local")
            expected_id = json.loads(docker("image", "inspect", expected_image))[0]["Id"]
            assert app["Image"] == expected_id
            assert app["Config"]["User"] == "10001:10001"
            assert app["HostConfig"]["ReadonlyRootfs"]
            assert "ALL" in app["HostConfig"]["CapDrop"]
            assert "no-new-privileges:true" in app["HostConfig"]["SecurityOpt"]
            assert all(not value for value in inspect("db")["NetworkSettings"]["Ports"].values())
            assert sql("SELECT rolsuper,rolcreatedb,rolcreaterole,rolreplication FROM pg_roles WHERE rolname='offscript'") == "f|f|f|f"
            assert compose("exec", "-T", "app", "python", "-c",
                "import importlib.util,os,pathlib; assert os.geteuid()==10001; "
                "assert all(not pathlib.Path(p).exists() for p in ['/app/.local','/app/.git','/app/tests','/run/secrets/db_admin_password']); "
                "assert all(importlib.util.find_spec(p) is None for p in ['pytest','httpx','ruff']); print('ok')") == "ok"
            passed("non-root read-only application, separate credentials, limited database role and no published DB port")

            if "CI_CONTEXT_PROBES" in os.environ:
                probes = json.loads(os.environ["CI_CONTEXT_PROBES"])
                assert isinstance(probes, list) and probes
                assert compose("exec", "-T", "app", "python", "-c",
                    "import json,pathlib,sys; assert all(not pathlib.Path(p).exists() for p in json.loads(sys.argv[1])); print('ok')",
                    json.dumps(probes)) == "ok"
                passed("private SQLite, environment and development-directory sentinels are excluded from the actual image")

            activity = "overture-64ec40bf-c520-4656-9152-5f78d059c904-place"
            excluded = host.get("/api/activities", params={"when": "anytime", "q": "De Lanna", "max_price": 10000}).json()
            included = host.get("/api/activities", params={"when": "anytime", "q": "De Lanna", "max_price": 20000, "max_minutes": 60}).json()
            assert not excluded["items"]
            info = host.get(f"/api/activities/{activity}").json()
            if info["provider_freshness"]["price"]["state"] == "current":
                assert [a["id"] for a in included["items"]] == [activity]
            else:
                assert not included["items"] and info["price_high"] is None
            passed("budget and activity-duration filters against PostgreSQL")

            created = host.post("/api/plans", json={"title": "Container integration check", "nickname": "Host QA", "day": config["today"], "use_today": True})
            assert created.status_code == 201
            plan = created.json()
            path = f"/api/plans/{plan['id']}"
            assert host.post(path + "/candidates", json={"activity_id": activity}).status_code == 201
            assert friend.post(path + "/join", json={"nickname": "Friend QA"}).status_code == 200
            candidate = friend.get(path).json()["candidates"][0]["id"]
            vote_path = f"{path}/candidates/{candidate}/vote"
            assert friend.put(vote_path, json={"choice": "yes"}).status_code == 200
            assert friend.put(vote_path, json={"choice": "maybe"}).json()["candidates"][0]["votes"] == {"yes": 0, "maybe": 1, "no": 0}
            review_path = f"/api/activities/{activity}/reviews"
            assert host.post(review_path, json={"nickname": "QA", "rating": 4, "visited_on": config["today"], "text": "Isolated automated container test, not a real venue review."}).status_code == 200
            passed("guest invitation, proposal, changed vote and review storage")

            with socket.socket() as available:
                available.bind(("127.0.0.1", 0))
                peer_port = available.getsockname()[1]
            compose("run", "--detach", "--rm", "--no-deps", "--name", peer_name,
                "--publish", f"127.0.0.1:{peer_port}:8000", "app")
            with httpx.Client(base_url=f"http://127.0.0.1:{peer_port}", timeout=15) as peer:
                peer.cookies.update(dict(host.cookies))

                def peer_ready():
                    try:
                        return peer.get("/ready").status_code == 200
                    except httpx.TransportError:
                        return False

                wait_for(peer_ready)
                concurrent = host.post("/api/plans", json={"title": "Two-replica final choice", "nickname": "Host QA", "day": config["today"], "use_today": True}).json()
                concurrent_path = f"/api/plans/{concurrent['id']}"
                for choice in (activity, "mecca-day"):
                    assert host.post(concurrent_path + "/candidates", json={"activity_id": choice}).status_code == 201
                choices = [c["id"] for c in host.get(concurrent_path).json()["candidates"]]
                barrier = Barrier(2)

                def confirm(client, choice):
                    barrier.wait(timeout=5)
                    return client.post(concurrent_path + "/pick", json={"candidate_id": choice})

                with ThreadPoolExecutor(max_workers=2) as pool:
                    pending = [pool.submit(confirm, client, choice) for client, choice in zip((host, peer), choices, strict=True)]
                    responses = [future.result(timeout=20) for future in pending]
                assert sorted(r.status_code for r in responses) == [200, 409]
                winner = next(r.json()["selected_candidate"] for r in responses if r.status_code == 200)
                assert peer.get(concurrent_path).json()["selected_candidate"] == winner
                assert host.get(concurrent_path).json()["selected_candidate"] == winner
            docker("rm", "--force", peer_name)
            passed("two real application containers start together and permit only one final choice across replicas")

            if os.environ.get("CI_BROWSER_TEST") == "1":
                subprocess.run(["node", "tests/browser/smoke.cjs"], cwd=ROOT, check=True, timeout=120,
                    env={**os.environ, "OFFSCRIPT_BASE_URL": root})
                passed("real browser UI, friend proposals, voting and mobile layout")

            compose("stop", "db")
            assert host.get("/health").status_code == 200
            assert host.get("/ready").status_code == 503
            unavailable = host.get("/api/activities")
            assert unavailable.status_code == 503
            assert "temporarily unavailable" in unavailable.json()["detail"]
            assert unavailable.headers["cache-control"] == "no-store"
            wait_for(lambda: inspect("app")["State"]["Health"]["Status"] == "unhealthy")
            passed("database outage changes readiness and Docker health while the app remains alive")
            compose("up", "--detach", "--wait", "--wait-timeout", "90", "db")
            wait_for(lambda: host.get("/ready").status_code == 200)
            assert host.get("/api/activities").status_code == 200
            wait_for(lambda: inspect("app")["State"]["Health"]["Status"] == "healthy")
            passed("database reconnection and automatic health recovery")

            previous_id = inspect("app")["Id"]
            compose("down")  # Keep the database volume; recreate both containers.
            compose("up", "--detach", "--no-build", "--wait", "--wait-timeout", "120")
            assert inspect("app")["Id"] != previous_id
            restored = friend.get(path).json()
            assert restored["me"]["nickname"] == "Friend QA"
            assert restored["participants"] == ["Host QA", "Friend QA"]
            assert restored["candidates"][0]["activity"]["id"] == activity
            assert restored["candidates"][0]["my_vote"] == "maybe"
            assert restored["candidates"][0]["votes"] == {"yes": 0, "maybe": 1, "no": 0}
            assert len(host.get(review_path).json()) == 1
            assert host.get("/api/config").json()["catalogue_stats"] == config["catalogue_stats"]
            assert host.post(path + "/pick", json={"candidate_id": candidate}).status_code == 200
            assert friend.put(vote_path, json={"choice": "yes"}).status_code == 409
            passed("data survives container recreation; existing guest cookies and finalized-plan rules remain valid")
    finally:
        docker("rm", "--force", peer_name, check=False)
        compose("down", "--volumes", "--remove-orphans")
        passed("only the disposable check project and its test database were removed")


if __name__ == "__main__":
    main()
