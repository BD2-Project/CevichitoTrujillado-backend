import ast
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SOURCES = sorted((ROOT / "src").rglob("*.py"))
FORBIDDEN_MODULES = ("engine", "soupdb", "soupdb_vector", "soupdb_inverted", "applications")
FORBIDDEN_NAMES = (
    "Catalog",
    "execute_sql",
    "OnDiskVectorFactory",
    "MultimediaProfile",
    "FileManager",
    "TransactionalSession",
    "ConnectionHandler",
)


def test_there_are_sources():
    assert len(SOURCES) > 10


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: str(p.relative_to(ROOT)))
def test_no_engine_imports_or_internals(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules = [node.module or ""]
        else:
            modules = []
        for module in modules:
            assert module.split(".")[0] not in FORBIDDEN_MODULES, f"{path}: import {module}"
        if isinstance(node, ast.Name | ast.Attribute):
            name = node.id if isinstance(node, ast.Name) else node.attr
            assert name not in FORBIDDEN_NAMES, f"{path}:{node.lineno}: {name}"
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in ("__import__", "import_module"), f"{path}: import dinámico"


def test_backend_image_does_not_contain_soupdb():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "engine" not in dockerfile and "SoupBD" not in dockerfile
    assert "COPY src" in dockerfile


def test_project_does_not_depend_on_soupdb_packages():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    names = pyproject["project"]["dependencies"] + pyproject["dependency-groups"]["dev"]
    assert not [name for name in names if "soupdb" in name.lower()]
