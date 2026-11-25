"""Render the generated OpenAPI contract without scripts or external assets."""

import json
from html import escape
from pathlib import Path
from string import Template

TEMPLATE = Path(__file__).with_name("static") / "api-reference.html"
METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}


def schema_block(value):
    return "<pre><code>" + escape(json.dumps(value, indent=2, ensure_ascii=False)) + "</code></pre>"


def render_reference(schema):
    operations = []
    for path, methods in schema["paths"].items():
        for method, operation in methods.items():
            if method not in METHODS:
                continue
            inputs = {key: operation[key] for key in ("parameters", "requestBody") if key in operation}
            operations.append(
                '<details class="operation"><summary><span class="method">'
                + method.upper() + "</span><code>" + escape(path) + "</code></summary>"
                + "<h3>" + escape(operation.get("summary", "")) + "</h3>"
                + ("<p>" + escape(operation["description"]) + "</p>" if operation.get("description") else "")
                + ("<h4>Inputs</h4>" + schema_block(inputs) if inputs else "<p>No request inputs.</p>")
                + "<h4>Responses</h4>" + schema_block(operation.get("responses", {})) + "</details>"
            )
    models = [
        '<details class="model"><summary><code>' + escape(name) + "</code></summary>"
        + schema_block(value) + "</details>"
        for name, value in schema.get("components", {}).get("schemas", {}).items()
    ]
    return Template(TEMPLATE.read_text()).substitute(
        title=escape(schema["info"]["title"]), version=escape(schema["info"]["version"]),
        operations="\n".join(operations), models="\n".join(models),
    )
