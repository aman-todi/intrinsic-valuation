#!/usr/bin/env python
"""Seed s3://<bucket>/damodaran/ with parsed Damodaran datasets (spec §14.6).

Downloads betas.xls, margin.xls and histimpl.xls from pages.stern.nyu.edu/~adamodar/pc/datasets/,
parses them, and uploads parsed JSON to damodaran/{dataset}/{fetched_at_iso}.json. Run once at setup
and again a few times a year (no automatic refresh by design).

If the site blocks the download, fetch the files in a browser and use --from-local-dir DIR (the
directory must contain betas.xls|.xlsx, margin.xls|.xlsx, histimpl.xls|.xlsx).

Usage (from backend/, with the backend venv):
    .venv/bin/python ../infra/scripts/seed_damodaran_cache.py --dry-run
    .venv/bin/python ../infra/scripts/seed_damodaran_cache.py --from-local-dir ~/Downloads/damodaran
    .venv/bin/python ../infra/scripts/seed_damodaran_cache.py --bucket my-bucket
"""

import argparse
import asyncio
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.data.macro.damodaran import DATASETS, DamodaranUnavailable, seed_cache  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--from-local-dir", type=Path, help="read manually downloaded .xls/.xlsx files from here")
    p.add_argument("--dry-run", action="store_true", help="parse and print counts; do not upload to S3")
    p.add_argument("--bucket", help="S3 bucket (default: settings.S3_BUCKET_NAME)")
    p.add_argument("--dataset", action="append", choices=sorted(DATASETS), help="limit to dataset(s)")
    return p


async def run(args: argparse.Namespace) -> int:
    try:
        results = await seed_cache(
            from_local_dir=args.from_local_dir,
            dry_run=args.dry_run,
            datasets=args.dataset,
            bucket=args.bucket,
        )
    except DamodaranUnavailable as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        if args.from_local_dir is None:
            print("Hint: download the files in a browser and re-run with --from-local-dir.", file=sys.stderr)
        return 1
    for r in results:
        extra = (
            f" implied_erp={r.summary['implied_erp']:.4f} (year {r.summary['year']})"
            if "implied_erp" in r.summary
            else ""
        )
        dest = "(dry run, not uploaded)" if r.s3_key is None else f"-> {r.s3_key}"
        print(f"{r.dataset:<9} rows={r.rows:<4}{extra} from {r.source} {dest}")
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(build_parser().parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
