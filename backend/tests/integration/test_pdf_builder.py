"""Builds a real PDF for every model-type fixture (spec §13.1 test_pdf_builder).

Needs only WeasyPrint's system libraries (Pango/Cairo); skips cleanly when they can't load.
"""

import io

import pytest
from pypdf import PdfReader

from app.export.pdf.builder import build_pdf
from app.export.pdf.formatting import fmt_per_share
from app.export.pdf.narrative import fallback_narrative
from tests.fixtures.valuation_results import ALL_MODEL_TYPES, COMPANY_NAMES, MODEL_REASONS, fixture_result


def _weasyprint_unavailable() -> str | None:
    try:
        from weasyprint import HTML

        HTML(string="<p>probe</p>").write_pdf()
    except (ImportError, OSError) as exc:  # missing Pango/Cairo shared libraries
        return f"WeasyPrint system libraries unavailable: {exc}"
    return None


_SKIP_REASON = _weasyprint_unavailable()
needs_weasyprint = pytest.mark.skipif(_SKIP_REASON is not None, reason=_SKIP_REASON or "")


def _text(pdf: bytes) -> str:
    return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(pdf)).pages)


@needs_weasyprint
@pytest.mark.parametrize("model_type", ALL_MODEL_TYPES, ids=lambda m: m.value)
def test_builds_pdf_for_each_model(model_type, tmp_path):
    result = fixture_result(model_type)
    name = COMPANY_NAMES[model_type]
    narrative = fallback_narrative(result, name, MODEL_REASONS[model_type])
    out = tmp_path / f"{model_type.value}.pdf"

    build_pdf(result, narrative, out, company_name=name, model_reasons=MODEL_REASONS[model_type])

    pdf = out.read_bytes()
    assert pdf[:4] == b"%PDF"
    assert len(pdf) > 20_000
    assert len(PdfReader(io.BytesIO(pdf)).pages) >= 5
    text = _text(pdf)
    flat = " ".join(text.split())
    assert result.ticker in text
    assert fmt_per_share(result.value_per_share) in text
    assert "not investment advice" in text.lower()
    for source in result.sources:
        assert source in flat
