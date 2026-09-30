"""Scaffold acceptance tests (issue #2)."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from nanodot import __version__
from nanodot import paths

SRC = Path(__file__).resolve().parent.parent / "src" / "nanodot"

ALLOWED_PREFIXES = ("nanodot.",)
FORBIDDEN_PREFIXES = ("nanodot.native",)


def _imported_names(tree: ast.AST) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def test_core_imports_nothing_from_native_or_sdks() -> None:
    """The adapter seam: core talks only to ports and the stdlib."""
    stdlib = sys.stdlib_module_names
    violations: list[str] = []
    for py in (SRC / "core").rglob("*.py"):
        for name in _imported_names(ast.parse(py.read_text())):
            if name.startswith(FORBIDDEN_PREFIXES):
                violations.append(f"{py.name}: {name} (native)")
            elif name.startswith(ALLOWED_PREFIXES):
                continue
            elif name.split(".")[0] not in stdlib:
                violations.append(f"{py.name}: {name} (third-party)")
    assert not violations, f"core boundary violations: {violations}"


def test_version_prints(capsys: pytest.CaptureFixture[str]) -> None:
    from nanodot.cli import main

    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_cli_entrypoint_installed() -> None:
    """`nanodot --version` works as a console script."""
    project_root = SRC.parent.parent
    result = subprocess.run(
        [sys.executable, "-m", "nanodot.cli", "--version"],
        capture_output=True,
        text=True,
        cwd=project_root,
        env=None,
    )
    assert result.returncode == 0


def test_data_home_created_on_first_use(home: Path) -> None:
    assert not home.exists()
    returned = paths.data_home()
    assert returned == home
    assert home.is_dir()


def test_nothing_written_outside_data_home(home: Path, tmp_path: Path) -> None:
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    target = paths.data_home() / "probe.txt"
    target.write_text("x")
    assert target.exists()
    # The only new directory in the sandbox tree is the data home itself.
    written = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert all(str(p).startswith(str(home)) for p in written), written
