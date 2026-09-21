# -*- coding: utf-8 -*-
"""项目配置加载（.env 风格，零依赖）测试。

覆盖：解析规则 / 优先级（环境变量压制文件）/ 白名单 / 查找顺序 /
strict 语义 / 端到端（配置文件 → CLI → 实际库路径）。
"""
import os

import pytest

from dnamemory.settings import (apply_config, find_config_file, load_config,
                                parse_env_text, preview)


@pytest.fixture(autouse=True)
def _isolate_environ():
    """apply_config 直接写 os.environ（不经 monkeypatch），测后需整体还原。"""
    before = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(before)


@pytest.fixture(autouse=True)
def _isolate_user_config(tmp_path, monkeypatch):
    """避免真实 ~/.dnamemory.env 干扰查找顺序断言。"""
    monkeypatch.setattr("dnamemory.settings.USER_CONFIG",
                        tmp_path / "no_such_user_config.env")


# ---------------- 解析 ----------------

def test_parse_basic_rules():
    text = (
        "# 整行注释\n"
        "\n"
        "DNAMEMORY_HOST=0.0.0.0\n"
        "   DNAMEMORY_PORT=9000   \n"
        "export DNAMEMORY_TOKEN=abc123\n"
        "DNAMEMORY_DB=\"my db.db\"\n"
        "DEEPSEEK_MODEL='deepseek-v4-pro'\n"
        "DNAMEMORY_CORS_ORIGINS=https://a.com,https://b.com  # 行尾注释\n"
    )
    cfg = parse_env_text(text)
    assert cfg["DNAMEMORY_HOST"] == "0.0.0.0"
    assert cfg["DNAMEMORY_PORT"] == "9000"
    assert cfg["DNAMEMORY_TOKEN"] == "abc123"
    assert cfg["DNAMEMORY_DB"] == "my db.db"
    assert cfg["DEEPSEEK_MODEL"] == "deepseek-v4-pro"
    assert cfg["DNAMEMORY_CORS_ORIGINS"] == "https://a.com,https://b.com"


def test_parse_quoted_value_keeps_hash():
    assert parse_env_text('K="a#b"  # 注释\n')["K"] == "a#b"
    assert parse_env_text("K='x # y'\n")["K"] == "x # y"


def test_parse_skips_malformed_lines():
    cfg = parse_env_text("没有等号\n=没有键\nBAD KEY=1\nGOOD=1\n")
    assert cfg == {"GOOD": "1"}


def test_parse_tolerates_bom(tmp_path):
    f = tmp_path / "bom.env"
    f.write_text("DNAMEMORY_PORT=8123\n", encoding="utf-8-sig")
    cfg, _ = load_config(str(f))
    assert cfg["DNAMEMORY_PORT"] == "8123"


# ---------------- 优先级与白名单 ----------------

def test_env_overrides_file(tmp_path, monkeypatch):
    f = tmp_path / "dnamemory.env"
    f.write_text("DNAMEMORY_TOKEN=from-file\nDNAMEMORY_HOST=0.0.0.0\n",
                 encoding="utf-8")
    monkeypatch.setenv("DNAMEMORY_TOKEN", "from-env")
    monkeypatch.delenv("DNAMEMORY_HOST", raising=False)

    applied = apply_config(str(f))
    assert applied == {"DNAMEMORY_HOST": "0.0.0.0"}        # token 被压制
    assert os.environ["DNAMEMORY_TOKEN"] == "from-env"     # 环境变量优先
    assert os.environ["DNAMEMORY_HOST"] == "0.0.0.0"


def test_override_true_forces_file(tmp_path, monkeypatch):
    f = tmp_path / "dnamemory.env"
    f.write_text("DNAMEMORY_TOKEN=from-file\n", encoding="utf-8")
    monkeypatch.setenv("DNAMEMORY_TOKEN", "from-env")
    apply_config(str(f), override=True)
    assert os.environ["DNAMEMORY_TOKEN"] == "from-file"


def test_non_whitelisted_keys_ignored(tmp_path, monkeypatch):
    f = tmp_path / "dnamemory.env"
    f.write_text("PATH=/evil\nPYTHONPATH=/evil\nDNAMEMORY_PORT=9999\n",
                 encoding="utf-8")
    original_path = os.environ.get("PATH", "")
    applied = apply_config(str(f))
    assert "PATH" not in applied and "PYTHONPATH" not in applied
    assert os.environ.get("PATH", "") == original_path
    assert os.environ["DNAMEMORY_PORT"] == "9999"


# ---------------- 查找顺序与 strict ----------------

def test_find_order_explicit_then_env_then_cwd(tmp_path, monkeypatch):
    explicit = tmp_path / "explicit.env"
    explicit.write_text("", encoding="utf-8")
    assert find_config_file(str(explicit)) == explicit

    env_file = tmp_path / "from_env.env"
    env_file.write_text("", encoding="utf-8")
    monkeypatch.setenv("DNAMEMORY_CONFIG", str(env_file))
    assert find_config_file() == env_file

    monkeypatch.delenv("DNAMEMORY_CONFIG")
    monkeypatch.chdir(tmp_path)
    cwd_file = tmp_path / "dnamemory.env"
    cwd_file.write_text("", encoding="utf-8")
    assert find_config_file() == cwd_file


def test_no_config_anywhere_returns_empty(tmp_path, monkeypatch):
    monkeypatch.delenv("DNAMEMORY_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    assert find_config_file() is None
    assert apply_config() == {}
    assert load_config() == ({}, None)


def test_strict_raises_on_missing_explicit(tmp_path):
    with pytest.raises(FileNotFoundError):
        apply_config(str(tmp_path / "不存在.env"), strict=True)
    assert apply_config(str(tmp_path / "不存在.env")) == {}   # 非 strict 静默


# ---------------- preview ----------------

def test_preview_classifies_keys(tmp_path, monkeypatch):
    f = tmp_path / "dnamemory.env"
    f.write_text("DNAMEMORY_TOKEN=t\nDNAMEMORY_PORT=8\nPATH=/x\n",
                 encoding="utf-8")
    monkeypatch.setenv("DNAMEMORY_TOKEN", "existing")
    monkeypatch.delenv("DNAMEMORY_PORT", raising=False)

    p = preview(str(f))
    assert p["applied"] == ["DNAMEMORY_PORT"]
    assert p["shadowed"] == ["DNAMEMORY_TOKEN"]
    assert p["ignored"] == ["PATH"]
    assert p["file"] == str(f)


# ---------------- 端到端 ----------------

def test_cli_reads_db_path_from_config(tmp_path, monkeypatch):
    """配置文件里的 DNAMEMORY_DB 决定 CLI 的实际库路径。"""
    from dnamemory import MemorySystem
    from dnamemory.cli import main

    db = tmp_path / "from_config.db"
    cfg = tmp_path / "my.env"
    cfg.write_text(f"DNAMEMORY_DB={db}\n", encoding="utf-8")
    monkeypatch.delenv("DNAMEMORY_DB", raising=False)
    monkeypatch.delenv("DNAMEMORY_DSN", raising=False)

    rc = main(["add-entity", "--name", "配置测试", "--config", str(cfg)])
    assert rc == 0
    assert db.exists(), "库应建在配置文件指定的路径"

    mem = MemorySystem(path=str(db))
    try:
        assert [n.name for n in mem.store.fetch_nodes()] == ["配置测试"]
    finally:
        mem.close()


def test_cli_cli_arg_beats_config(tmp_path, monkeypatch):
    """命令行 --db 优先于配置文件。"""
    from dnamemory.cli import main

    cfg_db = tmp_path / "from_config.db"
    cli_db = tmp_path / "from_cli.db"
    cfg = tmp_path / "my.env"
    cfg.write_text(f"DNAMEMORY_DB={cfg_db}\n", encoding="utf-8")
    monkeypatch.delenv("DNAMEMORY_DB", raising=False)

    rc = main(["add-entity", "--name", "命令行优先",
               "--config", str(cfg), "--db", str(cli_db)])
    assert rc == 0
    assert cli_db.exists() and not cfg_db.exists()
