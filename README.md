# PQC DNSSEC Kubernetes benchmark

The revision benchmark runs on one Linux host. It builds a small CoreDNS image with the pinned DNSSEC plugin, creates one named Kind cluster, sends bounded DNS load, captures the received packets, verifies signed positive answers, records per-pod CPU and memory, and generates tables and figures. The original `run.py` campaign remains in this repository for comparison, with its instructions in [docs/legacy-benchmark.md](docs/legacy-benchmark.md). Use `./bench` for the revised measurements.

## Run it

Prerequisites are Docker access, Go 1.26.0, CMake, Ninja, a C compiler, Python 3 with `venv`, `curl`, and enough free disk for the Kind node image and build cache. The tested setup is Linux x86-64. Kind is capped at 4 GiB; this is a limit, not proof that an 8 GiB host is sufficient. The measured host had 12 logical CPUs, an Intel Core i7-1365U processor, and 14.8 GiB RAM, with other applications running.

```bash
./bench doctor
./bench plan config/revision-smoke.yaml
./bench reproduce config/revision-smoke.yaml
```

`reproduce` bootstraps pinned Python, Kind, and kubectl tools, builds liboqs and CoreDNS with two build jobs, creates the named `pqc-dnssec-revision` Kind context, runs the cells, verifies packets, and plots results. It can take several minutes on the first build. It uses ports 30053/UDP, 30054/TCP, and 30153/TCP. It does not replace the cluster's normal DNS deployment. The smoke file has two ten-second cells. The reviewer study has twelve cells, three repetitions, 30 seconds of measurement and five seconds of warmup per run:

```bash
./bench plan config/revision-study.yaml
./bench reproduce config/revision-study.yaml
```

After the runs, `./bench cleanup` deletes only the named revision Kind cluster and frees its memory. A stopped campaign resumes by skipping runs with a `COMPLETE` marker. Failed and excluded attempts remain under their run directory; the next attempt gets a new number. Build jobs can be reduced with `BUILD_JOBS=1`. Build and cluster setup can be skipped after a successful first run with `--no-build --no-setup`. Regenerate tables and figures without Docker or Kubernetes using:

```bash
./bench report results/revision/revision-study
```

## What is measured

The client generates independent seeded Poisson arrivals or a two-second high phase in a 20-second burst cycle. Burst rates are normalized over the exact measurement window so the offered mean matches the control. Uniform and Zipf popularity are separate choices. It never creates unbounded in-flight tasks. Every offered query gets a record with its planned time, admission, first send, attempts, completion, status, received byte count, and DNS ID. A UDP truncation is retried on fresh TCP; separate cells use a bounded persistent TCP pool. One deadline covers all attempts. The original DNS response bytes are saved before parsing.

The workload uses fixed selectorless headless Services and managed EndpointSlices. It changes one ready address at a time and logs API acknowledgment and expected address. These synthetic endpoints exercise DNS discovery only. A normal ClusterIP record would remain stable during backend pod turnover.

Each run contains `config.json`, `host.json`, `queries.jsonl.gz`, `responses.bin.gz`, `updates.jsonl`, `metrics.jsonl`, `load.json`, `summary.json`, and, for signed cells, `verification.jsonl` and the public test-zone `trust_anchor.key`. Raw response frames use a 13-byte big-endian header: 8-byte query ID, 1-byte attempt index, and 4-byte length. The batch verifier checks the captured positive Answer RRsets against the pinned key after load stops. It also checks the response question, DNS ID, signature time, signer, and key. This is private-zone positive-answer verification, not a recursive public DNSSEC chain validator. Negative proofs are not validated and do not count as successful service resolutions.

Per-pod Prometheus samples are taken at roughly 1 Hz with before and after snapshots. A run is excluded if it lacks full-window CPU boundaries or enough periodic samples. Average CPU cores are the difference in CoreDNS process CPU seconds divided by elapsed time. This is whole-process CPU, not crypto-only CPU. Successful-response P99 is always shown next to the offered-query count and failures; a timed-out or invalid response never disappears from the denominator. Update visibility is an upper bound from API acknowledgment to the first observed new answer under ordinary query load. Events with no such query before the next update are censored.

The campaign directory contains `tables/run_summaries.csv`, `tables/outcomes.json`, `figures/latency_cpu.pdf`, `figures/latency_cpu.png`, and `report.md`. Figures are generated from complete runs only. The study does not infer server saturation from the product of query rate and signing wall time.

## Build and protocol scope

`scripts/build_revision.sh` checks exact dependency commits, applies the checked-in DNS verifier and plugin error-handling patches, builds only the signature algorithms needed for the study, and records the image ID and Go module build information. A signing error returns `SERVFAIL` instead of a successful unsigned answer. The old implementation and its broader algorithm list remain available through `run.py` and `scripts/build.sh`.

The fork uses algorithm number 17 for Falcon-512, while IANA assigns 17 to SM2SM3. These experiments use a private zone and an out-of-band trust anchor; the Falcon results do not establish public DNSSEC interoperability. ML-DSA-44 uses number 18 in the fork and current IANA registry, but this benchmark still checks its own wire encoding and key locally.

The measured cells are controlled single-host sensitivity tests. They do not represent production Kubernetes traffic, public DNS transport, energy use, or a multi-node CNI deployment.

## Reproduce the paper panels and preserve the raw data

The companion controls are `config/revision-cache-ttl.yaml` (response cache, freshness, and signature cache) and `config/revision-stress.yaml` (unsigned control, signing load, and matched CPU budgets across replicas). Run them one after another with `./bench reproduce CONFIG --no-build --no-setup` after the study. Campaigns keep failed attempts and report only complete repetitions.

The following command recreates the TeX table and PDF figure used by the revised manuscript from all complete raw runs. It requires three complete repetitions of each of the six displayed study cells.

```bash
./bench export-paper results/revision/revision-study --ttl results/revision/revision-cache-ttl --stress results/revision/revision-stress --output ../paper/comnet/figures
```

A portable archive can be built without copying the temporary build tree or Docker images. The adjacent JSON manifest holds a SHA256 for the tar file and for each contained file. Archive creation and verification do not need the cluster.

```bash
./bench package results/revision/revision-study results/revision/revision-cache-ttl results/revision/revision-stress --output release/revision-data.tar
./bench verify-package release/revision-data.tar release/revision-data.tar.sha256.json
```

Extract the archive in a separate directory and run `./bench report PATH/TO/revision-study` to rebuild the study tables and plots offline. The published archive location is intentionally left to the release process; the scripts do not download from an unverified or assumed URL. The result archive, its manifest, and the checked-in campaign YAMLs together identify the numerical evidence for the paper. The `release/`, `build/`, and `results/` directories are ignored by Git because raw campaign data and build objects are large.
