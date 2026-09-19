"""Branded result-link construction.

Builds a public ``/c/<verification_id>`` page URL with ``utm_medium=mcp`` from a
claim ``verification_id``. Pure string-building — no DB, no ORM. The web app's
bare-``verification_id`` route 301-redirects to the canonical ``/c/<slug>`` while
preserving the query string, so the UTMs survive.

We only ever build a link when the API hands us a ``verification_url`` (assess
cache-hits), which the API returns ONLY for already-public claims — so the
public-gate is free, no visibility check needed here. Fresh assess results and
all verify results (private by default) get no link.
"""

from __future__ import annotations

from urllib.parse import urlencode, urlparse, urlunparse

from lenz_mcp import config


def _add_utm(url: str) -> str:
    parsed = urlparse(url)
    query = urlencode(
        {
            'utm_source': config.UTM_SOURCE,
            'utm_medium': config.UTM_MEDIUM,
            'utm_campaign': config.UTM_CAMPAIGN,
        }
    )
    return urlunparse(parsed._replace(query=query))


def branded_link(verification_id: str | None) -> str | None:
    """UTM-tagged public ``/c/<verification_id>`` URL for ``verification_id``, or None.

    None when ``verification_id`` is falsy. The caller is responsible for only
    passing a verification_id that came from a public result (an assess
    ``verification_url``).
    """
    if not verification_id:
        return None
    return _add_utm(f'{config.FRONTEND_URL}{config.CLAIM_PATH_TEMPLATE.format(slug=verification_id)}')


def verification_id_from_verification_url(verification_url: str | None) -> str | None:
    """Extract the trailing verification_id from an assess ``verification_url``.

    Shape: ``{FRONTEND_URL}/api/v1/verifications/<verification_id>``. Returns None for
    an empty/None url (fresh assess results have no verification_url).
    """
    if not verification_url:
        return None
    tail = verification_url.rstrip('/').rsplit('/', 1)[-1]
    return tail or None
