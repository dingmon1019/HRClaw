import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import zipfile


ROOT_DIR = Path(__file__).resolve().parents[1]
DIST_DIR = ROOT_DIR / "dist"
ARCHIVE_PREFIX = "win-agent-runtime"
ALLOWED_PATHS = (
    ".env.example",
    ".gitignore",
    "LICENSE",
    "README.md",
    "main.py",
    "requirements.txt",
    "app",
    "docs",
    "examples",
    "scripts",
    "tests",
    "ui",
)
FORBIDDEN_SEGMENTS = {
    ".git",
    ".venv",
    ".codex-pkgs",
    ".codex-venv",
    ".pytest_cache",
    "__pycache__",
    "data",
    "runtime_workspace",
    "workspace",
    "logs",
    "secrets",
    "protected_blobs",
    "htmlcov",
    "build",
}
FORBIDDEN_FILENAMES = {
    ".env",
    "audit.jsonl",
    "session_secret.bin",
    "win_agent_runtime.db",
}
FORBIDDEN_SUFFIXES = {
    ".db",
    ".sqlite3",
    ".log",
    ".pyc",
    ".pyo",
    ".pyd",
}
RUNTIME_STATE_OUTSIDE_REPO_STATEMENT = (
    "Live runtime state belongs outside the repository by default under %LOCALAPPDATA%\\WinAgentRuntime "
    "and must not be shipped inside release archives."
)


@dataclass(frozen=True)
class PackageResult:
    archive_path: Path
    file_count: int
    revision: str | None = None
    archive_sha256: str | None = None
    sha256_path: Path | None = None


def _iter_allowed_files(root: Path):
    for relative in ALLOWED_PATHS:
        path = root / relative
        if not path.exists():
            continue
        if path.is_file():
            relative_path = Path(relative)
            if not _is_forbidden_relative(relative_path):
                yield path, relative_path
            continue
        for child in sorted(path.rglob("*")):
            if child.is_dir():
                continue
            relative_path = child.relative_to(root)
            if _is_forbidden_relative(relative_path):
                continue
            yield child, relative_path


def _is_forbidden_relative(relative: Path) -> bool:
    parts = {part.lower() for part in relative.parts}
    if parts.intersection({segment.lower() for segment in FORBIDDEN_SEGMENTS}):
        return True
    if relative.name.lower() in {name.lower() for name in FORBIDDEN_FILENAMES}:
        return True
    if relative.suffix.lower() in FORBIDDEN_SUFFIXES:
        return True
    return False


def _validate_relative(relative: Path) -> None:
    if _is_forbidden_relative(relative):
        parts = {part.lower() for part in relative.parts}
        if parts.intersection({segment.lower() for segment in FORBIDDEN_SEGMENTS}):
            raise ValueError(f"Forbidden artifact path detected in release manifest: {relative.as_posix()}")
        if relative.name.lower() in {name.lower() for name in FORBIDDEN_FILENAMES}:
            raise ValueError(f"Forbidden file detected in release manifest: {relative.as_posix()}")
        if relative.suffix.lower() in FORBIDDEN_SUFFIXES:
            raise ValueError(f"Forbidden file suffix detected in release manifest: {relative.as_posix()}")


def verify_working_tree(root: Path) -> list[str]:
    findings: list[str] = []
    for name in [
        ".venv",
        ".codex-pkgs",
        ".codex-venv",
        ".pytest_cache",
        "data",
        "runtime_workspace",
        "workspace",
    ]:
        if (root / name).exists():
            findings.append(name)
    return findings


def build_archive(root: Path, *, version: str | None = None) -> PackageResult:
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    suffix = version or "local"
    archive_path = DIST_DIR / f"{ARCHIVE_PREFIX}-{suffix}.zip"
    if archive_path.exists():
        archive_path.unlink()

    file_count = 0
    included_paths: list[str] = []
    revision = _git_revision(root)
    manifest = {
        "archive_prefix": ARCHIVE_PREFIX,
        "version": suffix,
        "build_time_utc": datetime.now(timezone.utc).isoformat(),
        "git_revision": revision,
        "include_policy": sorted(ALLOWED_PATHS),
        "included_paths": [],
        "excluded_policy": {
            "forbidden_segments": sorted(FORBIDDEN_SEGMENTS),
            "forbidden_filenames": sorted(FORBIDDEN_FILENAMES),
            "forbidden_suffixes": sorted(FORBIDDEN_SUFFIXES),
        },
        "runtime_state_outside_repo": True,
        "runtime_state_outside_repo_statement": RUNTIME_STATE_OUTSIDE_REPO_STATEMENT,
        "working_tree_preflight_default": True,
        "working_tree_verification_available": True,
        "archive_sha256_sidecar": f"{archive_path.name}.sha256",
    }
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as handle:
        for absolute, relative in _iter_allowed_files(root):
            _validate_relative(relative)
            archive_relative = Path(f"{ARCHIVE_PREFIX}-{suffix}") / relative
            handle.write(absolute, archive_relative.as_posix())
            file_count += 1
            included_paths.append(relative.as_posix())
        manifest_relative = Path(f"{ARCHIVE_PREFIX}-{suffix}") / "release_manifest.json"
        manifest["included_paths"] = sorted(included_paths)
        handle.writestr(
            manifest_relative.as_posix(),
            json.dumps(manifest, indent=2),
        )
    verify_archive(archive_path)
    archive_sha256 = _sha256_file(archive_path)
    sha256_path = archive_path.with_suffix(f"{archive_path.suffix}.sha256")
    sha256_path.write_text(f"{archive_sha256}  {archive_path.name}\n", encoding="utf-8")
    return PackageResult(
        archive_path=archive_path,
        file_count=file_count,
        revision=revision,
        archive_sha256=archive_sha256,
        sha256_path=sha256_path,
    )


def verify_archive(archive_path: Path) -> None:
    manifest_found = False
    with zipfile.ZipFile(archive_path, "r") as handle:
        for member in handle.namelist():
            relative = Path(member).parts[1:]
            if not relative:
                continue
            if Path(*relative).name == "release_manifest.json":
                manifest_found = True
                manifest = json.loads(handle.read(member).decode("utf-8"))
                if not manifest.get("runtime_state_outside_repo"):
                    raise ValueError("Release manifest must declare that runtime state stays outside the repo.")
                if not manifest.get("runtime_state_outside_repo_statement"):
                    raise ValueError("Release manifest is missing the runtime-state statement.")
                continue
            _validate_relative(Path(*relative))
    if not manifest_found:
        raise ValueError("Release archive is missing release_manifest.json")


def clean_dist() -> None:
    if DIST_DIR.exists():
        shutil.rmtree(DIST_DIR)


def _git_revision(root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    revision = result.stdout.strip()
    return revision or None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a clean release archive from an allowlist.")
    parser.add_argument("--version", default=None, help="Optional version label used in the archive name.")
    parser.add_argument(
        "--verify-working-tree",
        action="store_true",
        help="Fail if common local runtime/development artifacts still exist in the working tree.",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Remove the dist directory before packaging.",
    )
    parser.add_argument(
        "--verify-archive",
        default=None,
        help="Verify an existing archive path instead of creating a new one.",
    )
    parser.add_argument(
        "--ci",
        action="store_true",
        help="CI-oriented mode: clean dist, verify working tree artifacts, then build and verify the release archive.",
    )
    parser.add_argument(
        "--allow-dirty-working-tree",
        action="store_true",
        help="Explicitly bypass the working-tree runtime-artifact preflight. Use only for development smoke builds.",
    )
    args = parser.parse_args()

    if args.verify_archive:
        verify_archive(Path(args.verify_archive))
        print(f"Verified {args.verify_archive}.")
        return

    if args.ci:
        args.clean = True
        args.verify_working_tree = True
    verify_working_tree_by_default = not args.allow_dirty_working_tree
    if args.clean:
        clean_dist()
    if args.verify_working_tree or verify_working_tree_by_default:
        findings = verify_working_tree(ROOT_DIR)
        if findings:
            joined = ", ".join(findings)
            raise SystemExit(f"Working tree still contains ignored local artifacts: {joined}")
    result = build_archive(ROOT_DIR, version=args.version)
    revision = f" @ {result.revision}" if result.revision else ""
    print(
        f"Created {result.archive_path} with {result.file_count} files{revision}. "
        f"sha256={result.archive_sha256}"
    )


if __name__ == "__main__":
    main()
