from html import escape
from html.parser import HTMLParser

import pytest


class Assets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script":
            self.scripts.append(attrs)
        self.links.extend(value for key, value in attrs.items() if key in {"href", "src"})


@pytest.mark.parametrize("path", ["/docs", "/redoc"])
def test_reference_is_readable_under_the_application_security_policy(client, path):
    response = client.get(path)
    assert response.status_code == 200
    assert "Offscript Budapest API" in response.text
    assert "script-src 'self'" in response.headers["content-security-policy"]
    schema = client.get("/openapi.json").json()
    assert path not in schema["paths"]
    for endpoint in schema["paths"]:
        assert escape(endpoint) in response.text
    assert "PlanInput" in response.text and "max_price" in response.text
    assets = Assets()
    assets.feed(response.text)
    assert not assets.scripts
    assert all(url.startswith(("/", "#")) for url in assets.links)
    for asset in assets.links:
        if asset.startswith("/static/"):
            assert client.get(asset).status_code == 200
