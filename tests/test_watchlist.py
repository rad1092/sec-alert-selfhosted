from __future__ import annotations

import re


def extract_csrf_token(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match is not None
    return match.group(1)


def test_watchlist_crud(client):
    response = client.get("/watchlist")
    assert response.status_code == 200
    csrf_token = extract_csrf_token(response.text)

    create_response = client.post(
        "/watchlist",
        data={
            "csrf_token": csrf_token,
            "ticker": "MSFT",
            "issuer_cik": "0000789019",
            "manual_cik_override": "",
            "issuer_name": "Microsoft Corporation",
            "enabled": "on",
        },
        follow_redirects=True,
    )
    assert create_response.status_code == 200
    assert "MSFT" in create_response.text

    toggle_csrf = extract_csrf_token(create_response.text)
    toggle_response = client.post(
        "/watchlist/1/toggle",
        data={"csrf_token": toggle_csrf},
        follow_redirects=True,
    )
    assert toggle_response.status_code == 200
    assert "Paused" in toggle_response.text

    delete_csrf = extract_csrf_token(toggle_response.text)
    delete_response = client.post(
        "/watchlist/1/delete",
        data={"csrf_token": delete_csrf},
        follow_redirects=True,
    )
    assert delete_response.status_code == 200
    assert "No companies added yet." in delete_response.text
