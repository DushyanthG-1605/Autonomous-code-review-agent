import requests


TRAC_EVENTS_URL = "http://127.0.0.1:8000/api/events"


def report_to_trac(
    event_type: str,
    source: str,
    summary: str,
    repo: str | None = None,
    ref: str | None = None,
    actor: str | None = None,
) -> None:
    try:
        requests.post(
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
    except Exception:
        pass