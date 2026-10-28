# Photoroom reference analysis

## Actual REA execution

REA's upstream repository is retained at `/workspace/rea`. The published CLI from that project, `rea-agents@6.8.0`, is installed in `/workspace/rea-tools`, with isolated Node 24.22.1. We followed its `reverse-engineer-anything` skill and CLI fallback guidance. No MCP configuration or native analysis engine was required.

The public homepage and batch-background-removal page were fetched successfully over verified HTTPS on October 27, 2026. Two directly referenced homepage scripts were downloaded, without executing them:

- `https://www.photoroom.com/_astro/Layout.astro_astro_type_script_index_0_lang.DK9R0YPW.js`
- `https://www.photoroom.com/_astro/index.astro_astro_type_script_index_0_lang.D7AI93dx.js`

Executed command:

```sh
PATH=/workspace/rea-tools/node_modules/.bin:$PATH rea analyze-javascript-application \
  /workspace/Bg_Remove-/.data/research/photoroom-scripts \
  --analysis-timeout-ms 30000 --max-heap-mb 512 --format json
```

REA completed successfully. Its full output is retained at `.data/research/rea-analysis.json`. The evidence ID, source hashes and limitations are recorded in `rea-evidence-summary.json`. This is static shipped-artifact analysis, not a live browser trace or private-backend reconstruction. REA marks coverage incomplete: imported dependencies outside the two-file capture remain unresolved.

## Observations and implications

The fetched HTML references Astro-generated script paths. The layout source contains analytics initialization, navigation-event handling, attribution-link rewriting and route categorization. The index source imports and invokes a rating-form helper. These are public marketing-site concerns; they do not expose background-removal inference code or establish the editor's technology stack.

Photoroom's public page at https://www.photoroom.com/batch/background-remover already advertises bulk background removal. Its stated workflow is upload a photo set, apply background removal across the batch, and download the cutouts. It specifically discusses “20 images or 2,000.” That is website copy, not an independently measured limit or throughput result. We did not upload images to Photoroom or use a paid account.

The same page links bulk tools for resizing, positioning, background colors, shadows, filters, watermarks and format conversion. These are adjacent features, not all requirements for the user's background-removal application.

## Scope for Clearcut

Prioritize 1–10,000 images per batch, persistent per-image job states, bounded upload/inference concurrency, pause/resume, retry, transparent full-resolution PNGs and split ZIP downloads. These are already implemented in the local single-workspace version. Its 10,000-item queue and pagination are tested, while real inference has only been exercised on a small image sample. Do not market it as production-proven at 10,000 images.

The next production stage requires measured GPU capacity, object storage, user-isolated jobs, quotas, and a distributed queue. Keep 10,000 as the current upper bound. A 20,000-image tier should follow measured storage, throughput and recovery tests rather than simply increasing a constant. Model edge quality must be evaluated on representative product photos separately from queue correctness.

## Browser limitation

Chromium navigation failed with `ERR_CERT_AUTHORITY_INVALID` through this environment's proxy. Importing the narrowly selected environment proxy CA did not resolve it. Automatic approval review rejected a broader persistent trust-store import, so that operation was not performed. TLS verification was never disabled. Verified HTTPS downloads plus REA's supported static analyzer provided a narrower route to public-source analysis. Browser interaction, authenticated editor behavior, real network requests during removal, and server-side inference remain unverified.

This analysis does not copy Photoroom branding/assets into Clearcut or use its private APIs. Clearcut's UI and processing implementation are independent.
