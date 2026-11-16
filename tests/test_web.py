"""Web interface tests: auth, CSRF, CRUD, CSV, dashboard, logs."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import BarcodeMapping
from tests.conftest import build_settings
from tests.helpers import save_barcode


@pytest.fixture
def app(settings: Settings):
    return create_app(settings)


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


def extract_csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "CSRF token not found in page"
    return match.group(1)


def login(client: TestClient, password: str = "test-password") -> None:
    page = client.get("/login")
    token = extract_csrf(page.text)
    response = client.post(
        "/login",
        data={"username": "admin", "password": password, "csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 303


def page_token(client: TestClient, path: str = "/barcodes") -> str:
    response = client.get(path)
    assert response.status_code == 200
    return extract_csrf(response.text)


def test_unauthorized_redirect_and_public_health(client: TestClient) -> None:
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"

    assert client.get("/healthz").status_code == 200
    assert client.get("/login").status_code == 200
    assert client.get("/static/vendor/bootstrap.min.css").status_code == 200


def test_security_headers(client: TestClient) -> None:
    response = client.get("/login")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    csp = response.headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "unsafe-inline" not in csp


def test_login_logout(client: TestClient) -> None:
    login(client)
    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert "Панель управления" in dashboard.text

    token = page_token(client, "/")
    logout = client.post("/logout", data={"csrf_token": token}, follow_redirects=False)
    assert logout.status_code == 303
    assert client.get("/", follow_redirects=False).status_code == 303


def test_wrong_password_and_login_rate_limit(client: TestClient) -> None:
    for _ in range(5):
        page = client.get("/login")
        token = extract_csrf(page.text)
        response = client.post(
            "/login",
            data={"username": "admin", "password": "wrong", "csrf_token": token},
        )
        assert response.status_code == 400
        assert "Неверное имя пользователя или пароль" in response.text

    page = client.get("/login")
    token = extract_csrf(page.text)
    blocked = client.post(
        "/login",
        data={"username": "admin", "password": "test-password", "csrf_token": token},
    )
    assert blocked.status_code == 429


def test_csrf_is_required(client: TestClient) -> None:
    page = client.get("/login")
    token = extract_csrf(page.text)
    rejected = client.post(
        "/login",
        data={"username": "admin", "password": "test-password"},
    )
    assert rejected.status_code == 403

    login(client)
    token = page_token(client, "/barcodes")
    assert (
        client.post("/barcodes", data={"barcode": "1", "surname": "Иванов"}).status_code
        == 403
    )
    assert (
        client.post(
            "/barcodes",
            data={"barcode": "1", "surname": "Иванов", "csrf_token": "wrong-token"},
        ).status_code
        == 403
    )
    accepted = client.post(
        "/barcodes",
        data={"barcode": "1", "surname": "Иванов", "csrf_token": token},
        follow_redirects=False,
    )
    assert accepted.status_code == 303


def test_barcode_crud_and_search(app, client: TestClient) -> None:
    login(client)
    token = page_token(client, "/barcodes")
    response = client.post(
        "/barcodes",
        data={"barcode": "000123", "surname": "Иванов", "csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    listing = client.get("/barcodes?q=000123&field=barcode")
    assert "000123" in listing.text
    assert "Иванов" in listing.text

    # Duplicate is rejected without overwriting.
    token = page_token(client, "/barcodes")
    client.post(
        "/barcodes",
        data={"barcode": "000123", "surname": "Петров", "csrf_token": token},
        follow_redirects=False,
    )
    listing = client.get("/barcodes")
    assert "уже существует" in listing.text
    with app.state.db.session() as session:
        assert session.query(BarcodeMapping).one().surname == "Иванов"

    item_id = None
    with app.state.db.session() as session:
        item_id = session.query(BarcodeMapping).one().id

    edit_page = client.get(f"/barcodes/{item_id}/edit")
    assert edit_page.status_code == 200
    token = extract_csrf(edit_page.text)
    edited = client.post(
        f"/barcodes/{item_id}/edit",
        data={"barcode": "000124", "surname": "Сидоров", "csrf_token": token},
        follow_redirects=False,
    )
    assert edited.status_code == 303
    assert "Сидоров" in client.get("/barcodes").text

    # Delete requires the confirmation page and confirm=yes.
    confirm_page = client.get(f"/barcodes/{item_id}/delete")
    assert "Подтверждение удаления" in confirm_page.text
    token = extract_csrf(confirm_page.text)
    client.post(
        f"/barcodes/{item_id}/delete",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    with app.state.db.session() as session:
        assert session.query(BarcodeMapping).count() == 1

    token = page_token(client, "/barcodes")
    client.post(
        f"/barcodes/{item_id}/delete",
        data={"csrf_token": token, "confirm": "yes"},
        follow_redirects=False,
    )
    with app.state.db.session() as session:
        assert session.query(BarcodeMapping).count() == 0


def test_invalid_surname_is_rejected(app, client: TestClient) -> None:
    login(client)
    token = page_token(client, "/barcodes")
    response = client.post(
        "/barcodes",
        data={"barcode": "1", "surname": "../evil", "csrf_token": token},
    )
    assert response.status_code == 400
    assert "недопустимые" in response.text
    with app.state.db.session() as session:
        assert session.query(BarcodeMapping).count() == 0


def test_csv_import_and_export(app, client: TestClient) -> None:
    login(client)
    token = page_token(client, "/barcodes")
    payload = "barcode,surname\n4601234567890,Иванов\n000999,Петров\n".encode()
    response = client.post(
        "/barcodes/import",
        files={"file": ("mapping.csv", payload, "text/csv")},
        data={"csrf_token": token},
    )
    assert response.status_code == 200
    assert "Добавлено" in response.text
    assert "2" in response.text
    with app.state.db.session() as session:
        assert session.query(BarcodeMapping).count() == 2

    export = client.get("/barcodes/export.csv")
    assert export.status_code == 200
    assert export.content.startswith(b"\xef\xbb\xbf")
    assert "4601234567890" in export.content.decode("utf-8-sig")
    assert export.headers["content-type"].startswith("text/csv")


def test_csv_import_bad_header_is_reported(client: TestClient) -> None:
    login(client)
    token = page_token(client, "/barcodes")
    response = client.post(
        "/barcodes/import",
        files={
            "file": ("bad.csv", "surname,barcode\nИванов,1\n".encode(), "text/csv")
        },
        data={"csrf_token": token},
    )
    assert response.status_code == 400
    assert "Ожидается заголовок" in response.text


def test_dashboard_and_logs_show_processing_results(app, client: TestClient) -> None:
    with app.state.db.session() as session:
        session.add(BarcodeMapping(barcode="WEB-001", surname="Иванов"))
    source = save_barcode(app.state.settings.input_dir / "web_scan.png", "WEB-001")
    outcome = app.state.processor.process(source)
    assert outcome.status == "sorted"

    login(client)
    dashboard = client.get("/")
    assert "Панель управления" in dashboard.text
    assert "WEB-001" in dashboard.text
    assert "Иванов" in dashboard.text
    assert "Отсортирован" in dashboard.text

    partial = client.get("/partials/dashboard")
    assert partial.status_code == 200
    assert "WEB-001" in partial.text
    assert "<html" not in partial.text

    logs = client.get("/logs")
    assert "web_scan.png" in logs.text
    assert "отсортирован" in logs.text.lower()

    filtered = client.get("/logs?status=sorted")
    assert "web_scan.png" in filtered.text


def test_scanner_controls_via_htmx(app, client: TestClient) -> None:
    login(client)
    token = page_token(client, "/")
    response = client.post(
        "/scan",
        data={"csrf_token": token},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200
    assert "Сканер не запущен" in response.text

    response = client.post(
        "/scan/pause",
        data={"csrf_token": token},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200
    assert app.state.scanner.paused is True
    assert "приостановлено" in response.text

    response = client.post(
        "/scan/resume",
        data={"csrf_token": token},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200
    assert app.state.scanner.paused is False


def test_health_and_404(client: TestClient) -> None:
    health = client.get("/healthz")
    assert health.status_code == 200
    assert health.json()["database"] is True
    assert health.json()["status"] == "ok"
    login(client)
    missing = client.get("/does-not-exist")
    assert missing.status_code == 404
    assert "404" in missing.text


def test_trusted_hosts_still_allow_internal_healthcheck(tmp_path) -> None:
    settings = build_settings(tmp_path, allowed_hosts="scans.example.local")
    app = create_app(settings)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/healthz").status_code == 200
        rejected = client.get("/healthz", headers={"Host": "evil.example.com"})
        assert rejected.status_code == 400


def test_auth_can_be_disabled_for_local_use() -> None:
    import tempfile
    from pathlib import Path

    base = Path(tempfile.mkdtemp())
    settings = build_settings(base, auth_enabled=False)
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
