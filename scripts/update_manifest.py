#!/usr/bin/env python3
"""Generate/refresh the in-app update manifest (latest.json).

The app's update check (dvea/updater.py) fetches this JSON from a stable public
URL (branding.UPDATE_MANIFEST_URL) and compares its ``version`` to the running
build. Host it on GitHub Pages (a small public repo); the big zips are GitHub
Release assets on the same public repo — point the *_url fields at their
releases/download direct links (the in-app updater downloads whatever URL it's
given, so the link must serve the zip bytes, NOT an HTML page or a Drive
folder). Always pass the matching *_sha256 so the in-app download is
integrity-verified.

Usage:
    python scripts/update_manifest.py \
        --version 1.0.0 \
        --mac-url  "https://github.com/joelschat/cutvalet-releases/releases/download/v1.0.0/CutValet-1.0.0-mac.zip" \
        --mac-sha256 "<sha256 of the mac zip as uploaded>" \
        --win-url  "https://github.com/joelschat/cutvalet-releases/releases/download/v1.0.0/CutValet-1.0.0-win.zip" \
        --win-sha256 "<sha256 of the win zip as uploaded>" \
        --notes    "Local-first AI; folder templates; Windows stability." \
        --out      ../cutvalet-releases/latest.json

Only the fields you pass are updated; omitted URL fields are carried over from an
existing --out file so you can, e.g., refresh just the Windows build.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _declare_moves(data: dict, version: str) -> None:
    """``moved`` / ``added`` are THIS release's declaration (board 18b), read
    from ``dvea.release_moves`` for *version*, never carried over from the
    previous manifest. Carrying them forward told every 1.3.x updater that
    its Archive Manager had just moved (the 1.2.0 rows rode along for
    months, 2026-09-30). A release that declares nothing gets neither key,
    which is what switches the update dialog back to its plain three lines.
    """
    data.pop("moved", None)
    data.pop("added", None)
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from dvea import release_moves
    except Exception as exc:                            # noqa: BLE001
        print(f"warning: dvea.release_moves not importable ({exc}); "
              "no moved/added written", file=sys.stderr)
        return
    moved = [list(pair) for pair in release_moves.moved_for(version)]
    added = list(release_moves.added_for(version))
    if moved:
        data["moved"] = moved
    if added:
        data["added"] = added


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Write the update manifest latest.json")
    ap.add_argument("--version", required=True, help="e.g. 0.9.1-beta.3 (matches branding.APP_VERSION)")
    ap.add_argument("--mac-url", default=None, help="direct-download URL for the macOS zip")
    ap.add_argument("--win-url", default=None, help="direct-download URL for the Windows zip")
    ap.add_argument("--mac-sha256", default=None,
                    help="SHA-256 (hex) of the macOS zip as uploaded")
    ap.add_argument("--win-sha256", default=None,
                    help="SHA-256 (hex) of the Windows zip as uploaded")
    ap.add_argument("--win-installer-url", default=None,
                    help="direct-download URL for the Windows SETUP EXE — the "
                         "file 'Download manually…' hands a person (the zip is "
                         "the in-app updater's format and loses the Start Menu "
                         "entry and the uninstaller)")
    ap.add_argument("--win-installer-sha256", default=None,
                    help="SHA-256 (hex) of the Windows setup exe as uploaded. "
                         "With it, the in-app Update & Restart on Windows RUNS "
                         "THE INSTALLER instead of swapping the app folder; "
                         "without it the zip route is used. Never publish a "
                         "hash you did not compute from the uploaded bytes.")
    ap.add_argument("--mac-installer-url", default=None,
                    help="direct-download URL for a macOS installer, if one is "
                         "ever published; the .app zip is normally fine there")
    ap.add_argument("--notes", default=None, help="short 'what's new' shown in the update dialog")
    ap.add_argument("--out", default="latest.json", help="manifest path to write")
    args = ap.parse_args(argv)

    out = Path(args.out)
    data: dict = {}
    if out.exists():
        try:
            data = json.loads(out.read_text(encoding="utf-8")) or {}
        except (OSError, ValueError):
            data = {}

    data["version"] = args.version.lstrip("vV")
    if args.notes is not None:
        data["notes"] = args.notes
    _declare_moves(data, data["version"])
    if args.mac_url is not None:
        data["mac_url"] = args.mac_url
    if args.win_url is not None:
        data["win_url"] = args.win_url
    if args.win_installer_url is not None:
        data["win_installer_url"] = args.win_installer_url
    if args.mac_installer_url is not None:
        data["mac_installer_url"] = args.mac_installer_url
    # A hash must never outlive its URL: carrying the previous version's hash
    # against a new URL makes every in-app update FAIL verification (a wrong
    # hash is fatal; an absent hash just skips verification with a warning).
    # So a *_url passed without its *_sha256 DROPS any carried-over hash.
    if args.mac_sha256 is not None:
        data["mac_sha256"] = args.mac_sha256.strip().lower()
    elif args.mac_url is not None:
        data.pop("mac_sha256", None)
    if args.win_sha256 is not None:
        data["win_sha256"] = args.win_sha256.strip().lower()
    elif args.win_url is not None:
        data.pop("win_sha256", None)
    if args.win_installer_sha256 is not None:
        data["win_installer_sha256"] = args.win_installer_sha256.strip().lower()
    elif args.win_installer_url is not None:
        data.pop("win_installer_sha256", None)

    # Warn (don't fail) if a platform URL is missing — a partial manifest still
    # works for the platform it does cover. Likewise warn when a URL has no
    # hash: the in-app download then skips integrity verification.
    if not data.get("win_installer_url"):
        print("warning: no win_installer_url set — 'Download manually…' will "
              "hand Windows users the app-folder zip instead of the installer",
              file=sys.stderr)
    elif not data.get("win_installer_sha256"):
        print("warning: win_installer_url set but no win_installer_sha256 — "
              "Windows in-app updates will use the folder-swap route instead "
              "of running the installer",
              file=sys.stderr)
    for key in ("mac_url", "win_url"):
        if not data.get(key):
            print(f"warning: no {key} set — {key.split('_')[0]} users won't get a download link",
                  file=sys.stderr)
        elif not data.get(key.replace("_url", "_sha256")):
            print(f"warning: {key} set but no {key.replace('_url', '_sha256')} — "
                  "in-app updates will skip integrity verification",
                  file=sys.stderr)

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {out} (version {data['version']}).")
    print("Publish it (commit+push to the Pages repo, or upload to your host) so "
          "the app can see it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
