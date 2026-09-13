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

## Relationship to `docs/PRESENTATION.md`

`docs/PRESENTATION.md` is the same 7-slide narrative in Marp, and it reads
fine as a plain document without rendering. Keep it for anyone who wants the
argument as prose. This deck is what you present.
