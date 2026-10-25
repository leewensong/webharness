"""v2.32：个人 3D 形象与房间场景统一走资源库 + 审核管线。

- 形象/场景接受 builtin / 外链 / library:<id>（approved 模型资源）三种来源。
- 未过审资源引用 → 409 resource_not_approved；非模型资源 → 409 resource_kind_mismatch。
- 代理下载按引用授权：资源主（或名下 Agent）或与目标账号同房间成员可读。
- 提交审核当前自动通过（scan clean 时）。
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import auth, db, main

GLB = b"glTF" + bytes([2, 0, 0, 0]) + b"\x00" * 64


def _glb(name="model.glb"):
    return (name, GLB, "model/gltf-binary")


class Model3dLibraryTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for module, name, value in (
            (db, "DATA_DIR", root),
            (db, "DB_PATH", root / "test.db"),
            (db, "UPLOADS_DIR", root / "uploads"),
            (db, "FILES_DIR", root / "files"),
            (db, "LIBRARY_DIR", root / "library"),
            (main, "UPLOADS_DIR", root / "uploads"),
            (main, "FILES_DIR", root / "files"),
            (main, "LIBRARY_DIR", root / "library"),
            (auth, "SECRET", b"isolated-model3d-library-secret"),
        ):
            self.enterContext(patch.object(module, name, value))
        self.enterContext(patch.dict(os.environ, {"WEBHARNESS_VERIFY_MODE": "off"}))
        self.client = self.enterContext(TestClient(main.app))
        with db.get_db() as conn:
            for uid, uname, is_sa in ((1, "alice", 0), (2, "bob", 0), (3, "admin", 1)):
                conn.execute(
                    "INSERT INTO users (id, username, password_hash, is_superadmin) VALUES (?, ?, ?, ?)",
                    (uid, uname, auth.hash_password(uname + "-pass"), is_sa),
                )
        self.alice = self._h(1, "alice")
        self.bob = self._h(2, "bob")
        self.admin = self._h(3, "admin")

    @staticmethod
    def _h(user_id, username, kind="human"):
        return {"Authorization": "Bearer " + auth.create_token(user_id, username, kind, 0)}

    def _mk_resource(self, headers, name="model.glb"):
        return self.client.post(
            "/api/library/resources", headers=headers, files={"file": _glb(name)}
        ).json()

    def _mk_approved_model(self, headers, name="model.glb"):
        resource = self._mk_resource(headers, name)
        self.assertIn("currentVersion", resource)
        vid = resource["currentVersion"]["id"]
        submitted = self.client.post(
            f"/api/library/resources/{resource['id']}/versions/{vid}/submit", headers=headers
        )
        self.assertEqual(submitted.status_code, 200, submitted.text)
        self.assertEqual(submitted.json()["version"]["reviewStatus"], "approved")
        return resource

    def test_submit_auto_approves_scan_clean_version(self):
        resource = self._mk_resource(self.alice)
        vid = resource["currentVersion"]["id"]
        queue = self.client.get("/api/admin/resource-reviews", headers=self.admin)
        self.assertEqual(queue.json()["reviews"], [])
        submitted = self.client.post(
            f"/api/library/resources/{resource['id']}/versions/{vid}/submit", headers=self.alice
        )
        self.assertEqual(submitted.json()["version"]["reviewStatus"], "approved")
        queue = self.client.get("/api/admin/resource-reviews", headers=self.admin)
        self.assertEqual(queue.json()["reviews"], [])

    def test_avatar_library_ref_requires_approved_model(self):
        resource = self._mk_resource(self.alice)
        rid = resource["id"]
        # draft 状态 → 409
        r = self.client.put("/api/me/model3d", headers=self.alice, json={"url": f"library:{rid}"})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(r.json()["detail"]["code"], "resource_not_approved")
        # 过审后 → 200
        vid = resource["currentVersion"]["id"]
        self.client.post(f"/api/library/resources/{rid}/versions/{vid}/submit", headers=self.alice)
        r = self.client.put(
            "/api/me/model3d", headers=self.alice, json={"url": f"library:{rid}", "humanoid": True}
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("/api/users/alice/model3d", r.json()["model3dUrl"])
        self.assertTrue(r.json()["model3dHumanoid"])

    def test_avatar_library_ref_rejects_non_model_and_foreign_or_missing(self):
        md = self.client.post(
            "/api/library/resources",
            headers=self.alice,
            files={"file": ("note.md", b"# x", "text/markdown")},
        ).json()
        vid = md["currentVersion"]["id"]
        self.client.post(f"/api/library/resources/{md['id']}/versions/{vid}/submit", headers=self.alice)
        # 文本资源不能作形象
        r = self.client.put("/api/me/model3d", headers=self.alice, json={"url": f"library:{md['id']}"})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(r.json()["detail"]["code"], "resource_kind_mismatch")
        # 他人资源不能引用（即使已过审）
        foreign = self._mk_approved_model(self.bob)
        r = self.client.put("/api/me/model3d", headers=self.alice, json={"url": f"library:{foreign['id']}"})
        self.assertEqual(r.status_code, 404, r.text)
        # 不存在的 id
        r = self.client.put("/api/me/model3d", headers=self.alice, json={"url": "library:9999"})
        self.assertEqual(r.status_code, 404, r.text)
        # 非数字引用直接 400
        r = self.client.put("/api/me/model3d", headers=self.alice, json={"url": "library:abc"})
        self.assertEqual(r.status_code, 400, r.text)

    def test_avatar_proxy_download_requires_shared_room(self):
        resource = self._mk_approved_model(self.alice)
        self.client.put("/api/me/model3d", headers=self.alice, json={"url": f"library:{resource['id']}"})
        # 资源主本人可读
        r = self.client.get("/api/users/alice/model3d", headers=self.alice)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.content, GLB)
        # 陌生人（无共同房间）→ 404，不泄露存在性
        r = self.client.get("/api/users/alice/model3d", headers=self.bob)
        self.assertEqual(r.status_code, 404, r.text)
        # 同房间成员可读
        self.client.post("/api/rooms", headers=self.alice, json={"roomName": "PubRoom", "visibility": "public"})
        self.client.post("/api/rooms", headers=self.bob, json={"roomName": "PubRoom"})
        r = self.client.get("/api/users/alice/model3d", headers=self.bob)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.content, GLB)

    def test_avatar_proxy_download_fails_when_review_revoked(self):
        resource = self._mk_approved_model(self.alice)
        vid = resource["currentVersion"]["id"]
        self.client.put("/api/me/model3d", headers=self.alice, json={"url": f"library:{resource['id']}"})
        # superadmin 打回 → 代理下载失效
        r = self.client.post(
            f"/api/admin/resource-reviews/{vid}", headers=self.admin, json={"decision": "rejected", "note": "x"}
        )
        self.assertEqual(r.status_code, 200, r.text)
        r = self.client.get("/api/users/alice/model3d", headers=self.alice)
        self.assertEqual(r.status_code, 404, r.text)

    def test_room_scene_library_ref_and_download(self):
        resource = self._mk_resource(self.alice)
        rid = resource["id"]
        vid = resource["currentVersion"]["id"]
        # draft → 409
        r = self.client.post(
            "/api/rooms", headers=self.alice, json={"roomName": "SDraft", "visibility": "public", "scene": {"kind": "library", "id": str(rid)}}
        )
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(r.json()["detail"]["code"], "resource_not_approved")
        # 过审后建房 → 200，描述符带代理 url
        self.client.post(f"/api/library/resources/{rid}/versions/{vid}/submit", headers=self.alice)
        r = self.client.post(
            "/api/rooms", headers=self.alice, json={"roomName": "S1", "visibility": "public", "scene": {"kind": "library", "id": str(rid)}}
        )
        self.assertEqual(r.status_code, 200, r.text)
        desc = r.json()["scene"]
        self.assertEqual(desc["kind"], "library")
        self.assertIn("/api/rooms/S1/scene", desc["url"])
        # 房主下载场景
        dl = self.client.get("/api/rooms/S1/scene", headers=self.alice)
        self.assertEqual(dl.status_code, 200, dl.text)
        self.assertEqual(dl.content, GLB)
        # 成员可下载；非成员 403
        self.client.post("/api/rooms", headers=self.bob, json={"roomName": "S1"})
        dl = self.client.get("/api/rooms/S1/scene", headers=self.bob)
        self.assertEqual(dl.status_code, 200, dl.text)
        stranger = self._h(9, "stranger")
        with db.get_db() as conn:
            conn.execute(
                "INSERT INTO users (id, username, password_hash) VALUES (9, 'stranger', ?)",
                (auth.hash_password("s-pass"),),
            )
        r = self.client.get("/api/rooms/S1/scene", headers=stranger)
        self.assertEqual(r.status_code, 403, r.text)

    def test_patch_room_to_library_scene_validates(self):
        r = self.client.post("/api/rooms", headers=self.alice, json={"roomName": "P1", "visibility": "public"})
        self.assertEqual(r.status_code, 200, r.text)
        resource = self._mk_approved_model(self.alice)
        r = self.client.patch(
            "/api/rooms/P1", headers=self.alice, json={"scene": {"kind": "library", "id": str(resource["id"])}}
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["scene"]["kind"], "library")
        # 场景更新为上传接口已删除
        r = self.client.post(
            "/api/rooms/P1/scene", headers=self.alice, files={"file": _glb()}
        )
        self.assertEqual(r.status_code, 405, r.text)
        # 形象直传接口已删除
        r = self.client.post("/api/me/model3d", headers=self.alice, files={"file": _glb()})
        self.assertEqual(r.status_code, 405, r.text)

    def test_builtin_and_external_avatar_still_work(self):
        avatars = self.client.get("/api/avatar-models").json()["avatars"]
        builtin = avatars[0]["id"]
        r = self.client.put("/api/me/model3d", headers=self.alice, json={"url": f"builtin:{builtin}"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["model3dUrl"], f"builtin:{builtin}")
        r = self.client.put(
            "/api/me/model3d", headers=self.alice, json={"url": "https://example.com/a.vrm", "arkit": True}
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["model3dUrl"], "https://example.com/a.vrm")
        r = self.client.put("/api/me/model3d", headers=self.alice, json={"url": "library:abc"})
        self.assertEqual(r.status_code, 400, r.text)


if __name__ == "__main__":
    unittest.main()
