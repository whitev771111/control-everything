"""Include only the HTTP allowlist from the single web source tree."""

from pathlib import Path, PurePosixPath
import shutil

from setuptools.command.build_py import build_py


WEB_ASSETS = (
    "sessions/index.html", "sessions/app.js", "sessions/style.css",
    "progress/progress.html", "progress/progress.js", "progress/progress.css",
    "workspace/workspace.html", "workspace/workspace.js", "workspace/workspace.css",
    "workspace/task-center.js", "workspace/execution.js", "workspace/observation.js",
    "workspace/news.js", "workspace/quota.js",
)


class BuildPy(build_py):
    def asset_mapping(self):
        root = Path(__file__).resolve().parents[2]
        result = {}
        for relative in WEB_ASSETS:
            path = PurePosixPath(relative)
            if (path.is_absolute() or '..' in path.parts or path.as_posix() != relative
                    or '\\' in relative or ':' in relative or path.suffix not in {'.html', '.css', '.js'}):
                raise ValueError("invalid_web_asset")
            source = root / "web" / relative
            cursor = source
            while cursor != root:
                if cursor.is_symlink() or cursor.is_junction():
                    raise ValueError("linked_web_asset")
                cursor = cursor.parent
            if not source.is_file():
                raise ValueError("missing_web_asset: " + relative)
            destination = Path(self.build_lib) / "session_panel" / "_assets" / "web" / relative
            result[str(destination)] = str(source)
        return result

    def get_source_files(self):
        root = Path(__file__).resolve().parents[2]
        sources = [Path(source).relative_to(root).as_posix() for source in self.asset_mapping().values()]
        return list(dict.fromkeys(super().get_source_files() + sources))

    def get_outputs(self, include_bytecode=1):
        return list(dict.fromkeys(super().get_outputs(include_bytecode) + list(self.asset_mapping())))

    def get_output_mapping(self):
        return {**super().get_output_mapping(), **self.asset_mapping()}

    def run(self):
        mapping = self.asset_mapping()
        super().run()
        if self.editable_mode:
            return
        for destination, source in mapping.items():
            target = Path(destination)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            if target.read_bytes() != Path(source).read_bytes():
                raise ValueError("web_asset_copy_mismatch")
