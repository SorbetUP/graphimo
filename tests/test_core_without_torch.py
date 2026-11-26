from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_graphimo_core_imports_and_projects_without_torch_or_addon(tmp_path: Path):
    project = tmp_path / "core-only.py"
    project.write_text("# Core-only project\n", encoding="utf-8")
    program = r"""
import importlib.abc
import sys
from pathlib import Path
from graphimo import GraphimoApplication
from graphimo.metablocks import GraphimoHostAPI, MetaBlockAddonManager

class BlockTorch(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "torch" or fullname.startswith("torch.") or fullname == "torchlens" or fullname.startswith("torchlens."):
            raise ModuleNotFoundError(fullname)
        return None

sys.meta_path.insert(0, BlockTorch())
project = Path(sys.argv[1])
host = GraphimoHostAPI("core-only", project)
manager = MetaBlockAddonManager(host, entry_points=list)
application = GraphimoApplication(host, manager)
assert application.execute("metablock.list", {})["items"] == []
assert "torch" not in sys.modules and "torchlens" not in sys.modules
print("core-only-ok")
"""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT), str(ROOT / "core"), environment.get("PYTHONPATH", "")]
    )
    result = subprocess.run(
        [sys.executable, "-c", program, str(project)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, f"core-only import failed:\n{result.stdout}\n{result.stderr}"
    assert "core-only-ok" in result.stdout
