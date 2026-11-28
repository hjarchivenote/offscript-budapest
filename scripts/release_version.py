"""Identify a new application release from the validated main commit's parent."""

import ast
import re
import subprocess

from app import __version__

VERSION = r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"


def source_version(source):
    assignments = [node for node in ast.parse(source).body
                   if isinstance(node, ast.Assign)
                   and any(isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets)]
    if (len(assignments) != 1 or not isinstance(assignments[0].value, ast.Constant)
            or not isinstance(assignments[0].value.value, str)
            or not re.fullmatch(VERSION, assignments[0].value.value)):
        raise ValueError("Application versions must be one literal major.minor.patch value.")
    return assignments[0].value.value


def new_release_tag(commit):
    if not re.fullmatch(r"[a-f0-9]{40}", commit) or not re.fullmatch(VERSION, __version__):
        raise ValueError("A full source commit and semantic application version are required.")

    def git(*args):
        result = subprocess.run(["git", *args], capture_output=True, text=True, timeout=20, check=False)
        if result.returncode:
            raise RuntimeError("Release detection requires the validated commit and its first parent (checkout fetch-depth: 2).")
        return result.stdout.strip()

    if git("rev-parse", "HEAD") != commit:
        raise ValueError("The release checkout differs from the validated source commit.")
    previous = source_version(git("show", commit + "^:app/__init__.py"))
    if previous == __version__:
        return None
    if tuple(map(int, __version__.split("."))) <= tuple(map(int, previous.split("."))):
        raise ValueError("A new release must increase the application version.")
    return "v" + __version__
