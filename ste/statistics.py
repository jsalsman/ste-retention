"""Small dependency-free statistical helpers for the STE experiment."""

import math
import statistics


def _betacf(a: float, b: float, x: float, iterations: int = 200) -> float:
    """Evaluate the continued fraction used by the incomplete beta function."""
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    # Keep the fraction away from a zero denominator.
    d = 1.0 / max(abs(d), 1e-30) * (1 if d >= 0 else -1)
    result = d
    for index in range(1, iterations + 1):
        twice = 2 * index
        # Apply the positive and negative terms as one iteration.
        for coefficient in (
            index * (b - index) * x / ((qam + twice) * (a + twice)),
            -(a + index) * (qab + index) * x / ((a + twice) * (qap + twice)),
        ):
            d = 1.0 + coefficient * d
            d = d if abs(d) >= 1e-30 else 1e-30
            c = 1.0 + coefficient / c
            c = c if abs(c) >= 1e-30 else 1e-30
            d = 1.0 / d
            delta = d * c
            result *= delta
        # The fraction has converged sufficiently for this experiment.
        if abs(delta - 1.0) < 3e-7:
            break
    return result


def _betai(a: float, b: float, x: float) -> float:
    """Return the regularized incomplete beta function for a bounded input."""
    if x <= 0.0 or x >= 1.0:
        # Boundary values avoid logarithms of zero.
        return float(x >= 1.0)
    log_beta = (
        math.lgamma(a + b)
        - math.lgamma(a)
        - math.lgamma(b)
        + a * math.log(x)
        + b * math.log(1.0 - x)
    )
    # Use the numerically stable side of the symmetry relation.
    if x < (a + 1.0) / (a + b + 2.0):
        return math.exp(log_beta) * _betacf(a, b, x) / a
    return 1.0 - math.exp(log_beta) * _betacf(b, a, 1.0 - x) / b


def _t_critical_95(freedom: int) -> float:
    """Return the two-sided 95% Student t critical value for positive degrees of freedom."""
    if freedom < 1:
        # A t distribution is defined here only after a sample supplies positive df.
        raise ValueError("Degrees of freedom must be positive.")
    lower, upper = 0.0, 1.0
    # The beta expression is the two-sided tail probability for a positive t value.
    while _betai(freedom / 2.0, 0.5, freedom / (freedom + upper**2)) > 0.05:
        upper *= 2.0
    for _ in range(80):
        # Bisection is deterministic and amply precise for one-decimal leaderboard output.
        midpoint = (lower + upper) / 2.0
        tail = _betai(freedom / 2.0, 0.5, freedom / (freedom + midpoint**2))
        if tail > 0.05:
            lower = midpoint
        else:
            upper = midpoint
    # The midpoint of the final bracket avoids consistently choosing either bound.
    return (lower + upper) / 2.0


def paired_t(differences: list[float]) -> tuple[float, int, float, float, float, float] | None:
    """Calculate a paired t result as t, df, p, mean, effect size, and CI half-width."""
    if len(differences) < 2:
        # A variance estimate requires at least two paired observations.
        return None
    mean = statistics.mean(differences)
    deviation = statistics.stdev(differences)
    if deviation == 0:
        # Preserve a useful deterministic result for identical differences.
        return (
            float("inf") if mean else 0.0,
            len(differences) - 1,
            0.0 if mean else 1.0,
            mean,
            0.0,
            0.0,
        )
    error = deviation / math.sqrt(len(differences))
    statistic = mean / error
    freedom = len(differences) - 1
    probability = _betai(freedom / 2.0, 0.5, freedom / (freedom + statistic**2))
    # Small samples require the heavier-tailed t critical value rather than 1.96.
    half_width = _t_critical_95(freedom) * error
    return statistic, freedom, probability, mean, mean / deviation, half_width
