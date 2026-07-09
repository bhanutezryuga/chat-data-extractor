# Spike — proof-of-concept

**Throwaway** validation of the core idea before the real Cloudflare/TypeScript build:
`link → classify (rules engine) → fetch description → extract + make a task → dashboard`.

Pure Python stdlib (no pip, no Node). Reuses the **real** schema (`db/schema.sql`) and
rules (`db/seed_rules.sql`), so findings map onto the production design.

## Run
```bash
python spike/run.py demo            # ingest sample URLs -> build dashboard
python spike/run.py ingest <url>    # ingest one URL
python spike/run.py dashboard       # rebuild dashboard.html
python spike/run.py reset           # wipe spike/poc.db
```
Then open **`spike/dashboard.html`** in a browser.

### Real extraction (Gemini)
```bash
# PowerShell:  $env:GEMINI_API_KEY="..."; python spike/run.py reset; python spike/run.py demo
```
Without a key it runs a deterministic **offline stub** so the pipeline is still demonstrable.
Use `--stub` to force offline even with a key.

## What the spike proved
- ✅ Rules engine routes each URL to the right content type by priority.
- ✅ v1 **description-only** logic works: reels/shorts with thin descriptions correctly go to
  `NEEDS_REVIEW` (video analysis deferred to V2) instead of being downloaded.
- ✅ Real content is fetched (oEmbed for YouTube, og/meta + body for articles) and flows into
  an extraction + a task; everything persists to the real schema and renders on a dashboard.
- ✅ Network egress works here, so a real Gemini test is possible with a key.

## Findings (fed back into the design)
1. **Bug found + fixed:** arxiv-style PDF URLs (`/pdf/1706.03762`, no `.pdf` extension) were
   misclassified as `article`. Fixed the `rule_pdf` matcher to also catch `arxiv.org/(pdf|abs)/`.
   *Lesson:* the real processor should also confirm type via the response `Content-Type` header,
   not URL pattern alone.
2. **Readability is naive** in the spike (article summaries include nav-menu noise). Real Gemini
   tolerates this, but the production `readability` step should strip chrome before sending.
3. PDF text isn't parsed in the spike (stdlib limitation) — the real system hands PDF bytes to
   Gemini natively, which is the designed path.

This folder is **not** part of the production build (`/workers`, `/dashboard`). It exists only to
de-risk the concept. See `../docs/PLAN.md` for the real M1–M6 plan.
