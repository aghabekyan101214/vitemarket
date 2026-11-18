"""Migrations apply and revert cleanly, and the models match the migrated schema exactly."""

from tests.conftest import run_alembic


def test_downgrade_to_base_and_upgrade_again() -> None:
    run_alembic("downgrade", "base")
    run_alembic("upgrade", "head")


def test_models_match_migrations() -> None:
    # `alembic check` fails if autogenerate would emit any operation (model/migration drift).
    result = run_alembic("check")
    assert "No new upgrade operations detected" in result.stdout + result.stderr
