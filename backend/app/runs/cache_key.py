"""Filing-pinned cache key (spec §8.5)."""

import hashlib


def compute_cache_key(
    ticker: str, model_type: str, accession_number: str, engine_version: str, prompt_version: str
) -> str:
    raw = f"{ticker.upper()}|{model_type}|{accession_number}|{engine_version}|{prompt_version}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def compute_proposal_cache_key(
    ticker: str, model_type: str, accession_number: str, prompt_version: str
) -> str:
    raw = f"proposal|{ticker.upper()}|{model_type}|{accession_number}|{prompt_version}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]
