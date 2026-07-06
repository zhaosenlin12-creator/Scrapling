from __future__ import annotations

import argparse
import concurrent.futures
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests


DEFAULT_MAIN_REPO = Path(r"D:\kaifa\camp-pk-system-git")
DEFAULT_OUTPUT_ROOT = DEFAULT_MAIN_REPO / "public" / "pet-mirror" / "cwk"
DEFAULT_SNAPSHOT_OUT = DEFAULT_MAIN_REPO / "server" / "classPetCatalogBancxq95.json"
DEFAULT_WORK_ROOT = Path(r"D:\kaifa\Scrapling\recovery-work")

DEFAULT_SOURCE_API = "https://bancxq.cn/api/pet/list"
DEFAULT_SUPABASE_BASE = "https://brvjqiusgeqeyfadfwga.supabase.co/storage/v1/object/public/cwk"


@dataclass(frozen=True)
class DownloadTask:
    asset_key: str
    filename: str
    source_url: str

    @property
    def relative_path(self) -> Path:
        return Path(self.asset_key) / self.filename


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Sync full 95-pet catalog from bancxq source, write local snapshot JSON, "
            "and mirror all stage images into public/pet-mirror/cwk."
        )
    )
    parser.add_argument("--source-api", default=DEFAULT_SOURCE_API)
    parser.add_argument("--main-repo", type=Path, default=DEFAULT_MAIN_REPO)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--snapshot-out", type=Path, default=DEFAULT_SNAPSHOT_OUT)
    parser.add_argument("--work-root", type=Path, default=DEFAULT_WORK_ROOT)
    parser.add_argument("--supabase-base", default=DEFAULT_SUPABASE_BASE)
    parser.add_argument("--workers", type=int, default=20)
    parser.add_argument("--timeout-seconds", type=int, default=30)
    parser.add_argument("--skip-download", action="store_true")
    return parser.parse_args()


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


def fetch_source_pets(url: str, timeout_seconds: int) -> list[dict[str, Any]]:
    response = requests.get(url, timeout=timeout_seconds)
    response.raise_for_status()
    payload = response.json()
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise ValueError("Source API response missing data list.")
    return [item for item in data if isinstance(item, dict)]


def parse_asset_from_url(url: str) -> tuple[str, str] | None:
    if not isinstance(url, str):
        return None
    cleaned = url.strip()
    if not cleaned.startswith("http"):
        return None
    parts = [part for part in urlparse(cleaned).path.split("/") if part]
    if len(parts) < 2:
        return None
    asset_key = parts[-2].strip()
    filename = parts[-1].strip()
    if not asset_key or not filename:
        return None
    return asset_key, filename


def to_supabase_style_url(asset_key: str, filename: str, supabase_base: str) -> str:
    return f"{supabase_base.rstrip('/')}/{asset_key}/{filename}"


def build_catalog_and_tasks(
    source_pets: list[dict[str, Any]],
    supabase_base: str,
) -> tuple[list[dict[str, Any]], list[DownloadTask], dict[str, Any]]:
    snapshot: list[dict[str, Any]] = []
    task_map: dict[str, DownloadTask] = {}
    invalid_pets: list[dict[str, Any]] = []

    for pet in source_pets:
        image = pet.get("image")
        image_asset = parse_asset_from_url(image)
        if not image_asset:
            invalid_pets.append(
                {
                    "reason": "invalid_image",
                    "name": pet.get("name"),
                    "id": pet.get("id"),
                    "image": image,
                }
            )
            continue

        image_key, image_filename = image_asset
        source_stages = [
            stage
            for stage in (pet.get("evolutionStages") or [])
            if isinstance(stage, str) and stage.startswith("http")
        ]

        converted_stages: list[str] = []
        stage_tasks: list[DownloadTask] = []
        for stage_url in source_stages:
            cleaned_stage_url = stage_url.strip()
            parsed = parse_asset_from_url(cleaned_stage_url)
            if not parsed:
                continue
            stage_key, stage_filename = parsed
            converted_stages.append(to_supabase_style_url(stage_key, stage_filename, supabase_base))
            stage_tasks.append(DownloadTask(asset_key=stage_key, filename=stage_filename, source_url=cleaned_stage_url))

        # Keep at least one stage so server-side normalization is stable.
        if not converted_stages:
            converted_stages = [to_supabase_style_url(image_key, image_filename, supabase_base)]
            stage_tasks = [DownloadTask(asset_key=image_key, filename=image_filename, source_url=image)]

        # Ensure image file itself is mirrored, even if not explicitly present in stage list.
        image_task = DownloadTask(asset_key=image_key, filename=image_filename, source_url=str(image).strip())
        stage_tasks.insert(0, image_task)

        for task in stage_tasks:
            task_map[task.relative_path.as_posix().lower()] = task

        snapshot.append(
            {
                "sourceId": pet.get("id"),
                "name": pet.get("name"),
                "type": pet.get("type"),
                "breed": pet.get("breed") or "Cloud Pet",
                "assetKey": image_key,
                "image": to_supabase_style_url(image_key, image_filename, supabase_base),
                "evolutionStages": converted_stages,
            }
        )

    audit = {
        "source_pet_count": len(source_pets),
        "snapshot_pet_count": len(snapshot),
        "unique_asset_keys": len({item["assetKey"] for item in snapshot}),
        "invalid_pet_count": len(invalid_pets),
        "invalid_pets": invalid_pets,
    }
    return snapshot, list(task_map.values()), audit


def download_one(task: DownloadTask, output_root: Path, timeout_seconds: int) -> dict[str, Any]:
    output_path = output_root / task.relative_path
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if output_path.exists() and output_path.stat().st_size > 0:
        return {
            "status": "skipped_existing",
            "relative_path": task.relative_path.as_posix(),
            "source_url": task.source_url,
            "size": output_path.stat().st_size,
            "output_path": str(output_path),
        }

    response = requests.get(task.source_url, timeout=timeout_seconds)
    if response.status_code != 200:
        return {
            "status": "failed",
            "relative_path": task.relative_path.as_posix(),
            "source_url": task.source_url,
            "status_code": response.status_code,
        }

    output_path.write_bytes(response.content)
    return {
        "status": "downloaded",
        "relative_path": task.relative_path.as_posix(),
        "source_url": task.source_url,
        "size": len(response.content),
        "output_path": str(output_path),
    }


def verify_snapshot_files(snapshot: list[dict[str, Any]], output_root: Path) -> dict[str, Any]:
    missing: list[str] = []
    expected_files = 0
    for pet in snapshot:
        stages = pet.get("evolutionStages") or []
        for stage in stages:
            parsed = None
            if isinstance(stage, str) and "/storage/v1/object/public/cwk/" in stage:
                remainder = stage.split("/storage/v1/object/public/cwk/", 1)[1]
                parts = [part for part in remainder.split("/") if part]
                if len(parts) >= 2:
                    parsed = (parts[0], parts[1])
            if not parsed:
                continue
            expected_files += 1
            path = output_root / parsed[0] / parsed[1]
            if not path.exists():
                missing.append(f"{parsed[0]}/{parsed[1]}")
    return {
        "expected_stage_files": expected_files,
        "missing_stage_files": len(missing),
        "missing_samples": missing[:30],
    }


def summarize_download_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    summary = {"downloaded": 0, "skipped_existing": 0, "failed": 0}
    failed_samples: list[dict[str, Any]] = []
    for item in results:
        status = item.get("status")
        if status in summary:
            summary[status] += 1
        if status == "failed" and len(failed_samples) < 30:
            failed_samples.append(item)
    summary["failed_samples"] = failed_samples
    summary["total_tasks"] = len(results)
    return summary


def main() -> int:
    args = parse_args()
    args.output_root.mkdir(parents=True, exist_ok=True)
    args.work_root.mkdir(parents=True, exist_ok=True)

    source_pets = fetch_source_pets(args.source_api, timeout_seconds=args.timeout_seconds)
    snapshot, tasks, audit = build_catalog_and_tasks(
        source_pets=source_pets,
        supabase_base=args.supabase_base,
    )

    write_json(args.snapshot_out, snapshot)
    write_json(args.work_root / "bancxq-source-pets.json", source_pets)
    write_json(args.work_root / "classPetCatalogBancxq95.preview.json", snapshot)

    results: list[dict[str, Any]] = []
    if not args.skip_download:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
            futures = [
                executor.submit(download_one, task, args.output_root, args.timeout_seconds)
                for task in tasks
            ]
            for future in concurrent.futures.as_completed(futures):
                results.append(future.result())

    download_summary = summarize_download_results(results)
    verify_summary = verify_snapshot_files(snapshot, args.output_root)

    final_summary = {
        "audit": audit,
        "download": download_summary,
        "verify": verify_summary,
        "snapshot_out": str(args.snapshot_out),
        "output_root": str(args.output_root),
        "source_api": args.source_api,
    }
    write_json(args.work_root / "bancxq-full-sync-results.json", results)
    write_json(args.work_root / "bancxq-full-sync-summary.json", final_summary)
    print(json.dumps(final_summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
