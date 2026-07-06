from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from playwright.sync_api import sync_playwright


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Inspect Chromium CacheStorage entries for a copied browser profile."
    )
    parser.add_argument("--source-profile", type=Path, required=True, help="Original browser profile directory.")
    parser.add_argument("--work-profile", type=Path, required=True, help="Temporary profile copy to inspect.")
    parser.add_argument("--origin", default="https://camp.codebn.cn/", help="Origin to open before reading CacheStorage.")
    parser.add_argument("--output", type=Path, required=True, help="JSON file to write inspection results.")
    return parser.parse_args()


def prepare_profile(source: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)

    def ignore_locked(_src: str, names: list[str]) -> list[str]:
        ignored = []
        for name in names:
            if name.endswith("-journal"):
                ignored.append(name)
                continue
            if name in {"LOCK", "lockfile"}:
                ignored.append(name)
        return ignored

    shutil.copytree(source, destination, ignore=ignore_locked)


def main() -> int:
    args = parse_args()
    prepare_profile(args.source_profile, args.work_profile)
    args.output.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(args.work_profile),
            headless=True,
        )
        page = context.new_page()
        page.goto(args.origin, wait_until="networkidle", timeout=60000)

        result = page.evaluate(
            """async () => {
                const output = {
                  location: window.location.href,
                  cacheNames: [],
                  entries: {},
                };

                if (!('caches' in window)) {
                  output.error = 'CacheStorage API unavailable';
                  return output;
                }

                const names = await caches.keys();
                output.cacheNames = names;

                for (const name of names) {
                  const cache = await caches.open(name);
                  const requests = await cache.keys();
                  output.entries[name] = requests.map((req) => req.url);
                }

                return output;
            }"""
        )
        context.close()

    with args.output.open("w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
