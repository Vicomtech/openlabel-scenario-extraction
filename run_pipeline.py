#!/usr/bin/env python3
"""
GraphDB + VCD Hybrid Pipeline.

Workflow:
1. Automatic Setup: Starts Docker, creates the Repository, and imports the Ontology (Direct Upload).
2. Semi-Automatic Data Ingestion:
   - The script runs preload.py (generates data).
   - It MOVES the generated file to the 'graphdb-import' folder.
   - It PAUSES and asks you to import the file manually via GraphDB Server Files UI.
   - Once you press ENTER, it proceeds to the next step (queries.py).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
import shutil
import os
from pathlib import Path
from typing import Any, Dict, Optional

import requests
import yaml

DEFAULT_WAIT_SECONDS = 120
DEFAULT_HTTP_TIMEOUT = 300


def load_config(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def normalize_base_url(url: str) -> str:
    return url.rstrip("/")


def resolve_path(root: Path, value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else (root / p)


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def wait_for_graphdb(base_url: str, wait_seconds: int) -> bool:
    deadline = time.time() + wait_seconds
    url = f"{base_url}/rest/repositories"
    while time.time() < deadline:
        try:
            resp = requests.get(url, timeout=5)
            if resp.ok:
                return True
        except requests.RequestException:
            pass
        time.sleep(2)
    return False


def run_cmd(args: list[str], cwd: Path) -> None:
    print(f"Executing: {' '.join(args)}")
    result = subprocess.run(args, cwd=str(cwd))
    if result.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(args)}")


def get_graphdb_url(cfg: Dict[str, Any]) -> str:
    db_cfg = cfg.get("database", {})
    db_ip = db_cfg.get("db_ip")
    db_port = db_cfg.get("db_port")
    if db_ip and db_port:
        return f"http://{db_ip}:{db_port}"
    api_cfg = cfg.get("api", {})
    api_url = api_cfg.get("graphdb_url")
    if api_url:
        return api_url
    return "http://localhost:7200"


def get_repo_id(cfg: Dict[str, Any]) -> Optional[str]:
    db_cfg = cfg.get("database", {})
    repo = db_cfg.get("repository")
    if repo:
        return repo
    api_cfg = cfg.get("api", {})
    return api_cfg.get("repository_name")


def list_repositories(base_url: str) -> list[Dict[str, Any]]:
    resp = requests.get(f"{base_url}/rest/repositories", timeout=10)
    resp.raise_for_status()
    return resp.json()


def repository_exists(base_url: str, repo_id: str) -> bool:
    repos = list_repositories(base_url)
    for repo in repos:
        if repo.get("id") == repo_id or repo.get("repoId") == repo_id:
            return True
    return False


def build_repo_config(repo_id: str) -> str:
    return f"""@prefix rep: <http://www.openrdf.org/config/repository#>.
@prefix sr: <http://www.openrdf.org/config/repository/sail#>.
@prefix sail: <http://www.openrdf.org/config/sail#>.
@prefix graphdb: <http://www.ontotext.com/config/graphdb#>.

[] a rep:Repository ;
   rep:repositoryID "{repo_id}" ;
   rep:repositoryImpl [
      rep:repositoryType "graphdb:SailRepository" ;
      sr:sailImpl [
         sail:sailType "graphdb:Sail" ;
         graphdb:ruleset "rdfsplus-optimized" ;
         graphdb:storage-folder "storage" ;
         graphdb:context-index "true" ;
         graphdb:enable-predicate-list "true" ;
         graphdb:base-URL "http://example.org/" ;
      ]
   ].
"""


def create_repository(base_url: str, repo_id: str) -> None:
    ttl = build_repo_config(repo_id)
    resp = requests.post(
        f"{base_url}/rest/repositories",
        files={"config": ("repo-config.ttl", ttl, "text/turtle")},
        timeout=10,
    )
    if not resp.ok:
        raise RuntimeError(f"Failed to create repository '{repo_id}': {resp.status_code} {resp.text}")


def guess_content_type(path: Path) -> str:
    suffix = path.suffix.lower()
    mapping = {
        ".rdf": "application/rdf+xml",
        ".owl": "application/rdf+xml",
        ".xml": "application/rdf+xml",
        ".ttl": "text/turtle",
        ".nq": "application/n-quads",
        ".nt": "application/n-triples",
    }
    return mapping.get(suffix, "application/octet-stream")


def import_rdf_file_direct(
    base_url: str,
    repo_id: str,
    file_path: Path,
    content_type: str,
    http_timeout: int,
) -> None:
    """
    Direct HTTP upload (Used only for the Ontology).
    """
    url = f"{base_url}/repositories/{repo_id}/statements"
    headers = {"Content-Type": content_type}
    
    with file_path.open("rb") as fh:
        resp = requests.post(
            url,
            data=fh,
            headers=headers,
            timeout=(10, http_timeout),
        )
    if not resp.ok:
        raise RuntimeError(f"Direct import failed for {file_path.name}: {resp.status_code} {resp.text}")


def move_and_pause(src_path: Path, import_dir: Path, workbench_url: str) -> None:
    """
    1. Moves the file to vol/graphdb-import.
    2. Pauses execution requiring user manual action.
    """
    if not src_path.exists():
        raise FileNotFoundError(f"Source file not found: {src_path}")
    
    dest_path = import_dir / src_path.name
    
    # If destination exists, remove it first to avoid errors
    if dest_path.exists():
        os.remove(dest_path)

    print(f"  -> MOVING {src_path.name} to graphdb-import...")
    shutil.move(src_path, dest_path)
    
    # --- MANUAL PAUSE MESSAGE ---
    print("\n" + "="*70)
    print(f"WARNING: MANUAL ACTION REQUIRED FOR: {src_path.name}")
    print("="*70)
    print(f"1. Go to GraphDB Workbench: {workbench_url}")
    print(f"2. Navigate to: Import -> Server Files")
    print(f"3. Select '{src_path.name}' and click 'Import'")
    print(f"4. Wait for the import progress bar to finish.")
    print("="*70)
    
    # Pauses the script here
    input(">>> Once the import is finished, PRESS ENTER here to continue... <<<")
    print("Resuming pipeline...\n")


def find_latest_file(root: Path, patterns: list[str]) -> Optional[Path]:
    candidates: list[Path] = []
    for pattern in patterns:
        candidates.extend(root.glob(pattern))
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Synergies pipeline end-to-end")
    parser.add_argument("--config", default="conf.yaml", help="Path to conf.yaml")
    parser.add_argument("--no-docker", action="store_true", help="Do not start docker compose")
    parser.add_argument("--skip-ontology", action="store_true", help="Skip ontology import")
    parser.add_argument("--skip-preload", action="store_true", help="Skip preload.py")
    parser.add_argument("--skip-queries", action="store_true", help="Skip queries.py")
    parser.add_argument("--graphdb-url", help="Override GraphDB base URL")
    parser.add_argument("--repo", help="Override repository ID")
    parser.add_argument("--wait-seconds", type=int, default=DEFAULT_WAIT_SECONDS)
    parser.add_argument("--http-timeout", type=int, default=DEFAULT_HTTP_TIMEOUT)
    return parser.parse_args()


def prompt_preload_mode() -> str:
    print("\nChoose preload.py execution mode:")
    print("1) Multiprocessing (faster, WARNING: may fail if you don't have much RAM)")
    print("2) Sequential (slower but more stable)")
    choice = input("Option [1/2] (default 1): ").strip()
    return "seq" if choice == "2" else "mp"


def main() -> int:
    args = parse_args()

    config_path = Path(args.config).resolve()
    if not config_path.exists():
        print(f"Config not found: {config_path}")
        return 1

    root = config_path.parent
    cfg = load_config(config_path)

    graphdb_import_dir = root / "vol" / "graphdb-import"
    ensure_dir(graphdb_import_dir)
    print(f"[OK] graphdb-import ready: {graphdb_import_dir}")

    base_url = normalize_base_url(args.graphdb_url or get_graphdb_url(cfg))
    repo_id = args.repo or get_repo_id(cfg)
    
    if not repo_id:
        print("Repository ID not found in config. Use --repo to set it.")
        return 1

    # 1. DOCKER
    if not wait_for_graphdb(base_url, wait_seconds=5):
        if args.no_docker:
            print(f"GraphDB not reachable at {base_url}. Start it or remove --no-docker.")
            return 1
        print("GraphDB not reachable. Starting docker compose...")
        run_cmd(["docker", "compose", "up", "-d"], cwd=root)

    if not wait_for_graphdb(base_url, args.wait_seconds):
        print(f"GraphDB not reachable after {args.wait_seconds}s at {base_url}")
        return 1

    # 2. REPO
    if not repository_exists(base_url, repo_id):
        print(f"Repository '{repo_id}' not found. Creating...")
        create_repository(base_url, repo_id)
        print(f"[OK] Repository created: {repo_id}")
    else:
        print(f"[OK] Repository exists: {repo_id}")

    # 3. ONTOLOGY (Automatic Direct Import)
    if not args.skip_ontology:
        ontology_path_value = cfg.get("ontology", {}).get("ontology_path", "./Synergies.rdf")
        ontology_path = resolve_path(root, ontology_path_value)
        
        if not ontology_path.exists():
            print(f"Ontology file not found: {ontology_path}")
            return 1

        print(f"Importing ontology directly: {ontology_path.name}")
        import_rdf_file_direct(
            base_url,
            repo_id,
            ontology_path,
            guess_content_type(ontology_path),
            args.http_timeout,
        )
        print("[OK] Ontology imported")

    # 4. PRELOAD (Generate -> Move -> Manual Pause)
    if not args.skip_preload:
        print("\n--- Running preload.py ---")
        mode = prompt_preload_mode()
        run_cmd([sys.executable, "preload.py", "--mode", mode], cwd=root)

        nq_name = cfg.get("outputs", {}).get("nq_file", "Synergies.nq")
        nq_path = resolve_path(root, nq_name)
        if not nq_path.exists():
            nq_path = find_latest_file(
                root,
                [
                    "Synergies.nq",
                    "Synergies_partners.nq",
                    "*.nq",
                ],
            )
        if not nq_path:
            print("No .nq output file found after preload.py")
            return 1
        move_and_pause(nq_path, graphdb_import_dir, base_url)
        
        print("[OK] Preload step verified by user.")

    # 5. QUERIES (Generate -> Move -> Manual Pause)
    if not args.skip_queries:
        print("\n--- Running queries.py ---")
        run_cmd([sys.executable, "queries.py"], cwd=root)

        nt_name = cfg.get("outputs", {}).get("queries_file", "queries_Synergies.nt")
        nt_path = resolve_path(root, nt_name)
        if not nt_path.exists():
            nt_path = find_latest_file(
                root,
                [
                    "queries_Synergies.nt",
                    "queries_SynergiesPartners.nt",
                    "queries_*.nt",
                ],
            )
        if not nt_path:
            print("No .nt output file found after queries.py")
            return 1
        move_and_pause(nt_path, graphdb_import_dir, base_url)

        print("[OK] Queries step verified by user.")
        parser_path = root / "graphdb_to_vcd_parser.py"
        if parser_path.exists():
            resp = input("Do you want to export actions/events back to VCDs? [y/N]: ").strip().lower()
            if resp in ("y", "yes"):
                print("\n--- Running graphdb_to_vcd_parser.py ---")
                run_cmd([sys.executable, str(parser_path)], cwd=root)
                print("[OK] VCD export completed.")
            else:
                print("Skipping VCD export.")
        else:
            print("[WARN] graphdb_to_vcd_parser.py not found; skipping VCD export.")

    print("\nPipeline finished successfully")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
