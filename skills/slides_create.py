"""Create a Google Slides presentation with a title slide and content slides.

Usage:
  python3 skills/slides_create.py --title "TITLE" --slide "Slide 1 Title|bullet1|bullet2" --slide "Slide 2 Title|bullet1"
  python3 skills/slides_create.py --title "TITLE" --slide "Title|point1|point2" [--slide ...] [--gws /full/path/to/gws]

Each --slide value is pipe-separated: first token is the slide title, remaining tokens are bullet points.
Prints the presentation URL on success.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import uuid

GWS_DEFAULT = "gws"
# EMU units: 1 inch = 914400
W, H = 9144000, 5143500  # 10 x 5.625 inches (16:9)
TITLE_X, TITLE_Y = 457200, 274320
TITLE_W, TITLE_H = 8229600, 1143000
BODY_X, BODY_Y = 457200, 1600200
BODY_W, BODY_H = 8229600, 3200400
TITLE_PT, BODY_PT = 40, 24
PT_TO_EMU = 12700


def emu(pt: int) -> dict:
    return {"magnitude": pt * PT_TO_EMU, "unit": "EMU"}


def run(cmd: list[str]) -> dict:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Error: {result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)
    return json.loads(result.stdout)


def shape_request(obj_id: str, slide_id: str, x: int, y: int, w: int, h: int) -> dict:
    return {"createShape": {
        "objectId": obj_id,
        "shapeType": "TEXT_BOX",
        "elementProperties": {
            "pageObjectId": slide_id,
            "size": {"width": {"magnitude": w, "unit": "EMU"}, "height": {"magnitude": h, "unit": "EMU"}},
            "transform": {"scaleX": 1, "scaleY": 1, "translateX": x, "translateY": y, "unit": "EMU"},
        },
    }}


def text_request(obj_id: str, text: str) -> dict:
    return {"insertText": {"objectId": obj_id, "text": text}}


def style_request(obj_id: str, pt: int, bold: bool = False) -> dict:
    return {"updateTextStyle": {
        "objectId": obj_id,
        "style": {"fontSize": emu(pt), "bold": bold},
        "fields": "fontSize,bold",
    }}


def para_style_request(obj_id: str) -> dict:
    return {"updateParagraphStyle": {
        "objectId": obj_id,
        "style": {"spaceAbove": emu(4), "spaceBelow": emu(4)},
        "fields": "spaceAbove,spaceBelow",
    }}


def build_slide_requests(slide_id: str, title: str, bullets: list[str]) -> list[dict]:
    tid, bid = str(uuid.uuid4()).replace("-", "")[:16], str(uuid.uuid4()).replace("-", "")[:16]
    reqs: list[dict] = [
        shape_request(tid, slide_id, TITLE_X, TITLE_Y, TITLE_W, TITLE_H),
        text_request(tid, title),
        style_request(tid, TITLE_PT, bold=True),
    ]
    if bullets:
        body_text = "\n".join(f"• {b}" for b in bullets)
        reqs += [
            shape_request(bid, slide_id, BODY_X, BODY_Y, BODY_W, BODY_H),
            text_request(bid, body_text),
            style_request(bid, BODY_PT),
            para_style_request(bid),
        ]
    return reqs


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--title", required=True)
    parser.add_argument("--slide", action="append", default=[], dest="slides")
    parser.add_argument("--gws", default=GWS_DEFAULT)
    args = parser.parse_args()

    # 1. Create presentation
    pres = run([args.gws, "slides", "presentations", "create",
                "--json", json.dumps({"title": args.title})])
    pres_id = pres["presentationId"]

    # 2. Get the auto-created blank slide ID
    pres_full = run([args.gws, "slides", "presentations", "get",
                     "--params", json.dumps({"presentationId": pres_id})])
    slide_ids = [s["objectId"] for s in pres_full.get("slides", [])]

    # 3. Build all batchUpdate requests
    requests: list[dict] = []

    # Parse slides: "Title|bullet1|bullet2"
    parsed = []
    for s in args.slides:
        parts = s.split("|")
        parsed.append((parts[0].strip(), [p.strip() for p in parts[1:] if p.strip()]))

    # Use the existing blank slide for the first content slide, add the rest
    for i, (title, bullets) in enumerate(parsed):
        if i < len(slide_ids):
            sid = slide_ids[i]
        else:
            sid = str(uuid.uuid4()).replace("-", "")[:16]
            requests.append({"addSlide": {"objectId": sid, "insertionIndex": i}})
        requests.extend(build_slide_requests(sid, title, bullets))

    if requests:
        run([args.gws, "slides", "presentations", "batchUpdate",
             "--params", json.dumps({"presentationId": pres_id}),
             "--json", json.dumps({"requests": requests})])

    print(f"https://docs.google.com/presentation/d/{pres_id}/edit")


if __name__ == "__main__":
    main()
