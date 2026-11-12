import logging
import os

import requests

logger = logging.getLogger(__name__)

TRAC_EVENTS_URL = os.environ.get("TRAC_EVENTS_URL", "http://127.0.0.1:8000/api/events")


def report_to_trac(
    event_type: str,
    source: str,
    summary: str,
    repo: str | None = None,
    ref: str | None = None,
    actor: str | None = None,
) -> None:
    try:
        response = requests.post(
            TRAC_EVENTS_URL,
            json={
                "type": event_type,
                "source": source,
                "summary": summary,
                "repo": repo,
                "ref": ref,
                "actor": actor,
            },
            timeout=3,
        )
        response.raise_for_status()
    except Exception as exc:
        # Reporting must never break a review, but failures should be visible.
        logger.warning("Could not report event to TRAC at %s: %s", TRAC_EVENTS_URL, exc)
