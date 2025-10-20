#!/usr/bin/env python3
"""
Generate a new project from an existing project template.

Features:
- Copies a template project directory to a new destination
- Renames files, folders, and in-file content from old project name to new project name
- Supports multiple naming variants (kebab-case, snake_case, PascalCase, camelCase, SCREAMING_SNAKE, etc.)
- Skips common binary/build directories by default and detects binary files to avoid corrupting them
- Dry-run mode to preview changes
- Optional git initialization in the destination
- Optional extra replacements via a JSON mapping file or CLI

Usage examples:
  python3 scripts/generate_project_from_template.py \
    --template /path/to/old_project \
    --dest /path/to/new_project \
    --new-name "New Awesome Project" \
    --old-name "Old Project"

  # Dry run
  python3 scripts/generate_project_from_template.py --template ./old --dest ./new --new-name NewApp --dry-run

  # Add extra replacements (inline JSON mapping)
  python3 scripts/generate_project_from_template.py --template ./old --dest ./new --new-name NewApp \
    --extra-replacements '{"old.company": "new.company", "OLD_VENDOR": "NEW_VENDOR"}'

  # Provide an extra replacements file next to template
  # (if template_replacements.json exists in template root, it is auto-loaded)
  python3 scripts/generate_project_from_template.py --template ./old --dest ./new --new-name NewApp \
    --config ./old/template_replacements.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

DEFAULT_EXCLUDES = [
    ".git",
    ".github",
    "node_modules",
    "__pycache__",
    "dist",
    "build",
    ".idea",
    ".vscode",
    ".venv",
    "venv",
]

ALWAYS_TEXT_EXTENSIONS = {
    ".md",
    ".txt",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".conf",
    ".gitignore",
    ".gitattributes",
    ".dockerignore",
    ".py",
    ".sh",
    ".bash",
    ".zsh",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".css",
    ".scss",
    ".less",
    ".html",
    ".htm",
    ".xml",
    ".c",
    ".h",
    ".cpp",
    ".hpp",
    ".cc",
    ".java",
    ".kt",
    ".kts",
    ".go",
    ".rs",
    ".rb",
    ".php",
    ".gradle",
    ".sln",
    ".csproj",
    ".cs",
    ".cmake",
    ".mk",
    "Makefile",
    "Dockerfile",
}


@dataclass
class Options:
    template: Path
    dest: Path
    old_name: str
    new_name: str
    excludes: List[str]
    dry_run: bool
    no_content_replace: bool
    init_git: bool
    extra_replacements: Dict[str, str]


def debug(msg: str) -> None:
    print(msg)


def is_probably_text_file(path: Path) -> bool:
    if path.is_dir():
        return False
    name = path.name
    suffix = path.suffix
    if name in ALWAYS_TEXT_EXTENSIONS or suffix in ALWAYS_TEXT_EXTENSIONS:
        return True
    try:
        with path.open("rb") as f:
            chunk = f.read(4096)
            if b"\x00" in chunk:
                return False
            # Consider it binary if >30% non-ASCII bytes
            if not chunk:
                return True
            non_text = sum(1 for b in chunk if b < 9 or (13 < b < 32) or b > 126)
            return (non_text / len(chunk)) < 0.30
    except Exception:
        return False


def tokenize(name: str) -> List[str]:
    parts = re.split(r"[^A-Za-z0-9]+", name)
    tokens: List[str] = []
    for part in parts:
        if not part:
            continue
        tokens.extend(
            re.findall(r"[A-Z]+(?=[A-Z][a-z0-9])|[A-Z]?[a-z0-9]+|[0-9]+", part)
        )
    return tokens


def to_variants(name: str) -> Dict[str, str]:
    tokens = tokenize(name)
    if not tokens:
        return {"raw": name}
    lowers = [t.lower() for t in tokens]
    uppers = [t.upper() for t in tokens]

    def pascal(ts: List[str]) -> str:
        return "".join(t.capitalize() for t in ts)

    def camel(ts: List[str]) -> str:
        if not ts:
            return ""
        return ts[0].lower() + "".join(t.capitalize() for t in ts[1:])

    variants = {
        "raw": name,
        "lower": "".join(lowers),
        "upper": "".join(uppers),
        "kebab": "-".join(lowers),
        "snake": "_".join(lowers),
        "screaming_snake": "_".join(uppers),
        "pascal": pascal(lowers),
        "camel": camel(lowers),
        "dot": ".".join(lowers),
        "space": " ".join(lowers),
        "title": " ".join(t.capitalize() for t in lowers),
    }
    return variants


def build_replacement_map(old_name: str, new_name: str) -> Dict[str, str]:
    old_v = to_variants(old_name)
    new_v = to_variants(new_name)
    mapping: Dict[str, str] = {}

    # Core pairs
    pairs = [
        (old_v.get("raw", old_name), new_v.get("raw", new_name)),
        (old_v.get("kebab"), new_v.get("kebab")),
        (old_v.get("snake"), new_v.get("snake")),
        (old_v.get("screaming_snake"), new_v.get("screaming_snake")),
        (old_v.get("pascal"), new_v.get("pascal")),
        (old_v.get("camel"), new_v.get("camel")),
        (old_v.get("lower"), new_v.get("lower")),
        (old_v.get("upper"), new_v.get("upper")),
        (old_v.get("dot"), new_v.get("dot")),
        (old_v.get("title"), new_v.get("title")),
        (old_v.get("space"), new_v.get("space")),
    ]

    for k, v in pairs:
        if k is None or v is None:
            continue
        mapping[k] = v

    # Remove identity mappings
    mapping = {k: v for k, v in mapping.items() if k != v}

    return mapping


def load_extra_replacements(config_path: Path | None, inline_json: str | None) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    if config_path and config_path.exists():
        try:
            mapping.update(json.loads(config_path.read_text(encoding="utf-8")))
        except Exception as e:
            debug(f"Warning: failed to load config {config_path}: {e}")
    if inline_json:
        try:
            mapping.update(json.loads(inline_json))
        except Exception as e:
            debug(f"Warning: failed to parse --extra-replacements JSON: {e}")
    return mapping


def apply_replacements(s: str, mapping: Dict[str, str]) -> str:
    if not mapping:
        return s
    # Replace longer keys first to avoid partial overlaps
    for old in sorted(mapping.keys(), key=len, reverse=True):
        s = s.replace(old, mapping[old])
    return s


def should_exclude(rel_path: Path, excludes: Iterable[str]) -> bool:
    parts = set(rel_path.parts)
    for ex in excludes:
        if ex in parts:
            return True
        # Leading dot files or exact names
        if rel_path.name == ex:
            return True
    return False


@dataclass
class PlanItem:
    src: Path
    dst: Path
    is_dir: bool
    content_replace: bool


def plan_copy(template: Path, dest: Path, mapping: Dict[str, str], excludes: List[str], no_content_replace: bool) -> List[PlanItem]:
    plan: List[PlanItem] = []
    for root, dirs, files in os.walk(template):
        root_path = Path(root)
        rel_root = root_path.relative_to(template)

        if should_exclude(rel_root, excludes):
            # prune dirs under excluded root
            dirs[:] = []
            continue

        # Compute destination directory path (with name replacements)
        dest_rel_root_str = apply_replacements(str(rel_root), mapping) if str(rel_root) != "." else ""
        dest_root = dest / dest_rel_root_str

        # Plan directories
        for d in list(dirs):
            rel_d = rel_root / d
            if should_exclude(rel_d, excludes):
                dirs.remove(d)
                continue
            dest_dir_name = apply_replacements(d, mapping)
            dest_dir = dest_root / dest_dir_name
            plan.append(PlanItem(src=root_path / d, dst=dest_dir, is_dir=True, content_replace=False))

        # Plan files
        for f in files:
            rel_f = rel_root / f
            if should_exclude(rel_f, excludes):
                continue
            dest_file_name = apply_replacements(f, mapping)
            dest_file = dest_root / dest_file_name
            src_file = root_path / f
            content_replace = (not no_content_replace) and is_probably_text_file(src_file)
            plan.append(PlanItem(src=src_file, dst=dest_file, is_dir=False, content_replace=content_replace))

    return plan


def execute_plan(plan: List[PlanItem], mapping: Dict[str, str], dry_run: bool) -> Tuple[int, int, int]:
    dirs_created = 0
    files_copied = 0
    files_rewritten = 0

    # Ensure directories first
    for item in plan:
        if item.is_dir:
            if dry_run:
                debug(f"DIR   -> {item.dst}")
            else:
                item.dst.mkdir(parents=True, exist_ok=True)
            dirs_created += 1

    # Then files
    for item in plan:
        if item.is_dir:
            continue
        if dry_run:
            action = "REWR" if item.content_replace else "COPY"
            debug(f"{action}  {item.src} -> {item.dst}")
            if item.content_replace:
                files_rewritten += 1
            else:
                files_copied += 1
            continue

        item.dst.parent.mkdir(parents=True, exist_ok=True)
        if item.content_replace:
            try:
                text = item.src.read_text(encoding="utf-8", errors="ignore")
                new_text = apply_replacements(text, mapping)
                item.dst.write_text(new_text, encoding="utf-8")
                files_rewritten += 1
            except Exception:
                # Fallback to byte copy if anything goes wrong
                shutil.copy2(item.src, item.dst)
                files_copied += 1
        else:
            shutil.copy2(item.src, item.dst)
            files_copied += 1

    return dirs_created, files_copied, files_rewritten


def is_git_url(s: str) -> bool:
    return s.startswith("git@") or s.startswith("http://") or s.startswith("https://")


def clone_to_temp(url: str, branch: str | None = None) -> Path:
    import subprocess

    tmpdir = Path(tempfile.mkdtemp(prefix="template_clone_"))
    cmd = ["git", "clone", "--depth", "1"]
    if branch:
        cmd += ["-b", branch]
    cmd += [url, str(tmpdir)]
    subprocess.run(cmd, check=True)
    return tmpdir


def init_git_repo(path: Path) -> None:
    import subprocess

    try:
        subprocess.run(["git", "init"], cwd=str(path), check=True)
        # Do not auto-commit to respect user workflows
    except Exception as e:
        debug(f"Warning: failed to init git repo in {path}: {e}")


def parse_args(argv: List[str]) -> Options:
    p = argparse.ArgumentParser(description="Generate a new project from an existing project template")
    p.add_argument("--template", required=True, help="Path to template directory or a git URL")
    p.add_argument("--dest", required=True, help="Destination directory for the new project")
    p.add_argument("--new-name", required=True, help="New project name (e.g., 'New Project' or 'new-project')")
    p.add_argument("--old-name", default=None, help="Old project name (if omitted, derived from template directory name)")
    p.add_argument("--branch", default=None, help="Template git branch to clone (only if --template is a git URL)")
    p.add_argument("--exclude", action="append", default=[], help="Additional directories or files to exclude (can be used multiple times)")
    p.add_argument("--dry-run", action="store_true", help="Preview the plan without writing files")
    p.add_argument("--no-content-replace", action="store_true", help="Do not modify file contents, only rename files/folders")
    p.add_argument("--init-git", action="store_true", help="Run 'git init' in the destination after generation")
    p.add_argument("--config", default=None, help="Path to a JSON file with extra string replacements {from: to}")
    p.add_argument("--extra-replacements", default=None, help="Inline JSON mapping of extra string replacements {from: to}")
    p.add_argument("--force", action="store_true", help="Allow non-empty destination directory (may overwrite files)")

    args = p.parse_args(argv)

    # Resolve template path (clone if git URL)
    template_path: Path
    cleanup_tmp = False
    if is_git_url(args.template):
        template_path = clone_to_temp(args.template, args.branch)
        cleanup_tmp = True
    else:
        template_path = Path(args.template).resolve()
        if not template_path.exists() or not template_path.is_dir():
            p.error(f"Template path does not exist or is not a directory: {template_path}")

    try:
        dest_path = Path(args.dest).resolve()
    except Exception:
        dest_path = Path(args.dest)

    if dest_path.exists():
        if any(dest_path.iterdir()) and not args.force:
            p.error(f"Destination exists and is not empty: {dest_path}. Use --force to proceed.")
    else:
        dest_path.mkdir(parents=True, exist_ok=True)

    old_name = args.old_name or template_path.name
    new_name = args.new_name

    excludes = list(DEFAULT_EXCLUDES)
    for ex in args.exclude:
        if ex:
            excludes.append(ex)

    extra_map = load_extra_replacements(Path(args.config) if args.config else None, args.extra_replacements)

    opts = Options(
        template=template_path,
        dest=dest_path,
        old_name=old_name,
        new_name=new_name,
        excludes=excludes,
        dry_run=args.dry_run,
        no_content_replace=args.no_content_replace,
        init_git=args.init_git,
        extra_replacements=extra_map,
    )

    # Attach cleanup flag (simple closure-like attr)
    setattr(opts, "_cleanup_tmp", cleanup_tmp)
    return opts


def main(argv: List[str]) -> int:
    opts = parse_args(argv)

    # Build mapping
    mapping = build_replacement_map(opts.old_name, opts.new_name)
    mapping.update(opts.extra_replacements)

    debug("Replacement mapping (top 12 shown):")
    shown = 0
    for k in sorted(mapping.keys(), key=len, reverse=True):
        debug(f"  {k!r} -> {mapping[k]!r}")
        shown += 1
        if shown >= 12:
            break
    if len(mapping) > shown:
        debug(f"  ... and {len(mapping) - shown} more")

    # Auto-load template_replacements.json if present and not already provided
    auto_cfg = opts.template / "template_replacements.json"
    if not opts.extra_replacements and auto_cfg.exists():
        debug(f"Loading additional replacements from {auto_cfg}")
        mapping.update(load_extra_replacements(auto_cfg, None))

    if opts.dry_run:
        debug("Running in dry-run mode; no files will be written")

    # Build plan
    plan = plan_copy(
        template=opts.template,
        dest=opts.dest,
        mapping=mapping,
        excludes=opts.excludes,
        no_content_replace=opts.no_content_replace,
    )

    # Execute plan
    dirs_created, files_copied, files_rewritten = execute_plan(plan, mapping, opts.dry_run)

    debug("")
    debug(f"Plan summary:")
    debug(f"  Directories created: {dirs_created}")
    debug(f"  Files copied:        {files_copied}")
    debug(f"  Files rewritten:     {files_rewritten}")

    if opts.init_git and not opts.dry_run:
        init_git_repo(opts.dest)

    # Cleanup temp clone if used
    if getattr(opts, "_cleanup_tmp", False):
        try:
            shutil.rmtree(opts.template, ignore_errors=True)
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
