"""Exact weighted algebraic point reconstruction.

``solve`` is the shared numerical core.  ``reconstruct`` is the original
single-dataset report shape and is kept byte-for-byte compatible with the
baseline command; the calibration-aware store calls ``solve`` directly.
"""

from fractions import Fraction

from .algebra import least_squares
from .checks import number, vector


def solve(track, cameras):
    """Return the exact solution and per-observation diagnostics.

    All observations participate.  A rank-deficient normal matrix means the
    point is unobservable; no camera is discarded and no coordinate is fixed.
    """
    rows = []
    for observation in track["observations"]:
        projection = cameras[observation["camera_id"]]
        xy = vector(observation["xy"], 2, "observation xy")
        weight = number(observation["weight"], "observation weight")
        for index, value in enumerate(xy):
            coefficients = tuple(
                value * projection[2][i] - projection[index][i] for i in range(3)
            )
            rhs = projection[index][3] - value * projection[2][3]
            rows.append((coefficients, rhs, weight))

    xyz = least_squares(rows)
    if xyz is None:
        return {
            "status": "unobservable",
            "xyz": None,
            "observations": [],
            "algebraic_squared_error": None,
            "normal_equation_residual": None,
        }

    augmented = xyz + (Fraction(1),)
    diagnostics = []
    for observation in track["observations"]:
        projection = cameras[observation["camera_id"]]
        homogeneous = [
            sum(a * b for a, b in zip(row, augmented)) for row in projection
        ]
        depth = homogeneous[2]
        xy = vector(observation["xy"], 2, "observation xy")
        if depth:
            projected = [homogeneous[i] / depth for i in range(2)]
            residual = [projected[i] - xy[i] for i in range(2)]
            squared = residual[0] * residual[0] + residual[1] * residual[1]
        else:
            projected = None
            residual = None
            squared = None
        diagnostics.append(
            {
                "camera_id": observation["camera_id"],
                "depth": depth,
                "positive_depth": depth > 0,
                "projected_xy": projected,
                "residual_xy": residual,
                "squared_residual": squared,
            }
        )

    error = sum(
        (weight * (sum(a[i] * xyz[i] for i in range(3)) - b) ** 2 for a, b, weight in rows),
        Fraction(0),
    )
    normal_residual = [
        sum(
            (
                weight * a[i] * (sum(a[j] * xyz[j] for j in range(3)) - b)
                for a, b, weight in rows
            ),
            Fraction(0),
        )
        for i in range(3)
    ]
    if any(normal_residual):
        raise ValueError("internal least-squares residual failure")
    return {
        "status": "reconstructed",
        "xyz": xyz,
        "observations": diagnostics,
        "algebraic_squared_error": error,
        "normal_equation_residual": normal_residual,
    }


def reconstruct(track, cameras):
    """Original baseline report for one track (public output unchanged)."""
    solved = solve(track, cameras)
    if solved["status"] == "unobservable":
        return {
            "track_id": track["id"],
            "status": "unobservable",
            "xyz": None,
            "reprojections": [],
            "algebraic_squared_error": None,
        }
    reprojections = []
    for observation in solved["observations"]:
        projected = observation["projected_xy"]
        residual = observation["residual_xy"]
        reprojections.append(
            {
                "camera_id": observation["camera_id"],
                "depth": str(observation["depth"]),
                "positive_depth": observation["positive_depth"],
                "xy": None if projected is None else [str(v) for v in projected],
                "residual_xy": None if residual is None else [str(v) for v in residual],
            }
        )
    return {
        "track_id": track["id"],
        "status": "reconstructed",
        "xyz": [str(v) for v in solved["xyz"]],
        "reprojections": reprojections,
        "algebraic_squared_error": str(solved["algebraic_squared_error"]),
        "normal_equation_residual": [str(v) for v in solved["normal_equation_residual"]],
    }
