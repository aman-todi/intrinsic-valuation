"""Ticket 4: infra/scripts/seed_damodaran_cache.py CLI (dry run against local fixtures)."""

import importlib.util
from pathlib import Path

import httpx
import respx

from app.data.macro import damodaran

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "infra" / "scripts" / "seed_damodaran_cache.py"
FIXTURES = Path(__file__).parents[1] / "fixtures" / "damodaran"


def load_script():
    spec = importlib.util.spec_from_file_location("seed_damodaran_cache", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_dry_run_from_local_dir_prints_counts(capsys):
    rc = load_script().main(["--from-local-dir", str(FIXTURES), "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "betas" in out and "rows=6" in out
    assert "margin" in out and "rows=5" in out
    assert "implied_erp=0.0423" in out
    assert "dry run" in out


@respx.mock
def test_download_blocked_returns_error_with_hint(capsys):
    respx.get(damodaran.DAMODARAN_BASE_URL + "betas.xls").mock(return_value=httpx.Response(403))
    rc = load_script().main(["--dry-run", "--dataset", "betas"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "403" in err and "--from-local-dir" in err
