# PQC DNSSEC Kubernetes benchmark

The revision benchmark runs on one Linux host. It builds a small CoreDNS image with the pinned DNSSEC plugin, creates one named Kind cluster, sends bounded DNS load, saves the received DNS messages, verifies signed positive answers, records per-pod CPU and memory, and generates tables and figures. The original `run.py` campaign remains in this repository for comparison, with its instructions in [docs/legacy-benchmark.md](docs/legacy-benchmark.md). Use `./bench` for the smoke check and `./bench final` for the complete repeated suite. The freshness campaign measures algorithm choice, caching and endpoint changes together; see [the run guide](docs/freshness-study.md). The saved `revision-*` campaigns remain a separate historical dataset.

## Run it

Prerequisites are Docker access, Go 1.26.0, CMake, Ninja, a C compiler, Python 3.14 with `venv`, `curl`, and enough free disk for the Kind node image and build cache. The tested setup is Linux x86-64. Kind is capped at 4 GiB; this is a limit, not proof that an 8 GiB host is sufficient. The earlier campaigns used an Intel Core i7-1365U with 12 logical CPUs and 14.8 GiB RAM, with other applications running. The 36-run article campaign in `results/revision` used an Intel Core i5-12500T with 12 logical CPUs and 15.3 GiB RAM; its results are reported separately.

```bash
./bench doctor
./bench plan config/freshness-smoke.yaml
BUILD_JOBS=1 ./bench reproduce config/freshness-smoke.yaml
```

`reproduce` bootstraps pinned Python, Kind, and kubectl tools, builds liboqs and CoreDNS with two build jobs, creates the named `pqc-dnssec-revision` Kind context, runs the cells, verifies signed answers, and plots results. It can take several minutes on the first build. It uses ports 30053/UDP, 30054/TCP, and 30153/TCP. It does not replace the cluster's normal DNS deployment. The default smoke file has three ten-second cells and tests collection correctness. The complete campaign is launched with `./bench final` and documented in [the freshness run guide](docs/freshness-study.md).

The original study has twelve cells, three repetitions, 30 seconds of measurement and five seconds of warmup per run:

```bash
./bench plan config/revision-study.yaml
./bench reproduce config/revision-study.yaml
```

After the runs, `./bench cleanup` deletes only the named revision Kind cluster and frees its memory. A stopped campaign resumes by skipping runs with a `COMPLETE` marker. Failed and excluded attempts remain under their run directory; the next attempt gets a new number. Build jobs can be reduced with `BUILD_JOBS=1`. Build and cluster setup can be skipped after a successful first run with `--no-build --no-setup`. Regenerate tables and figures without Docker or Kubernetes using:

```bash
./bench report results/revision/revision-study
```

## Final revision campaign

`./bench build` followed by `./bench final-revision` produces every result of the revised article from one build: the twelve reference configurations, every algorithm of the submitted manuscript, SPHINCS+ signing capacity, the response-cache TTL trade-off, replicas and cores, concurrent signing, rollouts, added delay and loss, and a 250-name namespace. The second command gates the main runs on a smoke check, resumes after interruption, and ends with tables, figures and a checksummed archive. Design, predictions, duration and deliberate omissions are in [docs/revision-final.md](docs/revision-final.md).

## What is measured

The client generates independent seeded Poisson arrivals or a two-second high phase in a 20-second burst cycle. Burst rates are normalized over the exact measurement window so the offered mean matches the control. Uniform and Zipf popularity are separate choices. It never creates unbounded in-flight tasks. Every offered query gets a record with its planned time, admission, first send, attempts, completion, status, received byte count, and DNS ID. A UDP truncation is retried on fresh TCP; separate cells use a bounded persistent TCP pool. One deadline covers all attempts. The original DNS response bytes are saved before parsing.

The workload uses fixed selectorless headless Services and managed EndpointSlices. It changes one ready address at a time and logs API acknowledgment and expected address. These synthetic endpoints exercise DNS discovery only. A normal ClusterIP record would remain stable during backend pod turnover.

Each run contains `config.json`, `host.json`, `queries.jsonl.gz`, `responses.bin.gz`, `updates.jsonl`, `metrics.jsonl`, `load.json`, `summary.json`, and, for signed cells, `verification.jsonl` and the public test-zone `trust_anchor.key`. Raw response frames use a 13-byte big-endian header: 8-byte query ID, 1-byte attempt index, and 4-byte length. The batch verifier checks the captured positive Answer RRsets against the pinned key after load stops. It also checks the response question, DNS ID, signature time, signer, and key. This is private-zone positive-answer verification, not a recursive public DNSSEC chain validator. Negative proofs are not validated and do not count as successful service resolutions.

Per-pod Prometheus samples are normally taken at roughly 1 Hz with before and after snapshots. A completed query window with invalid or missing CPU samples retains its query outcomes and marks CPU unavailable. Average CPU cores are the difference in CoreDNS process CPU seconds divided by elapsed time. This is whole-process CPU, not crypto-only CPU. Successful-response P99 is always shown next to the offered-query count and failures; a timed-out or invalid response never disappears from the denominator. Update visibility is an upper bound from API acknowledgment to the first observed new answer under ordinary query load. Events with no such query before the next update are censored. The primary new outcome is fresh, verified answers delivered before a deadline, divided by all offered queries. It retains queries overlapping an update and bounds uncertain states. Returned TTLs, warmup history, signature-cache evictions, pending signing, client CPU/RSS and host memory pressure are saved as diagnostics.

The campaign directory contains `tables/run_summaries.csv`, `tables/outcomes.json`, `figures/latency_cpu.pdf`, `figures/latency_cpu.png`, and `report.md`. Figures are generated from complete runs only. The study does not infer server saturation from the product of query rate and signing wall time.

## Build and protocol scope

`scripts/build_revision.sh` checks exact dependency commits, applies the checked-in DNS verifier and plugin error-handling patches, builds only the signature algorithms needed for the study, and records the image ID and Go module build information. A signing error returns `SERVFAIL` instead of a successful unsigned answer. The old implementation and its broader algorithm list remain available through `run.py` and `scripts/build.sh`.

The fork uses algorithm number 17 for Falcon-512, while IANA assigns 17 to SM2SM3. These experiments use a private zone and an out-of-band trust anchor; the Falcon results do not establish public DNSSEC interoperability. ML-DSA-44 uses number 18 in the fork and current IANA registry, but this benchmark still checks its own wire encoding and key locally.

The measured cells are controlled single-host sensitivity tests. They do not represent production Kubernetes traffic, public DNS transport, energy use, or a multi-node CNI deployment.

## Reproduce the paper panels and preserve the raw data

The companion controls are `config/revision-cache-ttl.yaml` (response cache, freshness, and signature cache) and `config/revision-stress.yaml` (unsigned control, signing load, and matched CPU budgets across replicas). Run them one after another with `./bench reproduce CONFIG --no-build --no-setup` after the study. Campaigns keep failed attempts and report only complete repetitions.

The saved TTL campaign uses the earlier signature implementation; it is not a clean TTL comparison. Run `./bench reproduce config/revision-cache-ttl-fixed.yaml` to collect a separate corrected campaign after building the default ownership patch. The corrected campaign has not yet been measured.

The revised manuscript `paper/comnet/main_review.tex` uses the separate 36-run campaign in `results/revision`. Regenerate its three data tables, two measured figures and the JSON file of quoted numbers under `paper/comnet/figures/final` with:

```bash
./bench export-paper results/revision --output ../paper/comnet/figures/final
```

The export also applies the signing model of `src/revision/analysis.py` to the logged update times, fits CoreDNS and client CPU against offered load in the burst runs, and splits latency at the client's first write. The earlier response-cache and quota controls remain historical artifacts and are not mixed into these outputs.

A portable archive can be built without copying the temporary build tree or Docker images. The adjacent JSON manifest holds a SHA256 for the tar file and for each contained file. Archive creation and verification do not need the cluster.

```bash
./bench package results/revision/revision-study results/revision/revision-cache-ttl results/revision/revision-stress --output release/revision-data-audited.tar
./bench verify-package release/revision-data-audited.tar release/revision-data-audited.tar.sha256.json
```

Extract the archive in a separate directory and run `./bench report PATH/TO/revision-study` to rebuild the study tables and plots offline. The published archive location is intentionally left to the release process; the scripts do not download from an unverified or assumed URL. That archive identifies the historical campaigns. The current 36-run campaign requires its own data archive and checksum manifest before public release. The `release/`, `build/`, and `results/` directories are ignored by Git because raw campaign data and build objects are large.

## Source checks

The revision runner and offline analysis live in `src/revision/`. Plotting is isolated in `figures.py`; the CLI imports it only when producing a report, so benchmark load generation does not load Matplotlib. The archived legacy runner remains available under `run.py`. To check the revision source before changing or sharing it:

```bash
./bench doctor
build/venv/bin/python -m pip install -r config/revision-dev.requirements.txt
./scripts/check_revision.sh
```

The check runs Ruff lint and format verification, the Python regression tests, Go formatting verification, and shell syntax checks. Figure exports use the original blue and green bar palette and show all three runs as open circles. The figures are regenerated from complete runs without selecting a favorable repetition.

## Audit corrections and the archived build

The saved 66 study and control runs precede the signature ownership correction. Their response-cache experiment exposed shared RRSIG records: aging a response could reduce the TTL in the signature cache, confounding a one-versus-five-second cache comparison. The default build now applies `patches/plugin-signature-ownership.patch`. Its regression test demonstrates that a response cannot mutate another response or a stored signature. It also sets the RRSIG original TTL from the signed RRset. These fixes have not yet been measured in a new Kubernetes TTL campaign. To reproduce the measured baseline explicitly, use `BENCH_SIGNATURE_OWNERSHIP_FIX=0 ./bench reproduce CONFIG --output NEW_DIRECTORY`. Do not mix runs from the two builds in one comparison.

Builds fetch the declared commits directly and always invoke Go's incremental build. The runner checks `build/source-manifest.json` before measurement so a changed patch cannot silently be paired with an old binary. The manifest records whether the ownership fix is present. Each run also records hashes of the Python acquisition source. Campaign resume refuses a changed acquisition implementation or build, and the deployed image tag must match the build manifest. Use a new output directory for the corrected implementation; archived campaigns remain available for offline analysis.

For the archived schema, analysis computes CPU and signing rates between the first periodic load scrape and the end scrape. Those samples share a clock origin; the original pre-load snapshot did not. All repaired pod intervals span at least 29.97 seconds. New collection uses absolute monotonic timestamps. Missing verification in a signed run is an error, and every response frame must match its query ID, attempt index, and recorded length. The archived update-visibility metric uses the final DNS transaction and excludes responses overlapping the next update request. The new answer-completion freshness metric explicitly retains those overlaps and is reported separately. Both distinguish older versions from unexpected addresses.

The manuscript keeps three figures: the signing path, CPU against offered load, and exchange-time distributions with the burst phases. Received message sizes and signing times appear in its algorithm table. The historical cache/quota control is excluded because the TTL comparison is confounded and the host differs.

Offline commands (`plan`, `report`, `export-paper`, `package`, and `verify-package`) skip Kubernetes tool downloads once Python dependencies are installed. Starting the default four-GiB Kind node requires five GiB of available host memory; running with an existing node requires one GiB of available headroom. These are conservative guards, not a measured minimum host specification.

For the complete campaign, aim for 6 to 8 GiB available before starting Kind. Close unused browser and editor windows and check `vmstat 1 10` for active swapping. Use `BUILD_JOBS=1`; no Prometheus server or application pods are required. `./bench cleanup` releases the dedicated cluster. See [docs/freshness-study.md](docs/freshness-study.md) for the complete sequence, experimental assumptions and remaining limits.
