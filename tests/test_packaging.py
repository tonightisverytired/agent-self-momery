# -*- coding: utf-8 -*-
"""0.8.0 A-01-T 打包与包骨架验收。"""
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_import_server_package():
    import dnamemory
    import server
    assert dnamemory.__version__ == "0.8.2"
    assert hasattr(server, "__file__") or hasattr(server, "__path__")


def test_pyproject_version_and_scripts():
    data = tomllib.loads(
        (ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["version"] == "0.8.2"
    scripts = data["project"]["scripts"]
    assert scripts["dnamemory"] == "dnamemory.cli:main"
    assert scripts["dnamemory-server"] == "server.main:main"
    assert scripts["dnamemory-mcp"] == "server.mcp:main"


def test_pyproject_package_dir_covers_server():
    data = tomllib.loads(
        (ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    pkg_dir = data["tool"]["setuptools"]["package-dir"]
    assert pkg_dir == {"": "src", "server": "server"}
    # 显式 packages 清单（find 多 where 不可靠，A-01 决策）
    pkgs = data["tool"]["setuptools"]["packages"]
    assert "dnamemory" in pkgs and "server" in pkgs
    assert "server.api" in pkgs and "server.web" in pkgs
    pdata = data["tool"]["setuptools"].get("package-data", {})
    assert "server.web" in pdata
    assert any("html" in f or "js" in f or "css" in f
               for f in pdata["server.web"])


def test_cli_version_080():
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get(
        "PYTHONPATH", "")
    out = subprocess.run(
        [sys.executable, "-m", "dnamemory.cli", "--version"],
        capture_output=True, text=True, env=env, cwd=str(ROOT))
    assert out.returncode == 0
    assert "0.8.2" in (out.stdout + out.stderr)
