from __future__ import annotations

from pathlib import Path

from alembic.config import Config

from alembic import command

ROOT = Path(__file__).resolve().parents[1]


def _alembic_config() -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", "postgresql+psycopg://avis:avis@localhost:5432/avis")
    return cfg


def test_migration_offline_sql_generation(capsys) -> None:
    cfg = _alembic_config()
    command.upgrade(cfg, "head", sql=True)
    captured = capsys.readouterr()
    sql = captured.out

    assert "CREATE TABLE ref_company" in sql
    assert "CREATE TABLE val_run" in sql
    assert "CREATE TABLE auth_user" in sql


def test_migration_has_downgrade() -> None:
    versions = sorted((ROOT / "alembic" / "versions").glob("*.py"))
    assert versions, "Expected at least one migration file"
    for version_file in versions:
        content = version_file.read_text(encoding="utf-8")
        assert "def upgrade()" in content
        assert "def downgrade()" in content
    head_content = versions[-1].read_text(encoding="utf-8")
    assert "down_revision" in head_content
