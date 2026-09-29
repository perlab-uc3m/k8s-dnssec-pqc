"""Export the compact manuscript table and figure from complete raw runs."""
from __future__ import annotations

import csv
import shutil
from pathlib import Path

from revision.report import report

CELLS = [
    ('unsigned-reference', 'Unsigned'),
    ('ed25519-reference', 'Ed25519'),
    ('falcon512-reference', 'Falcon-512'),
    ('mldsa44-reference', 'ML-DSA-44'),
    ('mldsa44-no-signature-cache', 'ML-DSA-44, no sig. cache'),
    ('mldsa44-fast-updates', 'ML-DSA-44, 10 updates/s'),
]


def _fmt(value: str, digits: int) -> str:
    return f'{float(value):.{digits}f}' if value not in ('', 'None') else 'n/a'


def export_paper(campaign: Path, output: Path) -> None:
    report(campaign)
    rows = {r['cell_id']: r for r in csv.DictReader(
        (campaign / 'tables/cell_summaries.csv').open())}
    if any(key not in rows or int(rows[key]['repetitions_complete']) != 3 for key, _ in CELLS):
        raise ValueError('Paper export requires three complete runs per reference cell')
    output.mkdir(parents=True, exist_ok=True)
    lines = [
        r'\begin{table*}[t]',
        r'\caption{\textcolor{blue}{Follow-up results at 100 offered queries/s and 32 headless names. Values are medians of three 30 s runs; brackets give the range of run P99 values. The unsigned row has no signature verification. Process CPU is summed over DNS pods.}}',
        r'\label{tab:revision_results}',
        r'\centering',
        r'\begin{tabular}{lrrrr}',
        r'\hline',
        r'\textcolor{blue}{Cell} & \textcolor{blue}{P99 [range] (ms)} & \textcolor{blue}{CPU (cores)} & \textcolor{blue}{Signs/s} & \textcolor{blue}{UDP to TCP (\%)} \\',
        r'\hline',
    ]
    for key, label in CELLS:
        r = rows[key]
        p99 = f"{_fmt(r['p99_ms_median'], 1)} [{_fmt(r['p99_ms_min'], 1)}, {_fmt(r['p99_ms_max'], 1)}]"
        cpu = _fmt(r['cpu_cores_median'], 3)
        signs = _fmt(r['signs_per_s_median'], 1)
        fallback = _fmt(str(float(r['fallback_fraction_median']) * 100), 0)
        lines.append(rf'\textcolor{{blue}}{{{label}}} & \textcolor{{blue}}{{{p99}}} & \textcolor{{blue}}{{{cpu}}} & \textcolor{{blue}}{{{signs}}} & \textcolor{{blue}}{{{fallback}}} \\')
    lines.extend([r'\hline', r'\end{tabular}', r'\end{table*}'])
    (output / 'revision_results_table.tex').write_text('\n'.join(lines) + '\n')
    shutil.copyfile(campaign / 'figures/revision_contrasts.pdf', output / 'revision_contrasts.pdf')


def export_controls(ttl_campaign: Path, stress_campaign: Path, output: Path) -> None:
    report(ttl_campaign)
    report(stress_campaign)
    def grouped(campaign: Path) -> dict[str, list[dict]]:
        rows = list(csv.DictReader((campaign / 'tables/run_summaries.csv').open()))
        result: dict[str, list[dict]] = {}
        for row in rows:
            result.setdefault(row['cell_id'], []).append(row)
        return result
    ttl = grouped(ttl_campaign)
    stress = grouped(stress_campaign)
    ttl_keys = [
        ('mldsa44-response-off', 'No response\ncache'),
        ('mldsa44-response-ttl1', 'Response\n1 s'),
        ('mldsa44-response-ttl5', 'Response\n5 s'),
        ('mldsa44-no-sig-cache-response-ttl1', 'Response 1 s\nno sig. cache'),
    ]
    stress_keys = [
        ('mldsa44-300-one-core', 'One pod\n1 core'),
        ('mldsa44-300-two-half-cores', 'Two pods\n0.5 core each'),
        ('mldsa44-300-two-cores', 'Two pods\n1 core each'),
    ]
    for groups, keys in ((ttl, ttl_keys), (stress, stress_keys)):
        if any(len(groups.get(key, [])) != 3 for key, _ in keys):
            raise ValueError('Control export requires three complete runs per cell')
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import statistics
    fig, axes = plt.subplots(1, 3, figsize=(10.5, 3.4))
    def draw(ax, groups, keys, field, scale, title, ylabel):
        for index, (key, label) in enumerate(keys):
            values = [float(row[field]) * scale for row in groups[key]]
            for offset, value in enumerate(values):
                ax.scatter(index + (offset - 1) * 0.07, value, s=23,
                           color='#174a70', alpha=0.8)
            ax.plot(index, statistics.median(values), marker='_', markersize=17,
                    markeredgewidth=2, color='#b33d32')
        ax.set_xticks(range(len(keys)), [label for _, label in keys], fontsize=8)
        ax.set_title(title, fontsize=10)
        ax.set_ylabel(ylabel, fontsize=9)
        ax.grid(axis='y', alpha=0.2)
    draw(axes[0], ttl, ttl_keys, 'stale_post_ack_fraction', 100,
         '(a) Response cache', 'Signed stale answers (%)')
    draw(axes[1], stress, stress_keys, 'p99_ms', 1,
         '(b) 300 queries/s', 'P99 (ms)')
    draw(axes[2], stress, stress_keys, 'throttled_period_fraction', 100,
         '(c) 300 queries/s', 'CPU periods throttled (%)')
    fig.tight_layout()
    fig.savefig(output / 'revision_controls.pdf')
    fig.savefig(output / 'revision_controls.png', dpi=180)
    plt.close(fig)
