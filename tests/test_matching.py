"""Property tests: the overlap resolver must maximise verified points."""
import itertools
import random

from app.plan import _maximum_matching


class _R(dict):
    def __getitem__(self, key):
        return dict.__getitem__(self, key)


def _brute_force(node_count: int, candidates: dict[int, list[int]]) -> int:
    """Largest number of points that can each get a distinct reading."""
    best = 0
    points = sorted(candidates)
    for size in range(len(points), 0, -1):
        for chosen in itertools.combinations(points, size):
            options = [candidates[p] for p in chosen]
            if not options:
                continue
            if any(not o for o in options):
                continue
            for assignment in itertools.product(*options):
                if len(set(assignment)) == len(assignment):
                    return size
    return best


def test_matching_matches_brute_force_on_random_graphs():
    rng = random.Random(20260101)
    for trial in range(400):
        point_count = rng.randint(0, 6)
        reading_count = rng.randint(0, 6)
        candidates: dict[int, list] = {}
        for pid in range(1, point_count + 1):
            chosen = sorted(rng.sample(range(1, reading_count + 1),
                                       rng.randint(0, reading_count)))
            candidates[pid] = [
                _R(id=rid, account="A", meter="M",
                    read_at=f"2026-01-01T00:{rid:02d}:00Z", reading_cum=0.0)
                for rid in chosen
            ]
        eligible = [{"id": pid} for pid in sorted(candidates)]
        matched = _maximum_matching(eligible, candidates)
        brute = _brute_force(reading_count, {
            pid: [r["id"] for r in rows] for pid, rows in candidates.items()})
        assert len(matched) == brute, (trial, candidates, matched, brute)
        # No reading is used twice.
        used = [r["id"] for r in matched.values()]
        assert len(used) == len(set(used))
        # Every matched reading was actually a candidate for that point.
        for pid, reading in matched.items():
            assert reading["id"] in {r["id"] for r in candidates[pid]}


def test_matching_handles_long_augmenting_chain_without_recursion_error():
    # Point i can use reading i or i+1, but its *preferred* candidate is i+1,
    # so a greedy pass binds readings 2..n and strands the last point.  Only a
    # full alternating-path search verifies all n points.  This also proves the
    # iterative implementation survives a chain thousands of edges long.
    n = 4000
    candidates: dict[int, list] = {}
    for i in range(1, n + 1):
        ids = [i + 1, i] if i < n else [i]
        candidates[i] = [
            _R(id=rid, account="A", meter="M",
               read_at=f"2026-01-01T00:{rid:06d}", reading_cum=0.0)
            for rid in ids
        ]
    eligible = [{"id": pid} for pid in range(1, n + 1)]
    matched = _maximum_matching(eligible, candidates)
    assert len(matched) == n
    assert len({r["id"] for r in matched.values()}) == n
    # The stranded end of the chain is resolvable: all points get a reading.
    assert all(pid in matched for pid in range(1, n + 1))
