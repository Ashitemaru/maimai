"""Compare writable personal-score fields without changing the official snapshot."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

SCORE_FIELDS = ("achievements", "fc", "fs", "dxScore")
BADGE_ORDER = {
    "fc": ("", "fc", "fcp", "ap", "app"),
    "fs": ("", "sync", "fs", "fsp", "fsd", "fsdp"),
}


def chart_identity(record):
    song_id, difficulty = record.get("song_id"), record.get("level_index")
    if (
        isinstance(song_id, bool)
        or not str(song_id).isdigit()
        or int(song_id) <= 0
        or type(difficulty) is not int
        or difficulty < 0
    ):
        raise ValueError("invalid chart identity")
    return int(song_id), difficulty


def score_values(record):
    values = {field: record[field] for field in SCORE_FIELDS}
    achievement = values["achievements"]
    if isinstance(achievement, bool) or not isinstance(achievement, int | float):
        raise ValueError("invalid achievement")
    try:
        precise = Decimal(str(achievement))
        if not precise.is_finite() or not 0 <= precise <= 1000:
            raise ValueError("invalid achievement")
    except InvalidOperation:
        raise ValueError("invalid achievement") from None
    if type(values["dxScore"]) is not int or values["dxScore"] < 0:
        raise ValueError("invalid DX score")
    for field, order in BADGE_ORDER.items():
        if values[field] not in order:
            raise ValueError("unknown score badge")
    return values


def field_rank(field, value):
    if field in BADGE_ORDER:
        return BADGE_ORDER[field].index(value)
    return Decimal(str(value))


def personal_records(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
        raise ValueError("invalid personal score response")
    result = {}
    for record in payload["records"]:
        if not isinstance(record, dict):
            raise ValueError("invalid personal score")
        identity = chart_identity(record)
        if identity in result:
            raise ValueError("duplicate personal score")
        result[identity] = score_values(record)
    return result


@dataclass
class UploadPlan:
    body: list
    expected: dict
    counts: dict


def plan_upload(candidates, before):
    """Best achievement, combo, sync and DX score are independent historical maxima."""
    expected = {key: dict(values) for key, values in before.items()}
    body = []
    counts = {
        "compared_count": len(candidates),
        "remote_records_before": len(before),
        "new_count": 0,
        "improved_count": 0,
        "unchanged_count": 0,
        "preserved_count": 0,
        "outside_upload_count": len(set(before) - {chart_identity(c) for c in candidates}),
    }
    seen = set()
    for record in candidates:
        identity = chart_identity(record)
        if identity in seen:
            raise ValueError("duplicate upload candidate")
        seen.add(identity)
        values = score_values(record)
        previous = before.get(identity)
        if previous is None:
            counts["new_count"] += 1
        else:
            protected = [
                field
                for field in SCORE_FIELDS
                if field_rank(field, previous[field]) > field_rank(field, values[field])
            ]
            if protected:
                counts["preserved_count"] += 1
                values.update({field: previous[field] for field in protected})
            if values == previous:
                counts["unchanged_count"] += 1
                continue
            counts["improved_count"] += 1
        if identity[0] < 100000 and values["achievements"] > 101:
            raise ValueError("ordinary achievement would be clamped by the upload API")
        expected[identity] = values
        # Only send fields accepted by the write API, plus the stable ID for auditing.
        body.append(
            {
                "song_id": identity[0],
                "level_index": identity[1],
                "title": record["title"],
                "type": record["type"],
                **values,
            }
        )
    return UploadPlan(body, expected, counts)


def verify_records(expected, actual):
    """Allow newer improvements, but require every old and submitted value to survive."""
    differences = []
    for identity, required in expected.items():
        found = actual.get(identity)
        fields = (
            [
                field
                for field in SCORE_FIELDS
                if field_rank(field, found[field]) < field_rank(field, required[field])
            ]
            if found is not None
            else ["record"]
        )
        if fields:
            differences.append(
                {"song_id": identity[0], "level_index": identity[1], "fields": fields}
            )
    return differences
