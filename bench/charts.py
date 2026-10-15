from __future__ import annotations

import csv
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "results"


def load_rows() -> list[dict[str, str]]:
    with (OUT / "results.csv").open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def value(rows: list[dict[str, str]], scenario: str, chunker: str, column: str) -> float:
    return next(
        float(row[column])
        for row in rows
        if row["scenario"] == scenario and row["chunker"] == chunker
    )


def main() -> None:
    rows = load_rows()
    cdc_names = [row["scenario"] for row in rows if row["chunker"] == "cdc"]

    fig, ax = plt.subplots(figsize=(8, 4))
    ax.bar(cdc_names, [value(rows, name, "cdc", "dedup_ratio") for name in cdc_names], color="#0f5f66")
    ax.set_ylabel("dedup ratio (logical / physical)")
    ax.set_title("Figure 3. Deduplication ratio by dataset (content-defined chunking)")
    ax.tick_params(axis="x", labelrotation=20)
    for label in ax.get_xticklabels():
        label.set_horizontalalignment("right")
    fig.tight_layout()
    fig.savefig(OUT / "figure3_dedup_ratio.png", dpi=150)
    plt.close(fig)

    edited = ["D3_edited_text", "D4_edited_binary"]
    positions = list(range(len(edited)))
    width = 0.35
    fig, ax = plt.subplots(figsize=(7, 4))
    ax.bar(
        [position - width / 2 for position in positions],
        [value(rows, name, "cdc", "storage_saved_pct") for name in edited],
        width,
        label="content-defined",
        color="#0f5f66",
    )
    ax.bar(
        [position + width / 2 for position in positions],
        [value(rows, name, "fixed", "storage_saved_pct") for name in edited],
        width,
        label="fixed-size",
        color="#99aabb",
    )
    ax.set_xticks(positions, edited)
    ax.set_ylabel("storage saved (%)")
    ax.set_title("Figure 4. Content-defined vs fixed-size chunking on edited data")
    ax.set_ylim(0, 65)
    ax.legend(loc="upper center", ncol=2, frameon=False)
    fig.tight_layout()
    fig.savefig(OUT / "figure4_cdc_vs_fixed.png", dpi=150)
    plt.close(fig)
    print("wrote figure3_dedup_ratio.png and figure4_cdc_vs_fixed.png")


if __name__ == "__main__":
    main()
