import subprocess
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class BuildHook(BuildHookInterface):
    PLUGIN_NAME = "incidentlens-web-build"

    def initialize(self, version: str, build_data: dict) -> None:
        root = Path(__file__).parents[1]
        # Wheel builds run from an isolated sdist where the JavaScript
        # toolchain is intentionally absent.  The sdist already contains the
        # verified Vite output, so keep that artifact instead of attempting
        # an impossible npm install/build in the isolated environment.
        if not (root / "node_modules/.bin/tsc").exists():
            static_root = root / "apps/control-plane/src/incidentlens_control_plane/static/web"
            if any(static_root.rglob("*")):
                return
            raise RuntimeError("web assets are missing and the JavaScript toolchain is unavailable")
        subprocess.run(["npm", "run", "web:build"], cwd=root, check=True)
