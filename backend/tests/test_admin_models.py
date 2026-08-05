"""型号后台：不重建镜像就能接入新型号。

体检结论（2026-08-05）：型号此前只能来自 knowledge/diagnostic_flows.json 的
同步，而该目录 COPY 进镜像——加一个型号＝改仓库 + 重 build + 重 deploy。
"售后主管接入自家型号"是这个产品的核心场景，不该是发版动作。
"""

from __future__ import annotations

from sqlalchemy import select

from app.flow_catalog import load_flow_catalog, sync_flow_catalog
from app.models import RobotModel
from conftest import auth, register
from test_admin_api import make_admin


def _admin_token(client) -> str:
    return make_admin(client, "model-admin@example.com")


def test_admin_can_create_model_and_users_see_it(client):
    token = _admin_token(client)

    created = client.post(
        "/api/v1/admin/models",
        json={"code": "SR-X1", "name": "首如 X1 扫拖一体机", "brand": "首如"},
        headers=auth(token),
    )

    assert created.status_code == 201
    body = created.json()
    assert body["code"] == "SR-X1"
    assert body["brand"] == "首如"
    assert body["active"] is True

    # 用户端下拉框立刻能看到，不需要重建镜像
    codes = {item["code"] for item in client.get("/api/v1/models").json()}
    assert "SR-X1" in codes


def test_duplicate_code_is_rejected(client):
    token = _admin_token(client)
    payload = {"code": "SR-X2", "name": "首如 X2", "brand": "首如"}

    assert client.post("/api/v1/admin/models", json=payload, headers=auth(token)).status_code == 201
    duplicate = client.post("/api/v1/admin/models", json=payload, headers=auth(token))

    assert duplicate.status_code == 409


def test_invalid_code_is_rejected(client):
    """型号码进 URL 与检索缓存键，不允许空格/斜杠等歧义字符。"""
    token = _admin_token(client)

    for bad_code in ("has space", "sl/ash", "", "-leading"):
        response = client.post(
            "/api/v1/admin/models",
            json={"code": bad_code, "name": "非法型号"},
            headers=auth(token),
        )
        assert response.status_code == 422, bad_code


def test_admin_can_rename_model_without_touching_code(client):
    token = _admin_token(client)
    model_id = client.post(
        "/api/v1/admin/models",
        json={"code": "SR-X3", "name": "旧名字", "brand": "首如"},
        headers=auth(token),
    ).json()["id"]

    updated = client.patch(
        f"/api/v1/admin/models/{model_id}",
        json={"name": "新名字", "brand": "首如智能"},
        headers=auth(token),
    )

    assert updated.status_code == 200
    assert updated.json()["name"] == "新名字"
    assert updated.json()["brand"] == "首如智能"
    # code 不可改：它是知识文档与已发出报告的对外标识
    assert updated.json()["code"] == "SR-X3"
    assert updated.json()["active"] is True


def test_partial_update_does_not_clobber_other_fields(client):
    """只传 active 不该把名称清空——PATCH 语义必须是部分更新。"""
    token = _admin_token(client)
    model_id = client.post(
        "/api/v1/admin/models",
        json={"code": "SR-X4", "name": "保持这个名字", "brand": "首如"},
        headers=auth(token),
    ).json()["id"]

    response = client.patch(
        f"/api/v1/admin/models/{model_id}", json={"active": False}, headers=auth(token)
    )

    assert response.status_code == 200
    assert response.json()["name"] == "保持这个名字"
    assert response.json()["brand"] == "首如"
    assert response.json()["active"] is False


def test_deactivated_model_disappears_from_public_list(client):
    token = _admin_token(client)
    model_id = client.post(
        "/api/v1/admin/models",
        json={"code": "SR-X5", "name": "待停用型号"},
        headers=auth(token),
    ).json()["id"]

    client.patch(
        f"/api/v1/admin/models/{model_id}", json={"active": False}, headers=auth(token)
    )

    codes = {item["code"] for item in client.get("/api/v1/models").json()}
    assert "SR-X5" not in codes
    # 但管理端仍看得到，历史数据可查
    admin_codes = {
        item["code"] for item in client.get("/api/v1/admin/models", headers=auth(token)).json()
    }
    assert "SR-X5" in admin_codes


def test_manual_model_survives_catalog_sync(client):
    """镜像里的流程目录同步不得覆盖后台手工建的型号。

    否则每次重启容器，运营在后台建的型号就会被 JSON 里的定义顶掉——
    等于把"不用发版"这件事又还回去了。
    """
    token = _admin_token(client)
    client.post(
        "/api/v1/admin/models",
        json={"code": "SR-X6", "name": "手工建的型号", "brand": "首如"},
        headers=auth(token),
    )

    with client.app.state.session_factory() as db:
        sync_flow_catalog(db, load_flow_catalog())
        db.commit()
        survivor = db.scalar(select(RobotModel).where(RobotModel.code == "SR-X6"))

        assert survivor is not None
        assert survivor.name == "手工建的型号"
        assert survivor.brand == "首如"


def test_non_admin_cannot_create_model(client):
    user_token = register(client, "plain-user@example.com")["access_token"]

    response = client.post(
        "/api/v1/admin/models",
        json={"code": "SR-X7", "name": "越权型号"},
        headers=auth(user_token),
    )

    assert response.status_code in (401, 403)
    codes = {item["code"] for item in client.get("/api/v1/models").json()}
    assert "SR-X7" not in codes


def test_model_creation_is_audited(client):
    token = _admin_token(client)
    client.post(
        "/api/v1/admin/models",
        json={"code": "SR-X8", "name": "审计型号"},
        headers=auth(token),
    )

    audits = client.get("/api/v1/admin/audit-logs", headers=auth(token)).json()
    created = [item for item in audits if item["action"] == "robot_model.created"]

    assert created, "建型号必须留审计"
    assert created[0]["details_json"]["code"] == "SR-X8"
