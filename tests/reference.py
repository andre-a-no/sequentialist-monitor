# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""References the tests compare with, computed independently of sequential-analysis."""
import math


def normal_cdf(x):
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def cusum_arl(h, k=0.5, mu=0.0, states=200):
    """ARL of the one-sided CUSUM W = max(0, W + X - k), alarm at W >= h, X ~ N(mu, 1), from W = 0.

    Brook and Evans (1972): a Markov chain on the atom at 0 and `states - 1` intervals of width w below h;
    the ARL solves (I - P) a = 1.
    """
    n = states
    w = 2.0 * h / (2 * n - 1)
    rows = []
    for i in range(n):
        m = i * w
        row = [0.0] * n
        row[0] = normal_cdf(k - m + w / 2 - mu)
        for j in range(1, n):
            row[j] = normal_cdf((j + 0.5) * w - m + k - mu) - normal_cdf((j - 0.5) * w - m + k - mu)
        rows.append(row)
    # (I - P) a = 1 by Gaussian elimination with partial pivoting
    a = [[(1.0 if i == j else 0.0) - rows[i][j] for j in range(n)] + [1.0] for i in range(n)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(a[r][c]))
        a[c], a[p] = a[p], a[c]
        pivot = a[c][c]
        for r in range(c + 1, n):
            f = a[r][c] / pivot
            if f:
                ar, ac = a[r], a[c]
                for j in range(c, n + 1):
                    ar[j] -= f * ac[j]
    x = [0.0] * n
    for i in range(n - 1, -1, -1):
        x[i] = (a[i][n] - sum(a[i][j] * x[j] for j in range(i + 1, n))) / a[i][i]
    return x[0]


def run_length(procedure, draw, limit):
    """Steps until `procedure` stops on inputs `draw()`, at most `limit`."""
    state = procedure.initial_state()
    for t in range(1, limit + 1):
        state, _ = procedure.step(state, draw())
        if procedure.is_terminal(state):
            return t
    return limit


def mean_and_error(values):
    n = len(values)
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    return mean, math.sqrt(var / n)
