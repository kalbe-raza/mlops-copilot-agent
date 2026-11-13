"""Dataset contract shared by the data generator, the training pipeline and the inference API."""

from __future__ import annotations

FEATURE_COLUMNS: list[str] = [
    "mean_radius",
    "mean_texture",
    "mean_perimeter",
    "mean_area",
    "mean_smoothness",
    "mean_compactness",
    "mean_concavity",
    "mean_concave_points",
    "mean_symmetry",
    "mean_fractal_dimension",
    "radius_error",
    "texture_error",
    "perimeter_error",
    "area_error",
    "smoothness_error",
    "compactness_error",
    "concavity_error",
    "concave_points_error",
    "symmetry_error",
    "fractal_dimension_error",
    "worst_radius",
    "worst_texture",
    "worst_perimeter",
    "worst_area",
    "worst_smoothness",
    "worst_compactness",
    "worst_concavity",
    "worst_concave_points",
    "worst_symmetry",
    "worst_fractal_dimension",
]

TARGET_COLUMN = "malignant"  # 1 = malignant (positive class), 0 = benign
ID_COLUMN = "patient_id"
ALL_COLUMNS: list[str] = [ID_COLUMN, *FEATURE_COLUMNS, TARGET_COLUMN]

# Validation thresholds
MIN_ROWS = 200
MAX_NULL_FRACTION = 0.0
MIN_POSITIVE_FRACTION = 0.2
MAX_POSITIVE_FRACTION = 0.8
