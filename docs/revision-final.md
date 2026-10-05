# Final revision campaign

The campaign uses one build for 56 configurations and five randomized repetitions (280 measured runs). It re-measures the twelve reference configurations from 30 September. The new results replace, rather than pool with, that earlier campaign.

## Remote commands

Run on the Linux x86-64 measurement host, with working Docker access and the prerequisites in README.md. Keep the host otherwise idle, connected to power, and use the same CPU governor throughout. Allow 6–8 GiB of available RAM and enough disk for images, builds and raw data.

```bash
cd ~/k8s-dnssec-pqc                 # adjust to your checkout
git pull --ff-only
sudo modprobe sch_netem
./bench doctor
BUILD_JOBS=4 ./bench build
./bench final-revision
```

Use `BUILD_JOBS=2` on a memory-constrained machine. A rough planning allowance is 30–60 minutes for a clean build and 4–6 hours for smoke, measurement and exports; network downloads, cryptographic speed and deployment times can make it longer. Run in an existing tmux/screen session so a disconnected SSH session does not interrupt the campaign.

Optional Intel package-0 energy, before starting:

```bash
if [ -f /sys/class/powercap/intel-rapl:0/energy_uj ]; then
    sudo chmod a+r /sys/class/powercap/intel-rapl:0/energy_uj
fi
```

If `sch_netem` is missing on Ubuntu, install the kernel-matching `linux-modules-extra-$(uname -r)` package. A netem preflight inside the Kind node must pass. The runner restores no old experiment configuration and never silently drops a failed algorithm.

The build first checks all twelve signers through the DNS fork, including tampered-record rejection and reading keys produced by the classical key generator. The runner checks the build and image manifest, creates/loads the named Kind cluster, runs a 25-configuration smoke campaign, checks it, executes the main campaign, exports tables and figures, and verifies the raw archive. A repeated `./bench final-revision` validates the frozen source/configuration and skips completed runs. It does not trust an old smoke marker alone. Do not pull changes or rebuild partway through a campaign: changed acquisition files or builds are rejected. If intentionally starting over with different code, preserve both existing final and smoke directories under distinct archive names first.

## Experimental blocks

| Block | Conditions | Question and interpretation |
| --- | --- | --- |
| R | Twelve original reference configurations | Repeat the prior evidence on the new build, including bursts, Zipf access, cache disabled, two replicas and persistent TCP |
| S | Twelve signers in total, plus unsigned control | Measure size, signing duration, CPU and transport for all submitted signers; sizes and transport are measured, not assumed |
| U | ML-DSA-44 at 50 endpoint updates/s | Test frequent content changes; report achieved updates and missed deadlines |
| Q | Falcon-512 and two ML-DSA-44 connection policies at 400 queries/s | Compare steady load with 100 queries/s; distinguish server CPU from generator delay |
| A | SPHINCS+ at 1, 4, 8 and 12 updates/s | Seek real cryptographic CPU pressure; saturation is an outcome, not guaranteed on every host |
| B | Response-cache TTL 0, 1 and 5 s for ML-DSA-44 and SPHINCS+ | Measure verified freshness, deadline success, signing and CPU together |
| C | One pod × one core, two pods × half a core, one pod × two cores and two pods × one core at 4 updates/s; one pod × two cores at 8 and 12 updates/s | Compare replicas under matched total quotas and compare one versus two cores within one pod at matched workloads |
| K | One static hot name, no signature cache, SPHINCS+ at 2 and 10 queries/s and ML-DSA-44 at 10 queries/s | Use fresh direct TCP to obtain one DNS request per scheduled arrival, avoiding dependent UDP-to-TCP retries when testing the coalescence baseline |
| L | Eight distinct names updated back to back every eight seconds, with a seeded phase | Compare correlated updates with Poisson changes; measure achieved update timing, not just planned batch times |
| D | Added pod-egress delay of 1 and 10 ms with UDP, fallback and persistent TCP | Measure the incremental cost of the configured path; this is one-direction emulation, not a WAN |
| P | 1% random pod-egress packet loss; UDP retry after 1 s | Measure failure and latency tails under loss, including connection-policy effects |
| E | 250 names, 50 updates/s, 60 s, 1,024 versus 9,984 signature slots; Zipf with the large cache | Exercise cache capacity with enough changing versions. The implementation clamps smaller requested caches to 1,024 slots; evictions and achieved updates determine whether pressure occurred |

Every run records DNS-server and client CPU, query outcomes, transmitted attempts, received DNS messages, endpoint changes, signature verification and freshness. Process CPU is measured directly; the plugin's signing-duration metric measures wall time. A signing-rate × wall-time calculation is only a demand proxy. It cannot establish CPU utilization or provide a queueing bound. The signing-inflight gauge counts active leaders, not all queued or waiting queries.

Package-0 energy is optional. It includes other activity on that CPU package and is not per-process energy or whole-machine energy. Counter differences are accumulated between samples to handle wraps. Any regression against signing rate is an exploratory association, not an isolated energy cost per signature.

## Predictions and decision rules

- Caching should reduce signing when queries revisit unchanged RRsets. Test predicted counts against all repetitions. Report residuals, especially with eviction, dependent retries, delayed updates and replica routing. Do not call a model validated merely because aggregate totals agree.
- TTL can reduce signing at the cost of stale answers. Report fresh answers within one second, stale answers, unknown freshness, timeouts and rejections. Do not choose a TTL after seeing results and then describe it as a prespecified optimum.
- If the slow signer does not saturate at 12 updates/s, report the measured range as unsaturated. Do not claim that a capacity boundary was found or silently add cells to the completed campaign.
- Direct-TCP singleflight controls use the per-key Poisson relation as a baseline. Check actual dispatch delay and arrival spacing. It does not predict P99, waiting-time distributions or CPU contention.
- Delay slopes near 1, 3 and 1 are hypotheses for UDP, fresh fallback and persistent TCP. Netem affects all pod-egress packets, including SYN-ACKs, ACKs and Kubernetes API traffic. Loss is packet loss, not a fixed fraction of DNS replies or queries. Retransmission behavior and segmentation can change the result.
- Runs with poor service outcomes stay in the results. “Collapsed” is the prespecified descriptive threshold of fewer than half of offered queries receiving a verified answer within one second; it is not a substitute for CPU saturation evidence.
- Missing CPU data are shown as unavailable, not zero. Invalid cryptographic answers must be investigated. Report all offered queries and completed repetitions; technical failures and retries remain in the archive.
- If smoke fails, stop and diagnose. No algorithm is automatically dropped. A design change requires a new frozen campaign.

## Reviewer coverage and remaining limits

The campaign addresses workload sensitivity, higher updates and query load, correlated rollouts, popularity, namespace size, signature-cache capacity, response-cache TTL, resource use, actual slow signing, matched CPU quotas, TCP reuse, delay and loss. It cannot guarantee acceptance or establish universal PQC practicality. A measured combination of existing cache and transport mechanisms is not by itself a new algorithm.

Future work remains multi-host and multi-node behavior, production traces, multi-tenancy, eviction-policy comparisons, query-throughput saturation beyond the generator's capacity, encrypted DNS transports, independent public-resolver validation, and controlled path-MTU/fragmentation tests. Multiple virtual nodes are technically possible on one host but would still share the same physical CPU and network; adding that topology is outside this campaign's time budget.

A 1,232-byte advertised DNS UDP payload reduces fragmentation risk under appropriate path-MTU assumptions. It does not prove fragmentation impossible on every IPv4 path or encapsulated network. No packet-level fragmentation evidence is collected here. See RFC 9715: https://www.rfc-editor.org/rfc/rfc9715.html.

## Outputs to retain and copy back

- `results/revision/revision-final/`: raw runs, frozen acquisition/configuration records, `provenance/` with compiler/module build information and node versions, `report.md`, and per-run tables.
- `results/revision/revision-final/export/paper/`: the twelve reference configurations, using all five repetitions and no hard-coded historical results.
- `results/revision/revision-final/export/final/`: survey, model, capacity, coalescence, workload/network tables, figures, and `final_numbers.json`.
- `release/revision-final-data.tar` and `.tar.sha256.json`: verified archive containing both smoke and main campaigns, including failed attempts.

Copy the archive and checksum manifest back as well as `export/` and `report.md`. Summaries alone are insufficient for a final scientific audit. Keep the pushed source commit available. Generated manuscript figures belong under `paper/comnet/figures/review/` when integrating the new results. No new measurements or conclusions should be inserted into `main_review.tex` until this campaign completes and is checked.
