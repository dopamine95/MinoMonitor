# Development notes

Mino Monitor was built in a back-and-forth between two AI assistants
(Anthropic's Claude and OpenAI's Codex) plus an architect agent acting as
an independent reviewer. These two design documents capture the debate
and where it landed:

- [`DESIGN_v1.md`](./DESIGN_v1.md) — first proposal. Wrong about the most
  important thing: claimed pausing apps "frees RAM," which is false on
  Apple Silicon.
- [`DESIGN_v2.md`](./DESIGN_v2.md) — synthesized after critique. Ditches
  that framing, adds the three-rung action ladder
  (`taskpolicy → SIGSTOP → osascript quit`), drops `purge` (a placebo on
  modern macOS), and locks the safety model.

Useful as a worked example of "design first, then build" for a small TUI
app — and as honest record of what the first take got wrong.
