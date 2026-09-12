"""Component ownership shared by capture, verification and scheduling."""
from etf_registry import UNIVERSE_EUROPE_SECTORS, EUROPE_SUPERSECTORS_CANDIDATE

COMPONENTS = ("all", "core", "europe")


def engine_in_scope(script, component):
    if component not in COMPONENTS:
        raise ValueError(f"unknown component: {component}")
    european = script == "scripts/run_europe_rotation.py"
    return component == "all" or european == (component == "europe")


def select_panels(panels, component):
    if component not in COMPONENTS:
        raise ValueError(f"unknown component: {component}")
    european = set(UNIVERSE_EUROPE_SECTORS) | set(EUROPE_SUPERSECTORS_CANDIDATE)
    return [e for e in panels if component == "all"
            or ((e in european) == (component == "europe"))]
