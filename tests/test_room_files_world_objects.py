"""共同文件世界对象扩展（需求 13）回归测试：可摆放 kind、分类别上限、多格式模型分类、HLS 分类。

仅使用临时 SQLite 与临时目录，不修改真实服务数据。
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import auth, db, main

PNG_1PX = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa7\x35\x81\x84\x00\x00\x00\x00IEND\xaeB`\x82"
)
OBJ_TEXT = b"v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n"
M3U8_TEXT = b"#EXTM3U\n#EXT-X-VERSION:3\n#EXTINF:4.0,\nhttps://example.com/seg0.ts\n#EXT-X-ENDLIST\n"
POSE = {"visible": True, "position": [0.0, 0.5, -1.0]}


class RoomFilesWorldObjectTests(unittest.TestCase):
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
            (auth, "SECRET", b"isolated-world-objects-test-secret"),
        ):
            self.enterContext(patch.object(module, name, value))
        self.enterContext(patch.dict(os.environ, {"WEBHARNESS_VERIFY_MODE": "off"}))
        self.client = self.enterContext(TestClient(main.app))
        with db.get_db() as conn:
            conn.execute(
                "INSERT INTO users (id, username, password_hash) VALUES (1, 'wilson', ?)",
                (auth.hash_password("wilson-pass"),),
            )
        self.h = {"Authorization": "Bearer " + auth.create_token(1, "wilson")}
        created = self.client.post("/api/rooms", headers=self.h, json={"roomName": "WorldObj", "visibility": "public"})
        self.assertEqual(created.status_code, 200, created.text)
        self.base = "/api/rooms/WorldObj/files"

    def upload(self, name, data, mime="application/octet-stream"):
        r = self.client.post(self.base, headers=self.h, files={"file": (name, data, mime)})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["file"]

    def place(self, file_id, body=None):
        return self.client.put(f"{self.base}/{file_id}/placement", headers=self.h, json=body or POSE)

    def test_classify_extra_model_formats_before_text(self):
        cases = {
            "a.obj": "model/obj", "a.fbx": "model/x-fbx", "a.stl": "model/stl", "a.ply": "model/x-ply",
            "a.3mf": "model/3mf", "a.usdz": "model/vnd.usdz+zip", "a.usda": "model/vnd.usd",
        }
        for name, mime in cases.items():
            self.assertEqual(main._classify_file(name, "text/plain", OBJ_TEXT), ("model", mime), name)
        self.assertEqual(main._classify_file("live.m3u8", None, M3U8_TEXT), ("video", "application/vnd.apple.mpegurl"))
        self.assertEqual(main._reclassify_file("text", "text/plain", "mesh.obj"), ("model", "model/obj"))
        # 既有分类不变
        self.assertEqual(main._classify_file("notes.json", None, b"{}")[0], "text")
        self.assertEqual(main._classify_file("pic.png", None, PNG_1PX), ("image", "image/png"))

    def test_image_video_audio_placeable_text_not(self):
        img = self.upload("pic.png", PNG_1PX, "image/png")
        r = self.place(img["id"])
        self.assertEqual(r.status_code, 200, r.text)
        world = r.json()["file"]["world"]
        self.assertTrue(world["visible"])
        self.assertEqual(world["pose"]["position"], [0.0, 0.5, -1.0])

        mesh = self.upload("mesh.obj", OBJ_TEXT, "text/plain")
        self.assertEqual(mesh["kind"], "model")
        self.assertEqual(self.place(mesh["id"]).status_code, 200)

        hls = self.upload("live.m3u8", M3U8_TEXT, "application/vnd.apple.mpegurl")
        self.assertEqual(hls["kind"], "video")
        self.assertEqual(self.place(hls["id"]).status_code, 200)

        txt = self.client.post(self.base, headers=self.h, json={"name": "a.md", "content": "# hi"}).json()["file"]
        self.assertEqual(self.place(txt["id"]).status_code, 400)

        # visible:false 保留位姿
        hidden = self.place(img["id"], {"visible": False}).json()["file"]["world"]
        self.assertFalse(hidden["visible"])
        self.assertEqual(hidden["pose"]["position"], [0.0, 0.5, -1.0])

    def test_limits_are_per_group(self):
        media = [self.upload(f"v{i}.mp4", b"\x00\x00\x00\x18ftypmp42" + bytes(16), "video/mp4") for i in range(4)]
        for f in media[:3]:
            self.assertEqual(self.place(f["id"]).status_code, 200)
        over = self.place(media[3]["id"])
        self.assertEqual(over.status_code, 400)
        self.assertIn("媒体播放器", over.json()["detail"])
        # 媒体满了不影响图片与模型
        img = self.upload("pic.png", PNG_1PX, "image/png")
        self.assertEqual(self.place(img["id"]).status_code, 200)
        mesh = self.upload("mesh.stl", b"solid x\nendsolid x\n")
        self.assertEqual(self.place(mesh["id"]).status_code, 200)
        # 收起一个媒体后可以再摆
        self.assertEqual(self.place(media[0]["id"], {"visible": False}).status_code, 200)
        self.assertEqual(self.place(media[3]["id"]).status_code, 200)

    def test_model_limit_is_20(self):
        models = [self.upload(f"m{i}.obj", OBJ_TEXT, "text/plain") for i in range(21)]
        for f in models[:20]:
            self.assertEqual(self.place(f["id"]).status_code, 200, f["name"])
        over = self.place(models[20]["id"])
        self.assertEqual(over.status_code, 400)
        self.assertIn("3D 模型", over.json()["detail"])
        self.assertIn("20", over.json()["detail"])

    def test_avatar_scene_only_accept_gltf_family(self):
        with db.get_db() as conn:
            conn.execute(
                "INSERT INTO library_resources (id, owner_account_id, economic_owner_id, name, status) "
                "VALUES (50, 1, 1, 'mesh.fbx', 'active')"
            )
            conn.execute(
                "INSERT INTO library_resource_versions (id, resource_id, version_no, name, kind, mime, size, "
                "content_path, sha256, review_status, scan_status, created_by) "
                "VALUES (60, 50, 1, 'mesh.fbx', 'model', 'model/x-fbx', 1, 'x', 'x', 'approved', 'clean', 1)"
            )
            conn.execute("UPDATE library_resources SET current_version_id = 60 WHERE id = 50")
            with self.assertRaises(main.HTTPException) as ctx:
                main._require_approved_library_model(conn, 50, {"id": 1, "kind": "human"}, purpose="avatar")
        self.assertEqual(ctx.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()
