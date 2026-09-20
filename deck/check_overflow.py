#!/usr/bin/env python
"""
Find slides whose content is taller than the canvas, and optionally fix them.

    python check_overflow.py          # report only
    python check_overflow.py --fix    # write `zoom:` into the slides that need it

WHY THIS EXISTS

Slidev renders onto a fixed 16:9 canvas and does NOT shrink content to fit it.
Anything past the bottom edge is simply not drawn, and — this is the part that
wastes an afternoon — nothing warns you. The deck looks fine while you are
editing the markdown and loses its last three bullets in presentation mode.

`style.css` recovers space globally and `class: dense` recovers more on
individual slides, but both are guesses applied by eye. This measures instead:
it parses slides.md the way Slidev does, estimates the rendered height of each
slide from the values actually in style.css, and compares it against the real
canvas box.

The estimate is an estimate. It models font sizes, line heights, margins,
table rows, code-block chrome and text wrapping, but not glyph-level metrics,
so treat it as "this slide is 40% too tall" rather than a pixel figure. It is
calibrated to err toward reporting overflow, because a slide reported wrongly
costs a glance and a slide missed costs three bullets in front of an audience.

THE FIX IT APPLIES

Per-slide `zoom:` in the slide's own frontmatter, which is Slidev's supported
mechanism for exactly this. Chosen over the alternatives because it preserves
every word and changes no slide numbering: re-cutting 31 slides by hand is how
a deck that was merely too dense becomes a deck that is also wrong.

Zoom below ~0.65 means the slide has too much content to scale out of, and
`--fix` refuses to go there — it reports those for splitting instead.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

DECK = Path(__file__).parent / "slides.md"

# 16:9 at the canvasWidth set in the headmatter.
CANVAS_W = 1100.0
CANVAS_H = CANVAS_W * 9 / 16
ROOT_PX = 16.0

# Padding and type scale mirror deck/style.css. Change them there and here.
PAD_Y = (2.2 + 1.6) * ROOT_PX
PAD_X = 2.4 * ROOT_PX
BUDGET = CANVAS_H - PAD_Y
CONTENT_W = CANVAS_W - PAD_X * 2

# Layouts whose content is a title card, not a body of reference material.
TITLE_LAYOUTS = {"center", "cover", "intro", "section", "end", "statement", "quote"}

# Leave headroom, since the height is estimated.
HEADROOM = 0.96
# Below this, scaling is the wrong tool.
MIN_ZOOM = 0.65


def split_blocks(text: str):
    """Line index ranges between `---` separators, ignoring fenced code.

    Tracking fences matters: several slides show YAML and shell snippets that
    contain `---`, and splitting on the string alone silently invents slides.
    """
    lines = text.split("\n")
    in_fence = False
    separators = []
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence and line.rstrip() == "---":
            separators.append(i)
    blocks, previous = [], 0
    for index in separators:
        blocks.append((previous, index))
        previous = index + 1
    blocks.append((previous, len(lines)))
    return lines, blocks


def looks_like_frontmatter(block: list[str]) -> bool:
    meaningful = [line for line in block if line.strip()]
    if not meaningful:
        return False
    for line in meaningful:
        if line.startswith((" ", "\t", "-", "#")):
            continue
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*\s*:", line):
            return False
    return True


def parse(text: str):
    """Slides, each with its frontmatter line range and its body."""
    lines, blocks = split_blocks(text)
    slides, i = [], 0
    # A Slidev file opens with `---`, so block 0 is empty and block 1 is the
    # headmatter. Anything else and we are not looking at a Slidev deck.
    if blocks and not "".join(lines[blocks[0][0]:blocks[0][1]]).strip():
        i = 2
    while i < len(blocks):
        start, end = blocks[i]
        block = lines[start:end]
        frontmatter_range = None
        if looks_like_frontmatter(block) and i + 1 < len(blocks):
            frontmatter_range = (start, end)
            i += 1
            body_start, body_end = blocks[i]
        else:
            body_start, body_end = start, end
        slides.append(
            {
                "number": len(slides) + 1,
                "frontmatter_range": frontmatter_range,
                "frontmatter": "\n".join(lines[start:end]) if frontmatter_range else "",
                "separator_line": start - 1,
                "body": "\n".join(lines[body_start:body_end]),
            }
        )
        i += 1
    return lines, slides


def scale(dense: bool):
    base = (0.82 if dense else 0.92) * ROOT_PX
    code = (0.63 if dense else 0.68) * ROOT_PX
    return {
        "line": base * (1.36 if dense else 1.45),
        "chars_per_line": CONTENT_W / (base * 0.5),
        "h1": (1.5 if dense else 1.7) * ROOT_PX * 1.2
        + (0.5 if dense else 0.7) * ROOT_PX,
        "h2": 1.28 * ROOT_PX * 1.25 + 1.0 * ROOT_PX,
        "h3": (0.92 if dense else 1.0) * ROOT_PX * 1.3 + 0.55 * ROOT_PX,
        "code_line": code * 1.4,
        "code_chrome": (0.45 if dense else 0.6) * ROOT_PX * 2 + 0.8 * ROOT_PX,
        "table_row": 0.8 * ROOT_PX * 1.35 + 0.36 * ROOT_PX,
        "rule": ROOT_PX,
        "paragraph_gap": 0.7 * ROOT_PX,
        "list_gap": 0.24 * ROOT_PX,
    }


def estimate_height(body: str, dense: bool, two_columns: bool) -> float:
    m = scale(dense)
    height = 0.0
    in_fence = False
    fence_lines = 0
    table_rows = 0

    def wrapped(text: str, indent: int = 0) -> int:
        usable = max(20, int(m["chars_per_line"] - indent * 3))
        return max(1, -(-len(text) // usable))

    def flush_table():
        nonlocal table_rows, height
        if table_rows:
            height += table_rows * m["table_row"] + m["paragraph_gap"]
            table_rows = 0

    for raw in body.split("\n"):
        line = raw.strip()

        if line.startswith("```"):
            if in_fence:
                height += fence_lines * m["code_line"] + m["code_chrome"]
                fence_lines, in_fence = 0, False
            else:
                flush_table()
                in_fence = True
            continue
        if in_fence:
            fence_lines += 1
            continue

        if line.startswith("|") and line.endswith("|"):
            table_rows += 1
            continue
        flush_table()

        if not line:
            continue
        # Presenter notes are an HTML comment at the end of the slide and are
        # never rendered onto it.
        if line.startswith("<!--"):
            break
        if line in ("::right::", "::left::"):
            continue
        if re.match(r"^</?(style|div|v-clicks|Transform|span|img|br)", line):
            continue

        if line.startswith("# "):
            height += m["h1"]
        elif line.startswith("## "):
            height += m["h2"]
        elif line.startswith("### "):
            height += m["h3"]
        elif line in ("---", "***", "___"):
            height += m["rule"]
        elif re.match(r"^([-*+] |\d+\. )", line):
            indent = (len(raw) - len(raw.lstrip())) // 2
            height += wrapped(line, indent) * m["line"] + m["list_gap"]
        else:
            height += wrapped(line) * m["line"] + m["paragraph_gap"]

    if in_fence:
        height += fence_lines * m["code_line"] + m["code_chrome"]
    flush_table()

    if two_columns:
        # Content spreads across two columns, so the stack is roughly halved.
        # Not exactly: the taller column decides, and a header row sits above.
        height = height * 0.55 + 40
    return height


def title_of(body: str) -> str:
    for line in body.split("\n"):
        if line.startswith("# "):
            return re.sub(r"[`*]|<[^>]+>", "", line[2:]).strip()
    return "(untitled)"


def analyse(text: str):
    lines, slides = parse(text)
    findings = []
    for slide in slides:
        frontmatter = slide["frontmatter"]
        layout_match = re.search(r"layout:\s*(\S+)", frontmatter)
        layout = layout_match.group(1) if layout_match else "default"
        if layout in TITLE_LAYOUTS:
            continue

        dense = "dense" in frontmatter
        existing = re.search(r"zoom:\s*([\d.]+)", frontmatter)
        natural = estimate_height(slide["body"], dense, "two-cols" in layout)
        rendered = natural * (float(existing.group(1)) if existing else 1.0)

        findings.append(
            {
                **slide,
                "layout": layout,
                "dense": dense,
                "existing_zoom": float(existing.group(1)) if existing else None,
                "natural_height": natural,
                "rendered_height": rendered,
                "overflows": rendered > BUDGET,
                "needed_zoom": round((BUDGET * HEADROOM) / natural, 2),
                "title": title_of(slide["body"]),
            }
        )
    return lines, findings


def report(findings) -> int:
    over = [f for f in findings if f["overflows"]]
    print(f"canvas {CANVAS_W:.0f}x{CANVAS_H:.0f}, usable height {BUDGET:.0f}px")
    print(f"{len(findings)} content slides, {len(over)} overflowing\n")
    if not over:
        print("Every slide fits.")
        return 0

    print(f"{'#':>3} {'est px':>7} {'over':>6} {'zoom':>6}  title")
    print("-" * 94)
    for f in over:
        flags = "".join(["d" if f["dense"] else " "])
        print(
            f"{f['number']:>3} {f['rendered_height']:7.0f} "
            f"{(f['rendered_height'] / BUDGET - 1) * 100:5.0f}% "
            f"{max(f['needed_zoom'], MIN_ZOOM):6.2f} {flags} {f['title'][:60]}"
        )

    too_dense = [f for f in over if f["needed_zoom"] < MIN_ZOOM]
    if too_dense:
        print(
            f"\n{len(too_dense)} slide(s) cannot be fixed by scaling "
            f"(would need zoom < {MIN_ZOOM}). Split these:"
        )
        for f in too_dense:
            print(f"   slide {f['number']}: {f['title'][:64]}")
    return 1


def apply_fix(lines, findings) -> int:
    """Write `zoom:` into the frontmatter of every overflowing slide.

    Applied back-to-front so that inserting lines cannot invalidate the line
    numbers of slides not yet processed.
    """
    targets = [
        f for f in findings if f["overflows"] and f["needed_zoom"] >= MIN_ZOOM
    ]
    for f in sorted(targets, key=lambda f: f["number"], reverse=True):
        zoom = max(f["needed_zoom"], MIN_ZOOM)
        if f["frontmatter_range"]:
            start, end = f["frontmatter_range"]
            existing = [
                i for i in range(start, end) if lines[i].strip().startswith("zoom:")
            ]
            if existing:
                lines[existing[0]] = f"zoom: {zoom}"
            else:
                lines.insert(end, f"zoom: {zoom}")
        else:
            # No frontmatter block yet: turn the preceding separator into one.
            lines.insert(f["separator_line"] + 1, f"zoom: {zoom}")
            lines.insert(f["separator_line"] + 2, "---")
    return len(targets)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fix", action="store_true", help="write zoom: into overflowing slides"
    )
    args = parser.parse_args()

    text = DECK.read_text(encoding="utf-8")
    lines, findings = analyse(text)
    status = report(findings)

    if args.fix:
        changed = apply_fix(lines, findings)
        DECK.write_text("\n".join(lines), encoding="utf-8", newline="")
        print(f"\nWrote zoom: into {changed} slide(s). Re-run without --fix to verify.")
        return 0
    return status


if __name__ == "__main__":
    sys.exit(main())
