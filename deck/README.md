# Presentation deck (Slidev)

The demonstration deck for the CARDIO4Cities case study.

## Structure

The case study asks for a **5–8 slide** deck covering "the problem as you
understand it, your proposed AI solution and architecture, how trust and
evidence are handled, the user experience, and your trade-offs and
limitations". It also asks, separately, that you be able to *"explain your
architecture and workflow"* and *"discuss trade-offs and limitations"* during
the demo.

Those are two different artifacts, so this deck is two tracks in one file:

| Slides | Track | Use |
|---|---|---|
| 1–8 | **The deck** | Present in order, at the start of the demo |
| 9+ | **Appendix** | Jump to on demand while walking through the code |

Slides 1–8 map onto the brief's five required topics in order. The appendix has
one section per agent, per datastore and per supporting component — do **not**
present it linearly. Press `o` for overview mode and jump to the slide that
answers the question actually asked.

Every slide has speaker notes (press `n`, or use the presenter view).

## Running it

Requires Node 18+.

```bash
cd deck
npm install
npm run dev          # opens http://localhost:3030
```

| Command | Result |
|---|---|
| `npm run dev` | Live-reloading presentation at `localhost:3030` |
| `npm run build` | Static site in `deck/dist/` — deployable anywhere |
| `npm run export` | `cardio4cities-deck.pdf` |
| `npm run export-notes` | Speaker notes as a separate PDF |

`npm run export` needs Playwright's Chromium. Slidev prompts to install it on
first use; accept, or run `npx playwright install chromium` beforehand.

## Presenting

| Key | Action |
|---|---|
| `→` / `space` | Next slide or click-reveal |
| `o` | Overview — the fastest way into the appendix |
| `n` | Presenter view with notes and a timer |
| `d` | Toggle dark mode |
| `g` | Go to slide number |

## Editing

`slides.md` is one Markdown file; `---` separates slides. Per-slide options go
in a small YAML block immediately after the separator:

```markdown
---
layout: two-cols
---

# Left column

::right::

# Right column
```

Speaker notes are the **last** HTML comment in a slide. Diagrams are Mermaid
fenced blocks. Styling uses UnoCSS utility classes inline.

Full syntax reference: https://sli.dev/guide/syntax

## Checking that slides actually fit — run this after editing

Slidev renders onto a fixed 16:9 canvas and **does not shrink content to fit
it**. Anything past the bottom edge is simply not drawn, and nothing warns
you: the markdown looks complete while the presentation quietly loses the last
three bullets. This deck had 31 slides in that state.

```bash
python check_overflow.py          # report
python check_overflow.py --fix    # write zoom: into the slides that need it
```

It parses `slides.md` the way Slidev does, estimates each slide's rendered
height from the values in `style.css`, and compares that against the canvas.
`--fix` writes a per-slide `zoom:` — Slidev's supported mechanism for this —
which preserves every word and changes no slide numbering.

It refuses to go below `zoom: 0.65` and reports those slides for splitting
instead, because past that point the slide has more content than scaling can
rescue and the honest fix is to cut it in two.

**Run it before `npm run export`.** The `overflow-y: auto` fallback in
`style.css` lets an over-tall slide scroll in the browser, which hides the
problem interactively — but a PDF page cannot scroll, so anything relying on
that fallback is lost in the export.

Two caveats. The height is an *estimate*: it models type scale, margins,
wrapping, table rows and code chrome, not glyph metrics, so read it as "this
slide is 40% too tall" rather than a pixel count. And it is calibrated to
over-report, because a false positive costs a glance and a false negative
costs content in front of an audience. If you change `style.css`, update the
constants at the top of the script to match.

## Relationship to `docs/PRESENTATION.md`

`docs/PRESENTATION.md` is the same 7-slide narrative in Marp, and it reads
fine as a plain document without rendering. Keep it for anyone who wants the
argument as prose. This deck is what you present.
