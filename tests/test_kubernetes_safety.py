import base64
import json

import pytest

from scripts.kubernetes_local import ensure_credentials, require_local_server
from scripts.prepare_local import prepare


@pytest.mark.parametrize("server", ["https://cluster.example.com:6443", "https://127.0.0.1.example.com", "http://127.0.0.1:6443"])
def test_local_setup_rejects_remote_or_unsecured_targets(server):
    with pytest.raises(RuntimeError, match="dedicated loopback"):
        require_local_server(server)


def test_existing_secret_mismatch_cannot_overwrite_database_credentials(tmp_path, capsys):
    class Existing:
        def run(self, *args, **kwargs):
            return json.dumps({"data": {"db_password": "existing-private-value"}, "immutable": True})

        def apply(self, *args, **kwargs):
            pytest.fail("An existing credential must never be overwritten")

    with pytest.raises(RuntimeError, match="original credentials"):
        ensure_credentials(Existing(), tmp_path)
    assert "existing-private-value" not in capsys.readouterr().out


def test_matching_credentials_are_reused_without_changes(tmp_path):
    prepare(tmp_path)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}

    class Existing:
        def run(self, *args, **kwargs):
            data = {key: base64.b64encode(before[name]).decode() for key, name in (("db_password", "db-password"), ("db_admin_password", "admin-password"))}
            return json.dumps({"data": data, "immutable": True})

        def apply(self, *args, **kwargs):
            pytest.fail("Matching credentials need no update")

    ensure_credentials(Existing(), tmp_path)
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before
