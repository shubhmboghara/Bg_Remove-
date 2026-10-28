# Clearcut

A working, single-workspace background-removal website with an original responsive interface and durable batch queue. Built in the `Bg_Remove-` repository using FastAPI, SQLite, Pillow and rembg's IS-Net model. Inference runs on this server; it does not call a paid image API.

## Run

Requires Python 3.12 and Linux. From this repository:

```sh
bash scripts/setup.sh
bash scripts/start.sh
```

The server listens on loopback port 8000. Open it through your development environment's supported access mechanism. No public hosting is configured. Run just one server process: an exclusive data-directory lock prevents competing workers.

The setup script installs the locked dependencies and downloads the model through rembg with its checksum verification enabled. Initial setup needs access to PyPI, GitHub release assets and their redirect hosts. Later processing uses the retained model and needs no image-processing service or API key. The model is approximately 179 MB.

## Use

1. Upload JPG, PNG or WebP files, or select a folder. Keep the page open during uploads.
2. Click **Remove backgrounds**. Three uploads can be in flight; inference processes one image at a time on CPU.
3. Follow progress, pause after the active image, or retry failed images. Closing the page does not stop the server queue. Restarting the server requeues interrupted processing.
4. Download individual transparent PNGs, or ZIP parts after the batch finishes. ZIP parts contain up to 250 images, preserve unique filenames and are assembled on disk rather than in memory.
5. Delete completed or paused batches to release their disk space. An active image must finish before deletion.

Limits: 10,000 images per batch, 25 MB and 25 megapixels per image. Input and output dimensions are retained, with EXIF orientation applied. Results are lossless PNGs; quality depends on the subject and model. Glass, fine hair and existing transparency need visual review. The landing-page product comparison is an illustration, not a claim about model output.

Uploaded images, results and the SQLite queue are retained under `.data/` until a batch is deleted. Model files remain cached. Keep `.data/` on persistent storage and back it up with the server stopped. If a browser upload is interrupted, already accepted images remain in a draft batch; unuploaded files must be selected again in a new batch. Archive uploads are not implemented; folder and multi-file uploads are supported.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `DATA_DIR` | `.data` beside `app.py` | Persistent queue and images |
| `U2NET_HOME` | `<DATA_DIR>/models` | Model cache |
| `REMBG_MODEL` | `isnet-general-use` | rembg model; alternatives require their own download and validation |
| `OMP_NUM_THREADS` | `2` | Bounds inference CPU parallelism |
| `PORT` | `8000` | Development server port |
| `APP_ACCESS_TOKEN` | unset | Optional workspace access key; enter it in the UI when configured |

Supply access keys through your host's secret configuration, never committed files. This is a **single-user development application**, not a multi-tenant public SaaS. Before internet exposure, add HTTPS, set an access key, enforce reverse-proxy upload and request-rate limits, and define storage quotas/retention. A shared workspace key does not isolate multiple users.

For sustained 1,000–10,000-image production batches, the next deployment stage is object storage, authenticated user isolation, a durable distributed queue and measured GPU worker capacity. This version's persistent CPU queue accepts 10,000 items, but no 10,000-image inference benchmark or production capacity claim has been made. Storage and runtime depend on actual image sizes; large PNGs can require hundreds of GB across a collection. No billing, accounts, GPU fleet or public deployment is included.

## Checks

```sh
.venv/bin/python -m pytest -q
node --check static/app.js
curl --fail http://127.0.0.1:8000/api/status
```

The eight API tests use an explicit deterministic inference substitute to exercise queue behavior: upload validation, duplicate output names, retries, model failure pausing, restart recovery, workspace authentication/origin checks, 10,000-row pagination/limits, and 250-image ZIP boundaries/deletion. They do not claim model-quality coverage.

Separately verified during development with a real IS-Net inference on scikit-image's bundled astronaut photo: browser upload → queue → transparent PNG → ZIP download → history after reload. Output was 512×512 RGBA with 119,885 near-transparent pixels and 138,339 near-opaque pixels. Desktop and 390px mobile browser checks reported no JavaScript errors or horizontal overflow. The check initially required too many pixels with alpha exactly 255; IS-Net produces near-opaque pixels, so verification checks foreground alpha ≥230 and background alpha <20 instead.

## REA and the reference site

REA 6.8.0 has now been run against two public JavaScript files downloaded from Photoroom over verified HTTPS. It produced a 39-node static graph and an evidence record. See [the analysis report](docs/PHOTOROOM-ANALYSIS.md) and [evidence summary](docs/rea-evidence-summary.json) for exact scope, hashes and limitations.

Photoroom already advertises bulk background removal, including examples of 2,000 images. Clearcut targets a dependable 10,000-image workflow; production throughput remains unbenchmarked. Live browser analysis remains blocked by Chromium's proxy certificate trust issue. Private backend code and model behavior were not reverse-engineered.

## Environment notes

Use this existing checkout; each cloud task is already isolated. Do not create a Git worktree unless explicitly requested. Dependencies/model files persist in the environment snapshot; the server process must be started again in a new task. Configuration draft saving and publication are separate from the successful local checks above.
