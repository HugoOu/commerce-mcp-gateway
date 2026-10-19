"""Step 4：跑合取门与分数规则的阈值 sweep，输出表与 SVG。

用法：`uv run python eval/score_sweep.py`。
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval"))

from score_policy import (
    conjunction,
    evaluate,
    load_scenarios,
    perfect_thresholds,
    score,
    sweep,
)

DIAGRAM = ROOT / "docs" / "diagrams" / "score-vs-conjunction.svg"


def _svg(rows: list[dict[str, float]]) -> str:
    width, height = 760, 380
    left, right, top, bottom = 64, 24, 44, 56
    chart_w = width - left - right
    chart_h = height - top - bottom
    max_y = max(1, max(int(row["errors"]) for row in rows))

    def x(threshold: float) -> float:
        return left + (threshold - 0.5) / 0.5 * chart_w

    def y(value: float) -> float:
        return top + chart_h - value / max_y * chart_h

    def poly(key: str, color: str) -> str:
        points = " ".join(f"{x(row['threshold']):.1f},{y(row[key]):.1f}" for row in rows)
        return f'<polyline fill="none" stroke="{color}" stroke-width="2" points="{points}" />'

    grid: list[str] = []
    for value in range(max_y + 1):
        yy = y(value)
        grid.append(f'<line x1="{left}" y1="{yy:.1f}" x2="{width - right}" y2="{yy:.1f}" stroke="#e5e7eb" />')
        grid.append(f'<text x="{left - 8}" y="{yy + 4:.1f}" text-anchor="end" font-size="11" fill="#6b7280">{value}</text>')
    for threshold in (0.5, 0.6, 0.7, 0.8, 0.9, 1.0):
        xx = x(threshold)
        grid.append(f'<line x1="{xx:.1f}" y1="{top}" x2="{xx:.1f}" y2="{height - bottom}" stroke="#f3f4f6" />')
        grid.append(f'<text x="{xx:.1f}" y="{height - bottom + 20}" text-anchor="middle" font-size="11" fill="#6b7280">{threshold:.1f}</text>')

    return "\n".join(
        [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
            '<rect width="100%" height="100%" fill="white" />',
            f'<text x="{left}" y="24" font-size="16" font-weight="600" fill="#111827">Conjunction gate vs weighted score</text>',
            *grid,
            poly("false_accept", "#dc2626"),
            poly("false_deny", "#2563eb"),
            f'<text x="{left}" y="{height - 12}" font-size="11" fill="#6b7280">threshold</text>',
            f'<text x="{width - 24}" y="{top + 12}" text-anchor="end" font-size="12" fill="#dc2626">false accept</text>',
            f'<text x="{width - 24}" y="{top + 28}" text-anchor="end" font-size="12" fill="#2563eb">false deny</text>',
            "</svg>",
        ]
    )


def main() -> int:
    scenarios = load_scenarios()
    baseline = evaluate(scenarios, conjunction)
    rows = sweep(scenarios)
    perfect = perfect_thresholds(rows)
    best = min(rows, key=lambda row: row["errors"])

    print("## 合取门")
    print(f"false_accept={baseline['false_accept']} false_deny={baseline['false_deny']} errors={baseline['errors']}")
    print()
    print("## 分数规则：每个场景的分数")
    print("| 场景 | 期望 | 分数 |")
    print("| --- | --- | --- |")
    for scenario in scenarios:
        print(f"| {scenario.id} | {scenario.expected} | {score(scenario):.2f} |")
    print()
    print("## 阈值 sweep")
    print("| threshold | false_accept | false_deny | errors |")
    print("| --- | --- | --- | --- |")
    for threshold in (0.80, 0.85, 0.90, 0.91, 0.95, 1.00):
        row = next(item for item in rows if item["threshold"] == threshold)
        print(
            f"| {threshold:.2f} | {int(row['false_accept'])} | {int(row['false_deny'])} | "
            f"{int(row['errors'])} |"
        )
    print()
    print(f"perfect_thresholds={perfect}")
    print(f"best_threshold={best['threshold']:.2f} errors={int(best['errors'])}")
    DIAGRAM.parent.mkdir(parents=True, exist_ok=True)
    DIAGRAM.write_text(_svg(rows), encoding="utf-8")
    print(f"diagram={DIAGRAM.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
