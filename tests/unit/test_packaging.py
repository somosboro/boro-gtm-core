"""Packaging metadata must stay valid.

A malformed ``pyproject.toml`` does not surface locally once the virtualenv
already holds an editable install — it only fails on a clean build. These
stdlib-only checks catch that class of defect in the normal test run.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"

#: PEP 621 keys that belong to [project] and nowhere else.
_PROJECT_KEYS = {
    "name", "version", "description", "readme", "requires-python",
    "keywords", "classifiers", "dependencies", "license", "authors",
    "maintainers", "urls", "scripts", "optional-dependencies",
}


@pytest.fixture(scope="module")
def pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def test_pyproject_parses(pyproject) -> None:
    assert pyproject["project"]["name"] == "boro-gtm-core"


def test_required_metadata_is_present(pyproject) -> None:
    project = pyproject["project"]
    for key in ("name", "version", "description", "readme", "requires-python"):
        assert key in project, f"[project] is missing {key!r}"
    assert project["requires-python"].startswith(">=3.12")


def test_optional_dependencies_contains_only_arrays(pyproject) -> None:
    """The exact regression: metadata keys landing under a dependency group.

    ``[project.optional-dependencies]`` maps extra names to lists of
    requirements. A string value there means a ``[project]`` key was indented
    into the wrong table, which breaks any clean build.
    """
    for extra, value in pyproject["project"].get("optional-dependencies", {}).items():
        assert isinstance(value, list), (
            f"optional-dependencies.{extra} must be an array of requirements, "
            f"got {type(value).__name__} — a [project] key is in the wrong table"
        )
        assert all(isinstance(item, str) for item in value)


def test_no_project_keys_leaked_into_other_tables(pyproject) -> None:
    for table in ("optional-dependencies", "urls", "scripts", "entry-points"):
        section = pyproject["project"].get(table)
        if isinstance(section, dict):
            stray = _PROJECT_KEYS & set(section)
            assert not stray, f"[project.{table}] contains [project] keys: {stray}"


def test_declared_readme_exists(pyproject) -> None:
    readme = PYPROJECT.parent / pyproject["project"]["readme"]
    assert readme.is_file(), f"readme {readme.name} declared but missing"
    assert readme.stat().st_size > 0


def test_package_discovery_points_at_the_real_source_root(pyproject) -> None:
    find = pyproject["tool"]["setuptools"]["packages"]["find"]
    assert find["where"] == ["packages"]
    assert (PYPROJECT.parent / "packages" / "boro_gtm" / "__init__.py").is_file()


def test_project_urls_are_https(pyproject) -> None:
    for name, url in pyproject["project"].get("urls", {}).items():
        assert url.startswith("https://"), f"{name} is not https"


def test_console_script_target_is_importable(pyproject) -> None:
    import importlib

    target = pyproject["project"]["scripts"]["gtm"]
    module_path, _, attribute = target.partition(":")
    module = importlib.import_module(module_path)
    assert hasattr(module, attribute), f"{target} is not resolvable"


def test_version_matches_the_package(pyproject) -> None:
    import boro_gtm

    assert pyproject["project"]["version"] == boro_gtm.__version__
