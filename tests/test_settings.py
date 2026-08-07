"""
Tests for config/settings.py (SE-8, PO-2).

Two properties matter here and neither is about convenience:

1. A missing SECRET_KEY must stop the process. The previous hardcoded value
   is in git history, so a silent fallback to any default would mean forgeable
   session cookies on a system that looks correctly configured.
2. RECOGNITION_THRESHOLD must still resolve to 58.0. Phase 1 made it
   configurable; it did not tune it. That value is calibrated against the LBPH
   distance scale in train_model.LBPH_PARAMS, and the two moving apart is
   exactly the failure recorded in tasks/lessons.md L2 - every face silently
   rejected, no error raised.
"""

import pytest
from pydantic import ValidationError

from config.settings import Settings, settings

# A .env exists in the working tree and pydantic-settings would read it,
# masking the behaviour under test. Point every construction at a file that
# does not exist so only explicit values apply.
NO_ENV_FILE = "tests-no-such-env-file"


def test_secret_key_is_required():
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=NO_ENV_FILE)

    assert "secret_key" in str(excinfo.value)


def test_secret_key_must_not_be_trivially_short():
    """A 16-character floor stops "changeme" style placeholders."""
    with pytest.raises(ValidationError):
        Settings(_env_file=NO_ENV_FILE, secret_key="tooshort")


def test_defaults_match_the_values_previously_hardcoded_in_source():
    config = Settings(_env_file=NO_ENV_FILE, secret_key="x" * 32)

    assert config.db_host == "127.0.0.1"
    assert config.db_user == "root"
    assert config.db_password == ""
    assert config.db_name == "attendancesystem_db"


def test_recognition_threshold_is_unchanged_at_58():
    """
    Guards lessons.md L2. If this test ever fails, the threshold was changed
    without the FAR/FRR calibration that justifies a different operating
    point - see tasks/todo.md Phase 6.
    """
    config = Settings(_env_file=NO_ENV_FILE, secret_key="x" * 32)

    assert config.recognition_threshold == 58.0
    assert settings.recognition_threshold == 58.0


def test_environment_variables_override_defaults(monkeypatch):
    monkeypatch.setenv("DB_HOST", "db.example.internal")
    monkeypatch.setenv("DB_PORT", "3307")
    monkeypatch.setenv("DB_NAME", "some_other_db")
    monkeypatch.setenv("SECRET_KEY", "y" * 32)

    config = Settings(_env_file=NO_ENV_FILE)

    assert config.db_host == "db.example.internal"
    assert config.db_port == 3307
    assert config.db_name == "some_other_db"


def test_db_kwargs_matches_the_mysql_connector_signature():
    config = Settings(_env_file=NO_ENV_FILE, secret_key="x" * 32)

    assert config.db_kwargs() == {
        "host": "127.0.0.1",
        "port": 3306,
        "user": "root",
        "password": "",
        "database": "attendancesystem_db",
    }


def test_trainer_and_labels_paths_are_derived_from_trainer_dir(tmp_path):
    config = Settings(
        _env_file=NO_ENV_FILE, secret_key="x" * 32, trainer_dir=tmp_path
    )

    assert config.trainer_file == tmp_path / "trainer.yml"
    assert config.labels_file == tmp_path / "labels.txt"


def test_invalid_log_level_is_rejected():
    with pytest.raises(ValidationError):
        Settings(_env_file=NO_ENV_FILE, secret_key="x" * 32, log_level="CHATTY")


def test_log_level_is_normalised_to_upper_case():
    config = Settings(_env_file=NO_ENV_FILE, secret_key="x" * 32, log_level="debug")

    assert config.log_level == "DEBUG"
