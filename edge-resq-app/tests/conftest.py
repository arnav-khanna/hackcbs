"""Make `import app...` work whatever this folder is called on disk."""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

if importlib.util.find_spec("app") is None:
    spec = importlib.util.spec_from_file_location(
        "app", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["app"] = module
    spec.loader.exec_module(module)
