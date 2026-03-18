#!/usr/bin/env python3
"""
probe_updater.py — Probe APIs needed for the Fabric/mod updater.

Tests:
  1. Mojang version manifest  — confirm MC 1.21.11 exists
  2. Fabric meta API          — check 1.21.11 is stable, get latest loader/installer
  3. Modrinth API             — test version query with a real mod

Usage:
  python3 probe_updater.py
"""

import json
import sys
import urllib.request
from typing import Any, Dict, Optional

MC_VERSION = "1.21.11"

# Modrinth project IDs to probe (slug or ID both work in the API)
PROBE_MODS = [
    ("Fabric API",        "P7dR8mSH"),
    ("Sodium",            "AANobbMI"),
    ("Lithium",           "gvQqBUqZ"),
    ("Distant Horizons",  "distanthorizons"),
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def fetch(url: str, label: str) -> Optional[Any]:
    print(f"\n  GET {url}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "mc-server-probe/1.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.load(r)
        print(f"  ✓ {label}: HTTP 200")
        return data
    except urllib.error.HTTPError as e:
        print(f"  ✗ {label}: HTTP {e.code}")
    except Exception as e:
        print(f"  ✗ {label}: {e}")
    return None


def section(title: str) -> None:
    print(f"\n{'━' * 60}")
    print(f"  {title}")
    print("━" * 60)


# ── 1. Mojang version manifest ────────────────────────────────────────────────

section("1. Mojang version manifest")

manifest = fetch(
    "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json",
    "version manifest",
)

if manifest:
    latest = manifest.get("latest", {})
    print(f"\n  latest.release  = {latest.get('release')}")
    print(f"  latest.snapshot = {latest.get('snapshot')}")

    versions = {v["id"]: v for v in manifest.get("versions", [])}
    if MC_VERSION in versions:
        v = versions[MC_VERSION]
        print(f"\n  {MC_VERSION} found:")
        print(f"    type         = {v['type']}")
        print(f"    releaseTime  = {v['releaseTime']}")
        print(f"    url          = {v['url']}")
    else:
        print(f"\n  ✗ {MC_VERSION} NOT found in manifest")
        # Show nearby versions
        nearby = [vid for vid in versions if vid.startswith("1.21")]
        print(f"  Versions matching 1.21.*: {sorted(nearby)[-10:]}")


# ── 2. Fabric meta API ────────────────────────────────────────────────────────

section("2. Fabric meta API")

# 2a. Game versions — check stability flag for MC_VERSION
game_versions = fetch(
    "https://meta.fabricmc.net/v2/versions/game",
    "Fabric game versions",
)
if game_versions:
    match = next((v for v in game_versions if v["version"] == MC_VERSION), None)
    if match:
        print(f"\n  {MC_VERSION} in Fabric game versions:")
        print(f"    stable = {match['stable']}")
    else:
        print(f"\n  ✗ {MC_VERSION} not listed in Fabric game versions")
        nearby = [v["version"] for v in game_versions if v["version"].startswith("1.21")]
        print(f"  Versions matching 1.21.*: {nearby[:10]}")

# 2b. Loader versions for MC_VERSION
loaders = fetch(
    f"https://meta.fabricmc.net/v2/versions/loader/{MC_VERSION}",
    f"Fabric loaders for {MC_VERSION}",
)
if loaders:
    latest_loader = loaders[0]  # sorted newest-first
    li = latest_loader.get("loader", {})
    print(f"\n  Latest loader for {MC_VERSION}:")
    print(f"    version  = {li.get('version')}")
    print(f"    stable   = {li.get('stable')}")
    print(f"    Total loader entries returned: {len(loaders)}")

# 2c. Installer versions
installers = fetch(
    "https://meta.fabricmc.net/v2/versions/installer",
    "Fabric installers",
)
if installers:
    latest_inst = installers[0]
    print(f"\n  Latest installer:")
    print(f"    version  = {latest_inst.get('version')}")
    print(f"    stable   = {latest_inst.get('stable')}")
    print(f"    url      = {latest_inst.get('url')}")

# 2d. Full server launcher URL (what setup.sh uses)
if loaders and installers:
    loader_ver    = loaders[0]["loader"]["version"]
    installer_ver = installers[0]["version"]
    launch_url = (
        f"https://meta.fabricmc.net/v2/versions/loader/{MC_VERSION}"
        f"/{loader_ver}/{installer_ver}/server/jar"
    )
    print(f"\n  Server launcher JAR URL:")
    print(f"    {launch_url}")


# ── 3. Modrinth API ───────────────────────────────────────────────────────────

section(f"3. Modrinth API  (game_versions=[{MC_VERSION!r}], loaders=[\"fabric\"])")

for mod_name, project_id in PROBE_MODS:
    import urllib.parse
    params = urllib.parse.urlencode({
        "game_versions": json.dumps([MC_VERSION]),
        "loaders":       json.dumps(["fabric"]),
    })
    url = f"https://api.modrinth.com/v2/project/{project_id}/version?{params}"
    versions = fetch(url, f"{mod_name} ({project_id})")
    if versions:
        print(f"\n  {mod_name}: {len(versions)} version(s) for {MC_VERSION}")
        if versions:
            v = versions[0]  # newest first
            print(f"    latest version_number = {v.get('version_number')}")
            print(f"    date_published        = {v.get('date_published')}")
            print(f"    version_type          = {v.get('version_type')}")
            files = v.get("files", [])
            if files:
                primary = next((f for f in files if f.get("primary")), files[0])
                print(f"    primary filename      = {primary.get('filename')}")
                print(f"    primary url           = {primary.get('url')}")
                print(f"    primary size (bytes)  = {primary.get('size')}")
            deps = v.get("dependencies", [])
            print(f"    dependencies          = {[d.get('project_id') for d in deps]}")
    elif versions is not None:
        print(f"\n  {mod_name}: no versions found for {MC_VERSION}/fabric")


# ── 4. Modrinth: project metadata ────────────────────────────────────────────

section("4. Modrinth project metadata (slug → ID, title, description)")

proj = fetch(
    f"https://api.modrinth.com/v2/project/{PROBE_MODS[0][1]}",
    f"{PROBE_MODS[0][0]} project",
)
if proj:
    print(f"\n  title        = {proj.get('title')}")
    print(f"  slug         = {proj.get('slug')}")
    print(f"  id           = {proj.get('id')}")
    print(f"  project_type = {proj.get('project_type')}")
    print(f"  game_versions (sample) = {proj.get('game_versions', [])[-5:]}")
    print(f"  loaders      = {proj.get('loaders')}")


# ── 5. Modrinth: batch project lookup ────────────────────────────────────────

section("5. Modrinth batch project lookup  (multiple IDs in one request)")

ids = [pid for _, pid in PROBE_MODS]
params = urllib.parse.urlencode({"ids": json.dumps(ids)})
projects = fetch(
    f"https://api.modrinth.com/v2/projects?{params}",
    "batch project lookup",
)
if projects:
    print(f"\n  Returned {len(projects)} project(s):")
    for p in projects:
        print(f"    {p.get('id'):12s}  slug={p.get('slug'):20s}  title={p.get('title')}")


print(f"\n{'━' * 60}")
print("  Probe complete.")
print("━" * 60)
