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
    execution_id: str | None = None,
    status: str | None = None,
    metadata: dict | None = None,
) -> dict:
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
                "execution_id": execution_id,
                "status": status,
                "metadata": metadata or {},
            },
            timeout=3,
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException as exc:
        logger.error(
            "Could not persist review event to TRAC at %s (%s)",
            TRAC_EVENTS_URL,
            type(exc).__name__,
        )
        raise RuntimeError("Could not persist review event to TRAC") from exc
    except ValueError as exc:
        logger.error("TRAC returned an invalid event response (%s)", type(exc).__name__)
        raise RuntimeError("Could not persist review event to TRAC") from exc
