"""GET /catalogue — what data this platform holds, in one call.

Built for a reader who has to decide whether a question is answerable here:
the assistant, mainly, but the answer is the same one a new contributor needs.

The two halves come from the two places that already know, so neither can
drift from reality:

  * ``src.etl.registry`` reads each producer's own ``DESCRIPTION``. A new
    pipeline appears here when it declares itself, with no edit to this file.
  * the Atlas stats source lists the statistical datasets, the same rows
    ``/atlas/datasets`` serves.

Both halves degrade independently. The stats store being unconfigured costs
the statistical half and leaves the graph half intact, because a caller that
learns about half the platform is far better off than one that gets a 503 and
concludes the platform holds nothing — which is the exact failure this
endpoint exists to prevent.
"""
from __future__ import annotations

from fastapi import APIRouter, Request

from src.etl.registry import discover, undescribed

router = APIRouter(prefix="/catalogue", tags=["catalogue"])


def _datasets(request: Request) -> list[dict]:
    """Statistical datasets, or an empty list if the stats store is absent."""
    src = getattr(request.app.state, "fontem_stats_source", None)
    if src is None or not getattr(src, "configured", False):
        return []
    try:
        rows = src.list_datasets()
    except Exception:  # pylint: disable=broad-except
        # Never let a dashboard dependency turn "what do you hold" into an
        # error. Half a catalogue is a useful answer; a 500 is not.
        return []
    out = []
    for row in rows:
        item = row if isinstance(row, dict) else getattr(row, "__dict__", {})
        if not item.get("enabled", True):
            continue
        out.append({
            "code": item.get("code"),
            "label": item.get("label"),
            "theme": item.get("theme"),
            "nuts_levels": item.get("nuts_levels"),
            "time_unit": item.get("time_unit"),
            "update_freq": item.get("update_freq"),
        })
    return out


@router.get("")
def get_catalogue(request: Request) -> dict:
    """Every described producer plus the statistical dataset catalogue."""
    producers = [d.as_dict() for d in discover()]
    datasets = _datasets(request)
    return {
        "producers": producers,
        "datasets": datasets,
        # Surfaced rather than hidden: a loader with no DESCRIPTION is data
        # the assistant will deny holding. Making the gap visible in the
        # payload is what keeps it from growing quietly.
        "undescribed_producers": undescribed(),
        "counts": {
            "producers": len(producers),
            "datasets": len(datasets),
        },
    }
