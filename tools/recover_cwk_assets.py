from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests


DEFAULT_MAIN_REPO = Path(r"D:\kaifa\camp-pk-system-git")
DEFAULT_OUTPUT_ROOT = DEFAULT_MAIN_REPO / "public" / "pet-mirror" / "cwk"
DEFAULT_WORK_ROOT = Path(r"D:\kaifa\Scrapling\recovery-work")
DEFAULT_CAMP_API = "https://camp.codebn.cn/api/pets"
DEFAULT_SOURCE_API = "https://bancxq.cn/api/pet/list"
DEFAULT_OSS_BASE = "https://bcxq.oss-cn-beijing.aliyuncs.com"
SUPABASE_PATH_MARKER = "/storage/v1/object/public/cwk/"


@dataclass(frozen=True)
class StageTask:
    pet_id: int | None
    pet_name: str
    asset_key: str
    stage_index: int
    expected_filename: str
    expected_supabase_url: str
    direct_oss_url: str
    fallback_source_url: str | None

    @property
    def relative_path(self) -> Path:
        return Path(self.asset_key) / self.expected_filename


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Recover camp pet assets by reading current live camp pet URLs, "
            "then downloading matching files from bancxq OSS/source list into local pet-mirror."
        )
    )
    parser.add_argument("--main-repo", type=Path, default=DEFAULT_MAIN_REPO)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--work-root", type=Path, default=DEFAULT_WORK_ROOT)
    parser.add_argument("--camp-api", default=DEFAULT_CAMP_API)
    parser.add_argument("--source-api", default=DEFAULT_SOURCE_API)
    parser.add_argument("--oss-base", default=DEFAULT_OSS_BASE)
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--timeout-seconds", type=int, default=25)
    parser.add_argument("--skip-download", action="store_true")
    return parser.parse_args()


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


def fetch_json(url: str, timeout_seconds: int) -> Any:
    response = requests.get(url, timeout=timeout_seconds)
    response.raise_for_status()
    return response.json()


def parse_supabase_cwk_url(url: str) -> tuple[str, str] | None:
    if not isinstance(url, str) or SUPABASE_PATH_MARKER not in url:
        return None
    remainder = url.split(SUPABASE_PATH_MARKER, 1)[1].strip("/")
    if not remainder or "/" not in remainder:
        return None
    asset_key, filename = remainder.split("/", 1)
    filename = Path(filename).name
    if not asset_key or not filename:
        return None
    return asset_key, filename


def parse_source_key_from_url(url: str) -> str | None:
    if not isinstance(url, str) or not url.startswith("http"):
        return None
    parts = [part for part in urlparse(url).path.split("/") if part]
    if not parts:
        return None
    return parts[0]


def parse_filename_from_url(url: str) -> str | None:
    if not isinstance(url, str) or not url.startswith("http"):
        return None
    filename = Path(urlparse(url).path).name
    return filename if filename else None


def build_source_map(source_payload: Any) -> dict[str, dict[str, Any]]:
    source_list = source_payload.get("data") if isinstance(source_payload, dict) else None
    if not isinstance(source_list, list):
        raise ValueError("Source API did not return a list under data.")

    source_map: dict[str, dict[str, Any]] = {}
    for item in source_list:
        if not isinstance(item, dict):
            continue
        image = item.get("image")
        key = parse_source_key_from_url(image)
        if not key:
            continue
        stages = [
            stage
            for stage in (item.get("evolutionStages") or [])
            if isinstance(stage, str) and stage.startswith("http")
        ]
        filename_to_url: dict[str, str] = {}
        for candidate in stages:
            filename = parse_filename_from_url(candidate)
            if filename:
                filename_to_url[filename.lower()] = candidate
        if isinstance(image, str):
            filename = parse_filename_from_url(image)
            if filename:
                filename_to_url[filename.lower()] = image
        source_map[key] = {
            "name": item.get("name") or key,
            "image": image if isinstance(image, str) else None,
            "stages": stages,
            "filename_to_url": filename_to_url,
        }
    return source_map


def select_fallback_url(
    expected_filename: str,
    stage_index: int,
    source_info: dict[str, Any],
) -> str | None:
    source_stages: list[str] = source_info.get("stages") or []
    source_image = source_info.get("image")
    filename_to_url: dict[str, str] = source_info.get("filename_to_url") or {}

    lower_name = expected_filename.lower()
    exact = filename_to_url.get(lower_name)
    if exact:
        return exact

    # Common BancXQ rename: xxx10.jpg -> xxx.jpg
    stem = Path(expected_filename).stem
    suffix = Path(expected_filename).suffix
    match = re.match(r"^(.*?)(10)$", stem)
    if match:
        alt_name = f"{match.group(1)}{suffix}".lower()
        alt = filename_to_url.get(alt_name)
        if alt:
            return alt

    if stage_index < len(source_stages):
        return source_stages[stage_index]

    if isinstance(source_image, str):
        return source_image

    return None


def build_stage_tasks(
    camp_pets: Any,
    source_map: dict[str, dict[str, Any]],
    oss_base: str,
) -> list[StageTask]:
    if not isinstance(camp_pets, list):
        raise ValueError("Camp API did not return a pet list.")

    tasks: list[StageTask] = []
    seen_paths: set[str] = set()

    for pet in camp_pets:
        if not isinstance(pet, dict):
            continue

        raw_stages = pet.get("evolutionStages") or []
        if not isinstance(raw_stages, list):
            continue

        parsed_stages: list[tuple[str, str, str]] = []
        for raw_stage in raw_stages:
            parsed = parse_supabase_cwk_url(raw_stage)
            if not parsed:
                continue
            asset_key, filename = parsed
            parsed_stages.append((raw_stage, asset_key, filename))

        if not parsed_stages:
            continue

        primary_key = parsed_stages[0][1]
        source_info = source_map.get(primary_key, {})

        for idx, (supabase_url, asset_key, filename) in enumerate(parsed_stages):
            # Keep the original expected filename so local mirror matches current camp catalog URLs.
            relative_path = f"{asset_key}/{filename}".lower()
            if relative_path in seen_paths:
                continue
            seen_paths.add(relative_path)

            fallback_url = select_fallback_url(
                expected_filename=filename,
                stage_index=idx,
                source_info=source_info,
            )

            tasks.append(
                StageTask(
                    pet_id=pet.get("id") if isinstance(pet.get("id"), int) else None,
                    pet_name=str(pet.get("name") or asset_key),
                    asset_key=asset_key,
                    stage_index=idx,
                    expected_filename=filename,
                    expected_supabase_url=supabase_url,
                    direct_oss_url=f"{oss_base.rstrip('/')}/{asset_key}/{filename}",
                    fallback_source_url=fallback_url,
                )
            )

    return tasks


def download_binary(url: str, timeout_seconds: int) -> tuple[int, bytes]:
    response = requests.get(url, timeout=timeout_seconds)
    status = response.status_code
    if status != 200:
        return status, b""
    return status, response.content


def run_download_task(
    task: StageTask,
    output_root: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    output_path = output_root / task.relative_path
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if output_path.exists() and output_path.stat().st_size > 0:
        return {
            "status": "skipped_existing",
            "pet_id": task.pet_id,
            "pet_name": task.pet_name,
            "asset_key": task.asset_key,
            "stage_index": task.stage_index,
            "filename": task.expected_filename,
            "relative_path": task.relative_path.as_posix(),
            "source_url": None,
            "output_path": str(output_path),
            "size": output_path.stat().st_size,
        }

    status, data = download_binary(task.direct_oss_url, timeout_seconds=timeout_seconds)
    if status == 200 and data:
        output_path.write_bytes(data)
        return {
            "status": "downloaded_direct",
            "pet_id": task.pet_id,
            "pet_name": task.pet_name,
            "asset_key": task.asset_key,
            "stage_index": task.stage_index,
            "filename": task.expected_filename,
            "relative_path": task.relative_path.as_posix(),
            "source_url": task.direct_oss_url,
            "output_path": str(output_path),
            "size": len(data),
        }

    fallback_url = task.fallback_source_url
    if fallback_url and fallback_url != task.direct_oss_url:
        fb_status, fb_data = download_binary(fallback_url, timeout_seconds=timeout_seconds)
        if fb_status == 200 and fb_data:
            output_path.write_bytes(fb_data)
            return {
                "status": "downloaded_fallback",
                "pet_id": task.pet_id,
                "pet_name": task.pet_name,
                "asset_key": task.asset_key,
                "stage_index": task.stage_index,
                "filename": task.expected_filename,
                "relative_path": task.relative_path.as_posix(),
                "source_url": fallback_url,
                "output_path": str(output_path),
                "size": len(fb_data),
                "direct_status": status,
            }

    return {
        "status": "failed",
        "pet_id": task.pet_id,
        "pet_name": task.pet_name,
        "asset_key": task.asset_key,
        "stage_index": task.stage_index,
        "filename": task.expected_filename,
        "relative_path": task.relative_path.as_posix(),
        "direct_url": task.direct_oss_url,
        "fallback_url": fallback_url,
        "direct_status": status,
    }


def summarize(tasks: list[StageTask], results: list[dict[str, Any]]) -> dict[str, Any]:
    by_status: dict[str, int] = {}
    for item in results:
        status = str(item.get("status", "unknown"))
        by_status[status] = by_status.get(status, 0) + 1

    failed = [item for item in results if item.get("status") == "failed"]
    fallback = [item for item in results if item.get("status") == "downloaded_fallback"]

    return {
        "total_tasks": len(tasks),
        "downloaded_direct": by_status.get("downloaded_direct", 0),
        "downloaded_fallback": by_status.get("downloaded_fallback", 0),
        "skipped_existing": by_status.get("skipped_existing", 0),
        "failed": len(failed),
        "fallback_count": len(fallback),
        "failed_samples": failed[:20],
    }


def main() -> int:
    args = parse_args()
    args.work_root.mkdir(parents=True, exist_ok=True)
    args.output_root.mkdir(parents=True, exist_ok=True)

    camp_payload = fetch_json(args.camp_api, timeout_seconds=args.timeout_seconds)
    source_payload = fetch_json(args.source_api, timeout_seconds=args.timeout_seconds)

    source_map = build_source_map(source_payload)
    tasks = build_stage_tasks(camp_payload, source_map, oss_base=args.oss_base)

    plan_payload = [
        {
            "pet_id": task.pet_id,
            "pet_name": task.pet_name,
            "asset_key": task.asset_key,
            "stage_index": task.stage_index,
            "expected_filename": task.expected_filename,
            "relative_path": task.relative_path.as_posix(),
            "expected_supabase_url": task.expected_supabase_url,
            "direct_oss_url": task.direct_oss_url,
            "fallback_source_url": task.fallback_source_url,
        }
        for task in tasks
    ]
    write_json(args.work_root / "cwk-recovery-plan.json", plan_payload)

    results: list[dict[str, Any]] = []
    if not args.skip_download:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
            futures = [
                executor.submit(
                    run_download_task,
                    task,
                    args.output_root,
                    args.timeout_seconds,
                )
                for task in tasks
            ]
            for future in concurrent.futures.as_completed(futures):
                results.append(future.result())

    write_json(args.work_root / "recovered-assets.json", results)
    summary = summarize(tasks, results)
    summary["output_root"] = str(args.output_root)
    summary["camp_api"] = args.camp_api
    summary["source_api"] = args.source_api
    write_json(args.work_root / "summary.json", summary)

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
