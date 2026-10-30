"""Deterministic profiling of attached datasets: the LLM gets facts, not raw megabytes."""

from __future__ import annotations

import csv
import json
import re
from collections import Counter
from pathlib import Path

WKT = re.compile(r"^\s*(POINT|LINESTRING|POLYGON|MULTIPOINT|MULTILINESTRING|MULTIPOLYGON|GEOMETRYCOLLECTION)\b", re.I)


def _largest_array(node, path=""):
    best = (path, node) if isinstance(node, list) else (None, None)
    if isinstance(node, dict):
        for k, v in node.items():
            p, a = _largest_array(v, f"{path}.{k}" if path else k)
            if a is not None and (best[1] is None or len(a) > len(best[1])):
                best = (p, a)
    return best


def _kind(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, str):
        if WKT.match(v):
            return "wkt"
        if re.match(r"^\d{4}-\d{2}-\d{2}T", v):
            return "datetime"
        return "string"
    if isinstance(v, list):
        return "array"
    return "object"


def _profile_records(records: list[dict], prefix: str = "", depth: int = 0) -> list[str]:
    lines: list[str] = []
    keys: Counter = Counter()
    for r in records:
        if isinstance(r, dict):
            keys.update(r.keys())
    n = len(records)
    for key, cnt in keys.most_common(40):
        values = [r.get(key) for r in records if isinstance(r, dict)]
        kinds = Counter(_kind(v) for v in values)
        kind = "/".join(k for k, _ in kinds.most_common())
        info = f"- `{prefix}{key}`: {kind}, заполнено {cnt}/{n}"
        non_null = [v for v in values if v is not None]
        if kinds.get("wkt"):
            types = Counter(WKT.match(v).group(1).upper() for v in non_null if isinstance(v, str) and WKT.match(v))
            info += f"; геометрия WKT, типы: {dict(types)}"
        elif all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in non_null) and non_null:
            distinct = len(set(non_null))
            info += f"; min={min(non_null)}, max={max(non_null)}, различных={distinct}"
            if distinct <= 8:
                info += f", значения: {sorted(set(non_null))}"
        elif all(isinstance(v, str) for v in non_null) and non_null:
            distinct = Counter(non_null)
            info += f"; различных={len(distinct)}"
            if len(distinct) <= 6:
                info += f", значения: {[k[:120] for k in distinct]}"
            else:
                info += f", примеры: {[k[:80] for k in list(distinct)[:3]]}"
            if len(distinct) == len(non_null):
                info += " (уникальный ключ)"
        elif all(isinstance(v, bool) for v in non_null) and non_null:
            info += f"; распределение: {dict(Counter(non_null))}"
        lines.append(info)
        if depth < 2:
            nested = [v for v in non_null if isinstance(v, dict)]
            if nested:
                lines += _profile_records(nested, f"{prefix}{key}.", depth + 1)
            arrays = [x for v in non_null if isinstance(v, list) for x in v if isinstance(x, dict)]
            if arrays:
                lines.append(f"  - `{prefix}{key}[]`: массив объектов, всего элементов {len(arrays)}")
                lines += _profile_records(arrays, f"{prefix}{key}[].", depth + 1)
    return lines


def _insights(records: list[dict]) -> list[str]:
    out = []
    geo = Counter()
    for r in records:
        for v in (r.values() if isinstance(r, dict) else []):
            if isinstance(v, str) and (m := WKT.match(v)):
                geo[m.group(1).upper()] += 1
    if geo:
        lines = geo.get("LINESTRING", 0) + geo.get("MULTILINESTRING", 0)
        if lines > sum(geo.values()) / 2:
            out.append("Большинство геометрий — линии, а не полигоны: «нахождение в зоне» нужно определять по "
                       "расстоянию до линии (буфер), а не попаданием точки в полигон.")
        if geo.get("GEOMETRYCOLLECTION"):
            out.append(f"Есть GEOMETRYCOLLECTION ({geo['GEOMETRYCOLLECTION']} шт.) — смешанные геометрии.")
    return out


def profile_file(path: Path) -> str:
    head = f"### Файл `{path.name}` ({path.stat().st_size // 1024} КБ)"
    try:
        if path.suffix.lower() == ".json":
            data = json.loads(path.read_text(encoding="utf-8"))
            rp, records = _largest_array(data)
            if records is None:
                return head + "\nJSON без массивов записей."
            top = list(data.keys()) if isinstance(data, dict) else []
            lines = [head, f"Корневые ключи: {top}. Записи: путь `{rp}`, количество {len(records)}."]
            lines += _profile_records(records)
            ins = _insights(records)
            if ins:
                lines.append("Наблюдения:")
                lines += [f"- {x}" for x in ins]
            return "\n".join(lines)
        if path.suffix.lower() in (".csv", ".tsv"):
            with path.open(encoding="utf-8") as f:
                rows = list(csv.DictReader(f, delimiter="\t" if path.suffix == ".tsv" else ","))
            return "\n".join([head, f"CSV, строк {len(rows)}."] + _profile_records(rows))
    except Exception as e:  # profile is advisory
        return f"{head}\nНе удалось разобрать: {e}"
    return head + "\nФормат не поддерживается профилировщиком."


def profile_dir(data_dir: Path | None) -> str:
    if not data_dir or not data_dir.exists():
        return ""
    parts = [profile_file(p) for p in sorted(data_dir.iterdir()) if p.is_file() and not p.name.startswith(".")]
    return "\n\n".join(parts)
