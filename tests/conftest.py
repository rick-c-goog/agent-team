"""Shared fixtures — notably a real, minimal PDF so PDF extraction is genuinely tested."""

import pytest


def make_pdf(pages: list[str]) -> bytes:
    """Build a minimal, valid, uncompressed one-font PDF with the given page texts."""
    objs, kids = [], []
    font_num = 3 + 2 * len(pages)
    for i, text in enumerate(pages):
        pnum = 3 + 2 * i
        cnum = pnum + 1
        kids.append(f"{pnum} 0 R")
        stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET"
        objs.append((pnum,
                     f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                     f"/Resources << /Font << /F1 {font_num} 0 R >> >> /Contents {cnum} 0 R >>"))
        objs.append((cnum, f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream"))
    objs.insert(0, (1, "<< /Type /Catalog /Pages 2 0 R >>"))
    objs.insert(1, (2, f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(pages)} >>"))
    objs.append((font_num, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"))

    out = b"%PDF-1.4\n"
    offsets = {}
    for num, body in sorted(objs):
        offsets[num] = len(out)
        out += f"{num} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref = len(out)
    n = max(offsets) + 1
    out += f"xref\n0 {n}\n0000000000 65535 f \n".encode()
    for i in range(1, n):
        out += f"{offsets.get(i, 0):010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {n} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


@pytest.fixture
def kb_dir(tmp_path):
    """A knowledge root holding one of each supported format."""
    (tmp_path / "cole").mkdir()
    (tmp_path / "cole" / "brand.md").write_text(
        "# Brand\nIntro line.\n\n## Tone\nClear, concrete, no hype.\n\n"
        "## Launches\nLaunch posts must include the registration link above the fold.\n"
    )
    (tmp_path / "cole" / "notes.txt").write_text(
        "First paragraph about webinars.\n\nSecond paragraph about newsletters.\n"
    )
    (tmp_path / "cole" / "tiers.csv").write_text(
        "tier,price,seats\nStarter,49,3\nGrowth,149,10\nScale,499,50\n"
    )
    (tmp_path / "cole" / "policy.pdf").write_bytes(
        make_pdf(["Refund policy is 30 days.", "Escalate wire transfers to a human."])
    )
    return tmp_path
