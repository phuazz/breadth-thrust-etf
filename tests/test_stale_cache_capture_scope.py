"""A stale-cache refusal fails --strict-capture only inside its own component.

2026-09-26: the Saturday core pass failed its pipeline step because five
deployed Europe panels (EXH1, EXH3, EXH9, EXV1, EXV3) had price caches four
sessions old. Core does not refresh Europe caches, so the failure was one core
could never clear by itself.
"""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import pipeline
from component_scope import select_panels
from refresh_all import ETFS_ALL

EUROPE = select_panels(ETFS_ALL, "europe")
CORE = select_panels(ETFS_ALL, "core")


def test_fixture_has_both_components():
    assert EUROPE and CORE
    assert "EXV1" in EUROPE and "CSP1" in CORE


def test_core_run_ignores_stale_europe_panels():
    assert not pipeline.stale_cache_fails_capture(EUROPE, "core")


def test_core_run_fails_on_stale_core_panel():
    assert pipeline.stale_cache_fails_capture(EUROPE + [CORE[0]], "core")


def test_europe_run_fails_on_stale_europe_panel():
    assert pipeline.stale_cache_fails_capture([EUROPE[0]], "europe")


def test_europe_run_ignores_stale_core_panel():
    assert not pipeline.stale_cache_fails_capture([CORE[0]], "europe")


def test_full_run_fails_on_either():
    assert pipeline.stale_cache_fails_capture([EUROPE[0]], "all")
    assert pipeline.stale_cache_fails_capture([CORE[0]], "all")


@pytest.mark.parametrize("component", ["all", "core", "europe"])
def test_undeployed_panel_never_fails(component):
    assert not pipeline.stale_cache_fails_capture(["NOT_A_PANEL"], component)


@pytest.mark.parametrize("component", ["all", "core", "europe"])
def test_unnamed_refusal_still_fails(component):
    assert pipeline.stale_cache_fails_capture([], component)


def test_component_read_from_environment(monkeypatch):
    monkeypatch.setenv("BTE_COMPONENT_REFRESH", "core")
    assert not pipeline.stale_cache_fails_capture(EUROPE)
    monkeypatch.delenv("BTE_COMPONENT_REFRESH")
    assert pipeline.stale_cache_fails_capture(EUROPE)
