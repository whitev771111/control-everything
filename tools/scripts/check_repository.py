"""Check explicit release candidates or added commit content without printing values."""
import argparse
import hashlib
import ipaddress
import json
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tempfile


VERSION = "8.30.1"
MEDIA = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".bmp",
         ".mp4", ".mov", ".webm", ".avi", ".mp3", ".wav", ".ogg", ".woff", ".woff2"}
PRIVATE_NAMES = {"progress-registry.json", "progress-manual.json", "progress-todos.json"}
PUBLIC_DOCS = {"docs/README.md", "docs/architecture.md", "docs/usage.md", "docs/integrations.md", "docs/windows.md",
               "docs/workbench-contract.md",
               "docs/examples/integrations/codex.toml.example",
               "docs/examples/integrations/integration-settings.example.json",
               "docs/examples/legacy/progress-manual.example.json",
               "docs/examples/legacy/progress-registry.example.json"}
WINDOWS_PATH = re.compile(r"(?i)(?<![\w])(?:[a-z]:[\\/]+(?:users|documents and settings)[\\/]+[^\s'\"<>]+|[a-z]:[\\/]+(?!temporary(?:[\\/]|$)|temp(?:[\\/]|$)|example(?:[\\/]|$))[^\s'\"<>]+)")
HOME_PATH = re.compile(r"/(?:home|Users)/([^/\s'\"<>]+)(?:/|$)")
IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}(?:\\*\.)){3}\d{1,3}(?![\d.])")
PLACEHOLDERS = {"example", "user", "username", "placeholder", "your-user", "your-name"}
DOCUMENT_NETS = [ipaddress.ip_network(net) for net in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")]


class CheckError(Exception):
    pass


def run(command, root):
    try:
        return subprocess.run(command, cwd=root, capture_output=True, check=False)
    except OSError:
        raise CheckError("tool-unavailable") from None


def git(root, *args):
    result = run(["git", "-c", "core.quotePath=false", *args], root)
    if result.returncode:
        raise CheckError("git-command-failed")
    return result.stdout


def relative_path(value):
    if not isinstance(value, str) or not value or "\\" in value or ":" in value or any(ord(c) < 32 for c in value):
        raise CheckError("invalid-candidate-path")
    path = PurePosixPath(value)
    if (not path.parts or path.is_absolute() or ".." in path.parts or str(path) != value
            or any(part.endswith((".", " ")) for part in path.parts)):
        raise CheckError("invalid-candidate-path")
    return path


def forbidden(value):
    path = relative_path(value)
    parts = tuple(part.lower() for part in path.parts)
    name = parts[-1]
    if "docs" in parts and value not in PUBLIC_DOCS:
        return "excluded-directory"
    if any(part in {".git", "work", ".venv", ".exe", "node_modules", "__pycache__", "videos"} for part in parts):
        return "excluded-directory"
    if path.suffix.lower() in MEDIA:
        return "media"
    if ((name == ".env" or name.startswith(".env.")) and name != ".env.example"
            or ".codex" in parts and value != ".codex/config.toml.example"
            or name in PRIVATE_NAMES or re.search(r"\.(?:sqlite3?|db|log)(?:[.-].*)?$", name)
            or path.suffix.lower() in {".exe", ".dll", ".zip", ".pem", ".key", ".pfx", ".pyc"}):
        return "private-or-generated-file"
    return None


def safe_file(root, value):
    path = root.joinpath(*relative_path(value).parts)
    cursor = path
    while cursor != root:
        if cursor.exists() or cursor.is_symlink():
            info = cursor.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise CheckError("linked-candidate")
        cursor = cursor.parent
    canonical = path.resolve(strict=True)
    try:
        canonical_name = canonical.relative_to(root).as_posix()
    except ValueError:
        raise CheckError("candidate-outside-root") from None
    if forbidden(canonical_name):
        raise CheckError("forbidden-canonical-candidate")
    if not canonical.is_file():
        raise CheckError("missing-candidate")
    return canonical


def privacy(text, path, start=1):
    findings = []
    for number, line in enumerate(text.splitlines(), start):
        rules = set()
        if WINDOWS_PATH.search(line):
            rules.add("absolute-windows-path")
        if any(match.group(1).lower() not in PLACEHOLDERS for match in HOME_PATH.finditer(line)):
            rules.add("personal-home-path")
        for match in IPV4.finditer(line):
            try:
                address = ipaddress.ip_address(match.group().replace("\\", ""))
            except ValueError:
                continue
            if not address.is_loopback and not any(address in network for network in DOCUMENT_NETS):
                rules.add("fixed-external-ipv4")
        findings.extend({"rule": rule, "path": path, "line": number} for rule in sorted(rules))
    return findings


def scan_gitleaks(root, executable, source, mode, interval=None):
    version = run([str(executable), "version"], root)
    if version.returncode or version.stdout.decode("utf-8", errors="replace").strip() != VERSION:
        raise CheckError("gitleaks-version-mismatch")
    with tempfile.TemporaryDirectory(prefix="repository-scan-") as directory:
        scratch = Path(directory)
        config = scratch / "default.toml"
        config.write_text("[extend]\nuseDefault = true\n", encoding="utf-8")
        template = scratch / "report.tmpl"
        template.write_text('{{ range . }}{{ .RuleID }}\t{{ .File }}\t{{ .StartLine }}\n{{ end }}', encoding="utf-8")
        ignored = scratch / "empty-ignore"
        ignored.mkdir()
        report = scratch / "findings.txt"
        command = [str(executable), mode, str(source), "--redact=100", "--no-banner", "--no-color",
                   "--ignore-gitleaks-allow", "--config", str(config), "--gitleaks-ignore-path", str(ignored),
                   "--report-format", "template", "--report-template", str(template), "--report-path", str(report)]
        if interval:
            command.append("--log-opts=" + interval + " --diff-merges=first-parent")
        result = run(command, root)
        findings = []
        if report.exists():
            for line in report.read_text(encoding="utf-8").splitlines():
                fields = line.split("\t")
                if len(fields) != 3 or not fields[2].isdigit():
                    raise CheckError("invalid-gitleaks-report")
                raw_path = fields[1].replace("\\", "/")
                if mode == "dir" and Path(fields[1]).is_absolute():
                    try:
                        raw_path = Path(fields[1]).relative_to(source).as_posix()
                    except ValueError:
                        raise CheckError("invalid-gitleaks-path") from None
                relative_path(raw_path)
                findings.append({"rule": "gitleaks:" + fields[0], "path": raw_path, "line": int(fields[2])})
        return {"version": VERSION, "exit_code": result.returncode, "findings": findings}


def check_release(root, manifest, report, executable):
    document = json.loads(manifest.read_text(encoding="utf-8-sig"))
    if not isinstance(document, dict):
        raise CheckError("invalid-manifest")
    files = document.get("files")
    excluded = document.get("excluded", [])
    if not isinstance(files, list) or not files or not isinstance(excluded, list):
        raise CheckError("empty-or-invalid-manifest")
    report["excluded"] = [str(relative_path(path)) for path in excluded]
    if len(set(files)) != len(files):
        raise CheckError("duplicate-candidate")
    with tempfile.TemporaryDirectory(prefix="repository-candidates-") as directory:
        destination = Path(directory)
        for value in files:
            reason = forbidden(value)
            if reason:
                report["blocked"].append({"path": value, "reason": reason})
                continue
            try:
                data = safe_file(root, value).read_bytes()
                if b"\x00" in data:
                    raise CheckError("binary-candidate")
                text = data.decode("utf-8-sig")
            except UnicodeError:
                report["blocked"].append({"path": value, "reason": "non-utf8-candidate"})
                continue
            except (OSError, CheckError) as error:
                reason = str(error) if isinstance(error, CheckError) else "candidate-read-failed"
                report["blocked"].append({"path": value, "reason": reason})
                continue
            report["included"].append({"path": value, "sha256": hashlib.sha256(data).hexdigest()})
            report["findings"].extend(privacy(text, value))
            target = destination.joinpath(*PurePosixPath(value).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        if report["included"]:
            report["gitleaks"] = scan_gitleaks(root, executable, destination, "dir")
        else:
            raise CheckError("no-readable-candidates")


def commit(root, value):
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", value) or set(value) == {"0"}:
        raise CheckError("invalid-commit-first-import-requires-explicit-scope")
    return git(root, "rev-parse", "--verify", value + "^{commit}").decode("ascii").strip()


def added_findings(root, parent, current, report):
    difference = ["diff", parent, current] if parent else ["diff-tree", "--root", "--no-commit-id", "-r", current]
    records = git(root, *difference, "--name-status", "-z", "--find-renames").decode("utf-8").split("\0")
    findings = []
    index = 0
    while index < len(records) - 1:
        status, value = records[index:index + 2]
        index += 2
        names = [value]
        if status.startswith(("R", "C")):
            names.append(records[index])
            value = records[index]
            index += 1
        if status == "D":
            continue
        rejected = False
        for name in names:
            reason = forbidden(name)
            path = relative_path(name)
            if status == "M" and path.parts[0] == "videos" and path.suffix.lower() in {".md", ".json", ".mjs"}:
                # Existing video source edits still pass every other path and content guard.
                reason = forbidden(PurePosixPath(*path.parts[1:]).as_posix())
            if reason:
                report["blocked"].append({"path": name, "reason": reason})
                rejected = True
        if rejected:
            continue
        entry = git(root, "ls-tree", "-z", current, "--", value)
        if entry.startswith((b"120000 ", b"160000 ")):
            report["blocked"].append({"path": value, "reason": "linked-candidate"})
            continue
        patch = git(root, *difference, "--patch", "--no-color", "--no-ext-diff", "--no-textconv", "--unified=0", "--find-renames", "--", *names).decode("utf-8")
        if re.search(r"^(?:GIT binary patch|Binary files .* differ)$", patch, re.M):
            report["blocked"].append({"path": value, "reason": "binary-candidate"})
            continue
        included = {"path": value, "commit": current}
        if included not in report["included"]:
            report["included"].append(included)
        number = None
        for line in patch.splitlines():
            hunk = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
            if hunk:
                number = int(hunk.group(1))
            elif number is not None and line.startswith("+"):
                findings.extend(privacy(line[1:], value, number))
                number += 1
            elif number is not None and line.startswith(" "):
                number += 1
    return findings


def check_changes(root, base, head, report, executable):
    base, head = commit(root, base), commit(root, head)
    if base == head:
        raise CheckError("empty-commit-range")
    report.update(base=base, head=head)
    report["merge_base"] = git(root, "merge-base", base, head).decode("ascii").strip()
    interval = base + ".." + head
    commits = git(root, "rev-list", "--reverse", "--topo-order", interval).decode("ascii").splitlines()
    if not commits:
        raise CheckError("empty-commit-range")
    for current in commits:
        parents = git(root, "rev-list", "--parents", "-n", "1", current).decode("ascii").split()[1:]
        findings = added_findings(root, parents[0] if parents else None, current, report)
        # Imported parent content is already covered where its new commit introduced it.
        for parent in parents[1:]:
            if not findings:
                break
            other = added_findings(root, parent, current, report)
            keys = {(item["path"], item["line"], item["rule"]) for item in other}
            findings = [item for item in findings if (item["path"], item["line"], item["rule"]) in keys]
        report["findings"].extend(dict(item, commit=current) for item in findings)
    if report["blocked"]:
        raise CheckError("forbidden-commit-content-not-read")
    report["gitleaks"] = scan_gitleaks(root, executable, root, "git", interval)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("changes", "release"))
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--gitleaks", type=Path, default=Path("work/tools/gitleaks-8.30.1/gitleaks.exe"))
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--base")
    parser.add_argument("--head")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    report = {"mode": args.mode, "status": "blocked", "included": [], "excluded": [], "blocked": [], "findings": [], "errors": []}
    report_allowed = False
    try:
        report_path = args.report if args.report.is_absolute() else root / args.report
        relative = report_path.absolute().relative_to(root).as_posix()
        if not relative.startswith("work/") or ".." in PurePosixPath(relative).parts:
            raise CheckError("report-must-be-under-work")
        # Check each existing parent before creating or writing evidence.
        cursor = report_path.absolute()
        while cursor != root:
            if cursor.is_symlink() or cursor.exists() and getattr(cursor.lstat(), "st_file_attributes", 0) & 0x400:
                raise CheckError("linked-report-path")
            cursor = cursor.parent
        report_allowed = True
        executable = args.gitleaks if args.gitleaks.is_absolute() else root / args.gitleaks
        if args.mode == "release":
            if args.manifest is None:
                raise CheckError("manifest-required")
            check_release(root, args.manifest, report, executable)
        else:
            if not args.base or not args.head:
                raise CheckError("base-and-head-required")
            check_changes(root, args.base, args.head, report, executable)
        leaks = report.get("gitleaks", {})
        if not report["blocked"] and not report["findings"] and leaks.get("exit_code") == 0 and not leaks.get("findings"):
            report["status"] = "passed"
    except CheckError as error:
        report["errors"].append(str(error))
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        # Never include exception text from parsers, tools or filesystem APIs.
        report["errors"].append("scope-read-or-tool-failed")
    try:
        if not report_allowed:
            return 1
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    except OSError:
        return 1
    print("Repository check: " + report["status"] + "; inspect the local redacted report.")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
