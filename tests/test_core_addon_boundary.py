from __future__ import annotations

from pathlib import Path


def test_graphimo_core_does_not_import_or_depend_on_torchlens() -> None:
    root = Path(__file__).resolve().parents[1]
    core_sources = (root / "core" / "graphimo_core").rglob("*.py")

    assert all("torchlens" not in path.read_text(encoding="utf-8").lower() for path in core_sources)
    project_config = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert "inspection = [\"torchlens\"]" not in project_config
