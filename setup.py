"""Copy the TUI source tree into the package at wheel-build time."""

import shutil
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py

_TUI_SOURCE_EXCLUDE = {"node_modules", "dist"}


class TuiSourceBuildPy(build_py):
    def run(self) -> None:
        super().run()
        source = Path(__file__).parent / "tui"
        dest = Path(self.build_lib) / "sukuna" / "_tui_source"
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(
            source,
            dest,
            ignore=lambda _directory, names: [
                name for name in names if name in _TUI_SOURCE_EXCLUDE
            ],
        )


setup(cmdclass={"build_py": TuiSourceBuildPy})
