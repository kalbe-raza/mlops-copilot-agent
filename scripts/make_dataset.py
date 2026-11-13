"""Materialise a version of the patient dataset into data/raw/dataset.csv.

The source is the Wisconsin Diagnostic Breast Cancer dataset bundled with scikit-learn
(no network required). Versions simulate how a real dataset evolves:

* v1     - initial extract: a stratified sample of 400 patients
* v2     - full refresh: all 569 patients (169 new records)
* corrupt- deliberately broken file used for failure injection (schema drift + nulls)

Usage:  python scripts/make_dataset.py --version v1 [--out data/raw/dataset.csv]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.datasets import load_breast_cancer
from sklearn.model_selection import train_test_split

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from training.schema import FEATURE_COLUMNS, ID_COLUMN, TARGET_COLUMN  # noqa: E402


def full_frame() -> pd.DataFrame:
    raw = load_breast_cancer(as_frame=True)
    df = raw.data.copy()
    df.columns = FEATURE_COLUMNS
    # sklearn encodes 0 = malignant, 1 = benign; we model malignancy as the positive class.
    df[TARGET_COLUMN] = (1 - raw.target).astype(int)
    df.insert(0, ID_COLUMN, [f"P{idx:04d}" for idx in range(len(df))])
    return df.round(6)


def build(version: str) -> pd.DataFrame:
    df = full_frame()
    if version == "v1":
        sample, _ = train_test_split(df, train_size=400, stratify=df[TARGET_COLUMN], random_state=7)
        return sample.sort_values(ID_COLUMN).reset_index(drop=True)
    if version == "v2":
        return df
    if version == "corrupt":
        broken = df.drop(columns=["worst_concave_points"]).copy()
        rng = np.random.default_rng(13)
        idx = rng.choice(len(broken), size=40, replace=False)
        broken.loc[idx, "mean_radius"] = np.nan
        return broken
    raise SystemExit(f"unknown version {version!r}; expected v1, v2 or corrupt")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", required=True, choices=["v1", "v2", "corrupt"])
    parser.add_argument("--out", default=str(ROOT / "data" / "raw" / "dataset.csv"))
    args = parser.parse_args()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    frame = build(args.version)
    frame.to_csv(out, index=False)
    print(f"wrote {len(frame)} rows x {frame.shape[1]} cols ({args.version}) -> {out}")


if __name__ == "__main__":
    main()
