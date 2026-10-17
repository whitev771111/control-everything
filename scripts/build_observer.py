"""Build the observer with explicitly prepared tools; never install a toolchain."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ("cargo", "rustc", "rustdoc", "rustfmt", "cargo-fmt", "cargo-clippy", "clippy-driver")
TOOLCHAIN = "1.97.1-x86_64-pc-windows-msvc"


def prepared_environment():
    environment = os.environ.copy()
    local = ROOT / "work" / "native-observer"
    rustup_home = Path(environment.get("RUSTUP_HOME") or local / "rustup-home").resolve()
    environment.update(RUSTUP_HOME=str(rustup_home),
                       CARGO_HOME=str(local / "cargo-home"),
                       CARGO_TARGET_DIR=str(local / "target"), RUSTUP_AUTO_INSTALL="0")
    rustup = shutil.which("rustup")
    if not rustup:
        raise RuntimeError("rustup_missing")
    tools = {}
    for name in TOOLS:
        result = subprocess.run([rustup, "which", "--toolchain", TOOLCHAIN, name],
                                env=environment, capture_output=True, text=True, encoding="utf-8")
        if result.returncode:
            raise RuntimeError(f"prepared_component_missing:{name}")
        path = Path(result.stdout.strip()).resolve()
        if not path.is_file() or not path.is_relative_to(rustup_home):
            raise RuntimeError(f"prepared_component_identity:{name}")
        tools[name] = str(path)
    environment.update(RUSTC=tools["rustc"], RUSTDOC=tools["rustdoc"], RUSTFMT=tools["rustfmt"])
    environment["PATH"] = str(Path(tools["cargo"]).parent) + os.pathsep + environment.get("PATH", "")
    return environment, tools


def source_hash():
    digest = hashlib.sha256()
    for path in sorted((ROOT / "native" / "observer").rglob("*")):
        if path.is_file() and path.suffix in {".rs", ".toml", ".lock"}:
            digest.update(path.relative_to(ROOT).as_posix().encode("utf-8"))
            digest.update(path.read_bytes())
    return digest.hexdigest()


def production_inputs():
    paths = [ROOT / 'rust-toolchain.toml', ROOT / '.cargo/config.toml', Path(__file__).resolve(),
             ROOT / 'native/observer/protocol.json']
    paths.extend(path for path in (ROOT / 'native/observer').rglob('*')
                 if path.is_file() and path.suffix in {'.rs', '.toml', '.lock'})
    return [{'path': path.relative_to(ROOT).as_posix(), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
            for path in sorted(set(paths))]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build", "fmt", "test", "clippy", "lock"))
    parser.add_argument("--release", action="store_true", help="Build a separate production observer artifact.")
    args = parser.parse_args()
    if args.release and args.command != "build":
        parser.error("--release is supported only for build")
    try:
        environment, tools = prepared_environment()
        environment["OBS_BUILD_ID"] = source_hash()
        manifest = "native/observer/Cargo.toml"
        commands = {
            "lock": ["generate-lockfile", "--manifest-path", manifest],
            "fmt": ["fmt", "--manifest-path", manifest, "--", "--check"],
            "test": ["test", "--manifest-path", manifest, "--locked"],
            "clippy": ["clippy", "--manifest-path", manifest, "--locked", "--all-targets", "--", "-D", "warnings"],
            "build": ["build", "--manifest-path", manifest, "--locked"],
        }
        if args.release:
            commands["build"].append("--release")
            # The observer runs beside its own manifest, away from Python's CRT DLLs.
            environment = dict(environment)
            environment["RUSTFLAGS"] = "-C target-feature=+crt-static"
            environment.pop("CARGO_ENCODED_RUSTFLAGS", None)
        result = subprocess.run([tools["cargo"], *commands[args.command]], cwd=ROOT, env=environment)
        if result.returncode:
            return result.returncode
        if args.command == "build":
            install = ROOT / "work" / "native-observer" / ("release-install" if args.release else "install")
            install.mkdir(parents=True, exist_ok=True)
            binary = install / "session-panel-observer.exe"
            shutil.copyfile(Path(environment["CARGO_TARGET_DIR"]) / ("release" if args.release else "debug") / binary.name, binary)
            payload = {"protocol": 2, "build": environment["OBS_BUILD_ID"], "toolchain": TOOLCHAIN,
                       "binary": binary.name, "sha256": hashlib.sha256(binary.read_bytes()).hexdigest()}
            (install / "manifest.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            if args.release:
                evidence = {'schema': 1, 'mode': 'release', 'flags': commands['build'][1:],
                            'rustflags': environment.get('RUSTFLAGS', ''),
                            'encoded_rustflags': environment.get('CARGO_ENCODED_RUSTFLAGS', ''),
                            'source_hash': environment['OBS_BUILD_ID'], 'inputs': production_inputs()}
                (install / 'build-info.json').write_text(json.dumps(evidence, indent=2) + '\n', encoding='utf-8')
        return 0
    except (OSError, RuntimeError) as error:
        print(str(error) if isinstance(error, RuntimeError) else "build_tool_failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
