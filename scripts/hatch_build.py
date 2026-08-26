from hatchling.builders.hooks.plugin.interface import BuildHookInterface
from pathlib import Path
import subprocess


class BuildHook(BuildHookInterface):
    PLUGIN_NAME = "incidentlens-web-build"

    def initialize(self, version: str, build_data: dict) -> None:
        root = Path(__file__).parents[1]
        subprocess.run(["npm", "run", "web:build"], cwd=root, check=True)
