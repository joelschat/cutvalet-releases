#!/usr/bin/env python3
"""Decide whether latest.json should move, and move it with the ship skills' generator.

Runs inside joelschat/cutvalet-releases, from .github/workflows/publish-manifest.yml.
The DVEA repo keeps the master copy at deploy/cutvalet-releases/scripts/ and a
test there runs this file against the real v1.3.20 release data.

What it does, in order:

1. Takes the releases list (``gh api repos/<repo>/releases`` saved to a file)
   and picks the NEWEST non-draft, non-prerelease release by version (never by
   date: a re-published old release must not win).
2. Refuses to move the manifest backwards: a release older than the live
   ``latest.json`` version is a skip. The SAME version is allowed through, so
   a zip re-uploaded with ``--clobber`` (new bytes, new digest) refreshes its
   hash, and an edited release body refreshes the notes. The commit only
   happens when the file actually changed.
3. Waits (does nothing, exits 0) until all three files the manifest points at
   are uploaded AND carry GitHub's ``digest`` (``sha256:<hex>``):
   ``CutValet-<V>-win.zip``, ``CutValet-<V>-mac.zip`` and
   ``CutValet-<V>-win-setup.exe``. The installer is required, not optional:
   update_manifest.py CARRIES OVER any url it is not given, so a manifest
   written without the installer would keep pointing Windows Update & Restart
   at the PREVIOUS version's setup exe.
4. Runs the vendored ``update_manifest.py`` (byte-identical to DVEA's
   scripts/update_manifest.py, a DVEA test enforces it) with exactly the flags
   the ship skills pass, against the checked-out latest.json.
5. ``notes`` = the release body with its BOM, CRLFs, ``## <V>`` heading and
   moves block removed, which is what latest.json carried for 1.3.20.
   ``moved`` / ``added`` come from the body's hidden
   ``<!-- cutvalet-release-moves {...} -->`` block, written by DVEA's
   scripts/changelog_section.py --format release. The generator would read
   them from dvea.release_moves, which this repo cannot import (DVEA is
   private), so it prints a warning saying so; this script then writes the
   block's rows, or removes both keys when the release declares none.

Stdlib only; runs on the runner's stock python3.
"""
from __future__ import annotations

import argparse
import difflib
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

_HERE = Path(__file__).resolve().parent
MOVES_MARKER = "cutvalet-release-moves"     # same string as DVEA scripts/changelog_section.py
_MOVES_RE = re.compile(r"<!--\s*" + re.escape(MOVES_MARKER) + r"\s*(.*?)-->", re.S)
_HEADING = re.compile(r"^##\s+v?(?P<version>\d[0-9A-Za-z.+-]*)")
_DIGEST = re.compile(r"sha256:([0-9a-fA-F]{64})")


# ── version order: a copy of dvea/updater.py parse_version (a DVEA test
#    checks the two agree) ───────────────────────────────────────────────────

def _pre_token(part: str):
    try:
        return (1, int(part))
    except ValueError:
        return (0, part)


def parse_version(version: str) -> tuple:
    text = (version or "").strip().lstrip("vV").strip()
    core, _, pre = text.partition("-")
    nums: "list[int]" = []
    for part in core.split("."):
        try:
            nums.append(int(part))
        except ValueError:
            nums.append(0)
    while len(nums) < 3:
        nums.append(0)
    core_t = tuple(nums[:3])
    if not pre:
        return (core_t, 1, ())
    pre_t = tuple(_pre_token(p) for p in pre.replace("-", ".").split("."))
    return (core_t, 0, pre_t)


# ── reading the release ───────────────────────────────────────────────────

def asset_names(version: str) -> "dict[str, str]":
    v = version.lstrip("vV")
    return {"win": f"CutValet-{v}-win.zip",
            "mac": f"CutValet-{v}-mac.zip",
            "win_installer": f"CutValet-{v}-win-setup.exe"}


def asset_sha256(asset: dict) -> "str | None":
    """The asset's sha256 hex, or None while it is not fully uploaded/hashed."""
    if str(asset.get("state") or "uploaded") != "uploaded":
        return None
    m = _DIGEST.fullmatch(str(asset.get("digest") or "").strip())
    return m.group(1).lower() if m else None


def _tag_version(rel: dict) -> str:
    return str(rel.get("tag_name") or "").strip()


def pick_release(releases: "list[dict]") -> "dict | None":
    best = None
    for rel in releases or []:
        tag = _tag_version(rel)
        if rel.get("draft") or rel.get("prerelease"):
            continue
        if not re.match(r"^[vV]?\d", tag):
            continue
        if best is None or parse_version(tag) > parse_version(_tag_version(best)):
            best = rel
    return best


def _clean_body(body: str) -> str:
    return str(body or "").lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")


def release_moves(body: str) -> "tuple[list[list[str]], list[str]]":
    """``(moved, added)`` from the hidden block; both empty when there is none.

    A block that is present but unreadable raises ValueError: publishing
    without it would silently drop the "where things went" screen the
    release declared."""
    m = _MOVES_RE.search(_clean_body(body))
    if not m:
        return [], []
    data = json.loads(m.group(1))
    moved = data.get("moved") or []
    added = data.get("added") or []
    if not (isinstance(moved, list) and all(isinstance(p, list) and len(p) == 2
                                            and all(isinstance(s, str) for s in p)
                                            for p in moved)):
        raise ValueError("moves block: 'moved' must be a list of [old, new] pairs")
    if not (isinstance(added, list) and all(isinstance(s, str) for s in added)):
        raise ValueError("moves block: 'added' must be a list of strings")
    return [list(p) for p in moved], list(added)


def release_notes(body: str, version: str) -> str:
    text = _MOVES_RE.sub("", _clean_body(body))
    lines = text.split("\n")
    first = next((i for i, line in enumerate(lines) if line.strip()), None)
    if first is not None:
        m = _HEADING.match(lines[first].strip())
        if m and m.group("version").rstrip(":,") == version.lstrip("vV"):
            lines = lines[first + 1:]
    return "\n".join(lines).strip()


# ── the plan ──────────────────────────────────────────────────────────────

@dataclass
class Plan:
    action: str                 # "publish" | "wait" | "skip"
    reason: str
    version: str = ""
    argv: "list[str]" = field(default_factory=list)
    moved: "list[list[str]]" = field(default_factory=list)
    added: "list[str]" = field(default_factory=list)


def make_plan(releases: "list[dict]", live: dict, manifest_path: str) -> Plan:
    rel = pick_release(releases)
    if rel is None:
        return Plan("skip", "no published (non-draft, non-prerelease) release found")
    tag = _tag_version(rel)
    version = tag.lstrip("vV")
    live_version = str((live or {}).get("version") or "").strip()
    if live_version and parse_version(version) < parse_version(live_version):
        return Plan("skip", f"live latest.json is {live_version}, newer than the newest "
                            f"published release {version}; the manifest never moves backwards",
                    version)
    by_name = {str(a.get("name") or ""): a for a in rel.get("assets") or []}
    urls: "dict[str, str]" = {}
    hashes: "dict[str, str]" = {}
    for key, name in asset_names(version).items():
        asset = by_name.get(name)
        if asset is None:
            return Plan("wait", f"{tag}: {name} is not uploaded yet", version)
        digest = asset_sha256(asset)
        if digest is None:
            return Plan("wait", f"{tag}: {name} has no sha256 digest yet "
                                f"(state {asset.get('state')!r})", version)
        url = str(asset.get("browser_download_url") or "").strip()
        if not url.endswith("/" + name) or f"/download/{tag}/" not in url:
            return Plan("wait", f"{tag}: {name} has an unexpected download url {url!r}",
                        version)
        urls[key], hashes[key] = url, digest
    try:
        moved, added = release_moves(rel.get("body") or "")
    except ValueError as exc:
        return Plan("wait", f"{tag}: the release body's moves block is unreadable ({exc}); "
                            "fix the block in the release body", version)
    notes = release_notes(rel.get("body") or "", version)
    argv = ["--version", version,
            "--mac-url", urls["mac"], f"--mac-sha256={hashes['mac']}",
            "--win-url", urls["win"], f"--win-sha256={hashes['win']}",
            "--win-installer-url", urls["win_installer"],
            f"--win-installer-sha256={hashes['win_installer']}",
            f"--notes={notes}",
            "--out", str(manifest_path)]
    why = ("refresh of the live version" if live_version
           and parse_version(version) == parse_version(live_version)
           else f"{live_version or 'nothing'} -> {version}")
    return Plan("publish", why, version, argv, moved, added)


# ── running the generator ─────────────────────────────────────────────────

def _load_generator():
    spec = importlib.util.spec_from_file_location("update_manifest",
                                                  _HERE / "update_manifest.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def write_manifest(plan: Plan) -> None:
    """Run update_manifest.py with the plan's flags, then set moved/added."""
    out = Path(plan.argv[plan.argv.index("--out") + 1])
    print("moved/added for this version come from the release body's moves block; "
          "a 'dvea.release_moves not importable' warning below is expected here.")
    rc = _load_generator().main(list(plan.argv))
    if rc:
        raise SystemExit(f"update_manifest.py exited {rc}")
    data = json.loads(out.read_text(encoding="utf-8"))
    if (data.get("moved"), data.get("added")) == (plan.moved or None, plan.added or None):
        return                  # the generator already wrote exactly these
    data.pop("moved", None)
    data.pop("added", None)
    if plan.moved:
        data["moved"] = plan.moved
    if plan.added:
        data["added"] = plan.added
    out.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _read_live(path: Path) -> "tuple[dict, bytes | None]":
    try:
        raw = path.read_bytes()
    except OSError:
        return {}, None
    try:
        return (json.loads(raw.decode("utf-8-sig")) or {}), raw
    except ValueError:
        return {}, raw


def _emit(name: str, value: str) -> None:
    target = os.environ.get("GITHUB_OUTPUT")
    if target:
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(f"{name}={value}\n")


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description="Publish latest.json from the newest release")
    ap.add_argument("--releases", required=True,
                    help="JSON file: the releases list from the GitHub API (or one release)")
    ap.add_argument("--manifest", default="latest.json")
    ap.add_argument("--dry-run", action="store_true",
                    help="show the change, leave latest.json untouched")
    args = ap.parse_args(argv)

    releases = json.loads(Path(args.releases).read_text(encoding="utf-8-sig"))
    if isinstance(releases, dict):
        releases = [releases]
    manifest = Path(args.manifest)
    live, before = _read_live(manifest)

    target = manifest
    tmpdir = None
    if args.dry_run:
        tmpdir = tempfile.mkdtemp(prefix="manifest-dry-")
        target = Path(tmpdir) / "latest.json"
        if before is not None:
            target.write_bytes(before)
    plan = make_plan(releases, live, str(target))
    print(f"plan: {plan.action} {plan.version}: {plan.reason}")
    _emit("action", plan.action)
    _emit("version", plan.version)
    changed = False
    try:
        if plan.action == "publish":
            write_manifest(plan)
            after = target.read_bytes()
            # Line endings do not count: the generator writes the platform's
            # (CRLF on a Windows fallback run) and git normalises them.
            changed = (before is None
                       or after.replace(b"\r\n", b"\n") != before.replace(b"\r\n", b"\n"))
            if changed:
                old = (before or b"").decode("utf-8-sig").splitlines()
                new = after.decode("utf-8").splitlines()
                for line in difflib.unified_diff(old, new, "live", "new", lineterm="", n=0):
                    print(line[:200])
            else:
                print("latest.json already says exactly this; nothing to commit.")
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
    _emit("changed", "true" if (changed and not args.dry_run) else "false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
