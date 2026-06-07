"""Tests for trading profile loading."""

from pathlib import Path

import pytest

from polytempo.profiles.load import generate_all_twelve_profiles, load_paper_profiles


def test_generate_all_twelve_profiles_count() -> None:
    profiles = generate_all_twelve_profiles(
        lead_gates={
            "lead30": {"target_lead_hours": 30},
            "lead24": {"target_lead_hours": 24},
        }
    )
    assert len(profiles) == 12
    ids = {p.id for p in profiles}
    assert "bh_dist_arb_lead30" in ids
    assert "es_mid_band_lead24" in ids


def test_load_paper_profiles_from_repo_config() -> None:
    path = Path("config/paper_profiles.yaml")
    if not path.is_file():
        pytest.skip("config/paper_profiles.yaml missing")
    profiles = load_paper_profiles(path)
    assert len(profiles) == 12
