from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_CONFIG = ROOT / "config" / "watchlists.yaml"


@pytest.fixture
def example_data() -> dict:
    return yaml.safe_load(EXAMPLE_CONFIG.read_text())


@pytest.fixture
def write_config(tmp_path):
    def _write(data) -> Path:
        path = tmp_path / "watchlists.yaml"
        path.write_text(data if isinstance(data, str) else yaml.safe_dump(data))
        return path

    return _write
