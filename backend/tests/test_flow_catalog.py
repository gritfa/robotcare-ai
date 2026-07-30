from copy import deepcopy
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.flow_catalog import (
    FlowCatalog,
    PublishedFlowMutationError,
    load_flow_catalog,
    sync_flow_catalog,
)
from app.models import DiagnosticFlow
from conftest import auth, register


EXPECTED_FLOWS = {
    ("JH69U1", "return_to_dock_failure"): "jh69u1-return-to-dock",
    ("JH69U1", "base_station_water_tank_issue"): "jh69u1-mop-washing",
    ("VC35U1", "wifi_setup_failure"): "vc35u1-wifi-setup",
    ("VC35U1", "cleaning_noise"): "vc35u1-cleaning-noise",
}


def create_device(client, token: str, model_code: str) -> int:
    model_id = next(
        item["id"] for item in client.get("/api/v1/models").json() if item["code"] == model_code
    )
    response = client.post(
        "/api/v1/devices",
        headers=auth(token),
        json={"robot_model_id": model_id, "nickname": f"{model_code} 流程测试"},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def create_diagnostic(client, token: str, device_id: int, category: str):
    return client.post(
        "/api/v1/diagnostics",
        headers=auth(token),
        json={
            "device_id": device_id,
            "issue_category_code": category,
            "issue_description": "需要执行经过审核的安全排查流程",
        },
    )


def test_catalog_contains_four_published_reviewed_flows_with_page_sources():
    catalog = load_flow_catalog()
    assert len(catalog.flows) == 30  # 5 条真实型号流程 + 25 条 D1 合成演示流程
    published = [flow for flow in catalog.flows if flow.status == "published"]
    drafts = [flow for flow in catalog.flows if flow.status == "draft"]
    real_published = [flow for flow in published if not flow.stable_key.startswith("rc-")]
    synthetic_published = [flow for flow in published if flow.stable_key.startswith("rc-")]
    assert {(flow.model_code, flow.issue_category_code) for flow in real_published} == set(EXPECTED_FLOWS)
    assert len(synthetic_published) == 25
    assert [(flow.model_code, flow.issue_category_code) for flow in drafts] == [
        ("VC35U1", "navigation_abnormal")
    ]
    for flow in published:
        assert flow.status == "published"
        assert flow.version == 1
        assert flow.reviewed_at is not None
        assert flow.reviewed_by
        assert [step.position for step in flow.steps] == list(range(1, len(flow.steps) + 1))
        assert all(step.source_page > 0 for step in flow.steps)
        assert all(step.evidence_level in {"direct", "partial"} for step in flow.steps)
        assert all(step.evidence_basis for step in flow.steps)
    for flow in real_published:
        assert all(step.source_url.startswith("https://download.haier.com/") for step in flow.steps)
    for flow in synthetic_published:
        assert all(step.source_url.startswith("synthetic://robotcare-demo/") for step in flow.steps)


def test_all_four_published_flows_are_available_one_step_at_a_time(client):
    token = register(client, "four-flows@example.com")["access_token"]
    devices = {
        model_code: create_device(client, token, model_code)
        for model_code in {model_code for model_code, _ in EXPECTED_FLOWS}
    }

    for (model_code, category), stable_key in EXPECTED_FLOWS.items():
        response = create_diagnostic(client, token, devices[model_code], category)
        assert response.status_code == 201, response.text
        diagnostic = response.json()
        assert diagnostic["flow_stable_key"] == stable_key
        assert diagnostic["flow_version"] == 1
        step = client.get(
            f"/api/v1/diagnostics/{diagnostic['id']}/steps/current", headers=auth(token)
        )
        assert step.status_code == 200
        assert step.json()["position"] == 1
        assert step.json()["source_page"] > 0
        assert step.json()["source_url"].startswith("https://download.haier.com/")
        assert step.json()["evidence_level"] in {"direct", "partial"}


def test_draft_navigation_flow_is_not_available_to_regular_users(client):
    token = register(client, "draft-flow@example.com")["access_token"]
    device_id = create_device(client, token, "VC35U1")
    response = create_diagnostic(client, token, device_id, "navigation_abnormal")
    assert response.status_code == 404


def test_diagnostic_options_expose_only_published_flows_for_selected_model(client):
    token = register(client, "flow-options@example.com")["access_token"]
    models = client.get("/api/v1/models").json()
    jh_id = next(item["id"] for item in models if item["code"] == "JH69U1")
    vc_id = next(item["id"] for item in models if item["code"] == "VC35U1")

    jh = client.get(f"/api/v1/models/{jh_id}/diagnostic-options", headers=auth(token))
    vc = client.get(f"/api/v1/models/{vc_id}/diagnostic-options", headers=auth(token))
    assert jh.status_code == 200
    assert vc.status_code == 200
    assert {item["issue_category_code"] for item in jh.json()} == {
        "return_to_dock_failure",
        "base_station_water_tank_issue",
    }
    assert {item["issue_category_code"] for item in vc.json()} == {
        "wifi_setup_failure",
        "cleaning_noise",
    }
    assert "navigation_abnormal" not in {
        item["issue_category_code"] for item in vc.json()
    }
    assert all(item["version"] == 1 for item in jh.json() + vc.json())


def catalog_with(flow) -> FlowCatalog:
    return FlowCatalog(
        schema_version="2.0",
        updated_at="2026-07-20",
        flows=[flow],
    )


def test_released_flow_cannot_be_changed_in_place(client):
    original = load_flow_catalog().flows[0]
    changed_payload = original.model_dump(mode="json")
    changed_payload["steps"][0]["instruction"] += " 未经版本升级的修改"
    changed = type(original).model_validate(changed_payload)

    with client.app.state.session_factory() as db:
        with pytest.raises(PublishedFlowMutationError):
            sync_flow_catalog(db, catalog_with(changed))


def test_new_published_version_retires_old_but_history_keeps_old_steps(client):
    token = register(client, "flow-version@example.com")["access_token"]
    device_id = create_device(client, token, "JH69U1")
    old_response = create_diagnostic(
        client, token, device_id, "return_to_dock_failure"
    )
    assert old_response.status_code == 201
    old_diagnostic = old_response.json()
    old_step = client.get(
        f"/api/v1/diagnostics/{old_diagnostic['id']}/steps/current", headers=auth(token)
    ).json()

    original = next(
        flow for flow in load_flow_catalog().flows if flow.stable_key == "jh69u1-return-to-dock"
    )
    version_two_payload = deepcopy(original.model_dump(mode="json"))
    version_two_payload["version"] = 2
    version_two_payload["title"] = "JH69U1 无法回充安全排查 v2"
    version_two_payload["reviewed_at"] = datetime.now(UTC).isoformat()
    version_two_payload["steps"][0]["instruction"] += " 完成后确认基站指示状态。"
    version_two = type(original).model_validate(version_two_payload)

    with client.app.state.session_factory() as db:
        sync_flow_catalog(db, catalog_with(version_two))
        versions = list(
            db.scalars(
                select(DiagnosticFlow)
                .where(DiagnosticFlow.stable_key == "jh69u1-return-to-dock")
                .order_by(DiagnosticFlow.version)
            )
        )
        assert [(flow.version, flow.status) for flow in versions] == [
            (1, "retired"),
            (2, "published"),
        ]

    new_response = create_diagnostic(client, token, device_id, "return_to_dock_failure")
    assert new_response.status_code == 201
    assert new_response.json()["flow_version"] == 2

    old_refreshed = client.get(
        f"/api/v1/diagnostics/{old_diagnostic['id']}", headers=auth(token)
    ).json()
    assert old_refreshed["flow_version"] == 1
    old_step_refreshed = client.get(
        f"/api/v1/diagnostics/{old_diagnostic['id']}/steps/current", headers=auth(token)
    ).json()
    assert old_step_refreshed["id"] == old_step["id"]
    assert old_step_refreshed["instruction"] == old_step["instruction"]
