"""论坛（v2.33）：问答区 / 精华区、采纳答案、管理员策划。仅使用临时 SQLite，不碰真实数据。"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import auth, db, main


class ForumApiTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for module, name, value in (
            (db, "DATA_DIR", root), (db, "DB_PATH", root / "test.db"),
            (db, "UPLOADS_DIR", root / "uploads"), (db, "FILES_DIR", root / "files"),
            (main, "UPLOADS_DIR", root / "uploads"), (main, "FILES_DIR", root / "files"),
            (auth, "SECRET", b"isolated-test-secret"),
        ):
            self.enterContext(patch.object(module, name, value))
        self.enterContext(patch.dict(os.environ, {"WEBHARNESS_VERIFY_MODE": "off"}))
        self.client = self.enterContext(TestClient(main.app))
        with db.get_db() as conn:
            conn.execute(
                "INSERT INTO users (id, username, password_hash, is_superadmin) VALUES (1, 'wilson', ?, 1)",
                (auth.hash_password("testpass"),),
            )
            # alice 既当普通用户也当她名下 agent 的主人
            conn.execute("INSERT INTO users (id, username, kind, owner_id) VALUES (3, 'wilson-bot', 'agent', 1)")
            conn.execute("INSERT INTO users (id, username, kind, owner_id) VALUES (2, 'alice', 'human', NULL)")
        self.admin = self.headers(1, "wilson", "human")
        self.human = self.headers(2, "alice", "human")
        self.agent = self.headers(3, "wilson-bot", "agent")

    @staticmethod
    def headers(user_id, username, kind):
        return {"Authorization": "Bearer " + auth.create_token(user_id, username, kind)}

    def new_topic(self, headers=None, **body):
        response = self.client.post(
            "/api/forum/topics", headers=headers or self.human,
            json={"title": "如何看日志", "body": "在哪里看服务器日志？", "tags": ["运维"], **body},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_topic_lifecycle_and_listing(self):
        admin_topic = self.new_topic(self.admin, title="部署经验汇总", body="历次部署的经验",
                                     board="showcase", curatorNote="沉淀自多起部署事故", tags=["经验", "部署"])
        qna = self.new_topic(self.agent, title="watch.py 不唤醒？")
        self.assertEqual((admin_topic["board"], admin_topic["featuredBy"], admin_topic["curatorNote"]),
                         ("showcase", "wilson", "沉淀自多起部署事故"))
        self.assertTrue(admin_topic["featuredAt"])
        self.assertEqual((qna["board"], qna["author"], qna["authorKind"], qna["tags"]), ("qna", "wilson-bot", "agent", ["运维"]))
        listing = self.client.get("/api/forum/topics", headers=self.human).json()
        self.assertEqual(listing["total"], 2)
        self.assertEqual(len(listing["topics"]), 2)
        self.assertEqual(self.client.get("/api/forum/topics?board=showcase", headers=self.human).json()["total"], 1)
        self.assertEqual(self.client.get("/api/forum/topics?tag=运维", headers=self.human).json()["total"], 1)
        self.assertEqual(self.client.get("/api/forum/topics?author=wilson-bot", headers=self.human).json()["total"], 1)
        self.assertEqual(self.client.get("/api/forum/topics?q=日志", headers=self.human).json()["total"], 1)
        detail = self.client.get(f"/api/forum/topics/{qna['id']}", headers=self.human).json()
        self.assertEqual((detail["replyCount"], detail["status"], detail["acceptedPostId"]), (0, "open", None))
        # canManage 只看本人/超管：alice 不是作者，管理员是
        self.assertFalse(detail["canManage"])
        self.assertTrue(self.client.get(f"/api/forum/topics/{qna['id']}", headers=self.admin).json()["canManage"])

    def test_auth_and_authorization_boundaries(self):
        topic = self.new_topic(self.human)
        self.assertEqual(self.client.get("/api/forum/topics").status_code, 401)
        self.assertEqual(self.client.post("/api/forum/topics", json={"title": "t", "body": "b"}).status_code, 401)
        for body in ({"board": "showcase"}, {"curatorNote": "好贴"}, {"suggestionId": 1}):
            response = self.client.post("/api/forum/topics", headers=self.human,
                                        json={"title": "t", "body": "b", **body})
            self.assertEqual(response.status_code, 403, response.text)
        # 非作者不能改/删别人主题
        for method, path, body in (
            ("PATCH", f"/api/forum/topics/{topic['id']}", {"title": "x"}),
            ("DELETE", f"/api/forum/topics/{topic['id']}", None),
            ("POST", f"/api/forum/topics/{topic['id']}/accept", {"postId": 1}),
        ):
            response = self.client.request(method, path, headers=self.agent, json=body)
            self.assertEqual(response.status_code, 403, response.text)
        # 非管理员不能锁定、不能写策划字段
        response = self.client.patch(f"/api/forum/topics/{topic['id']}", headers=self.human,
                                     json={"status": "locked"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.patch(f"/api/forum/topics/{topic['id']}", headers=self.human,
                                           json={"curatorNote": "n"}).status_code, 403)

    def test_replies_accept_and_tombstone(self):
        topic = self.new_topic(self.human)
        own = self.client.post(f"/api/forum/topics/{topic['id']}/posts", headers=self.human,
                               json={"body": "补充：我试过重置"}).json()
        reply = self.client.post(f"/api/forum/topics/{topic['id']}/posts", headers=self.agent,
                                 json={"body": "看 ~/.webharness/logs", "parentId": own["id"]}).json()
        self.assertEqual(reply["parentId"], own["id"])
        detail = self.client.get(f"/api/forum/topics/{topic['id']}", headers=self.admin).json()
        self.assertEqual((detail["replyCount"], [p["body"] for p in detail["posts"]]),
                         (2, ["补充：我试过重置", "看 ~/.webharness/logs"]))
        # parentId 必须指向本主题未删除的楼层
        other = self.new_topic(self.human, title="另一个问题")
        self.assertEqual(self.client.post(f"/api/forum/topics/{topic['id']}/posts", headers=self.agent,
                                          json={"body": "x", "parentId": 999}).status_code, 404)
        # 采纳：提问者不能采纳自己的楼层；采纳他人回答后状态 resolved
        self.assertEqual(self.client.post(f"/api/forum/topics/{topic['id']}/accept", headers=self.human,
                                          json={"postId": own["id"]}).status_code, 404)
        accepted = self.client.post(f"/api/forum/topics/{topic['id']}/accept", headers=self.human,
                                    json={"postId": reply["id"]}).json()
        self.assertEqual((accepted["status"], accepted["acceptedPostId"]), ("resolved", reply["id"]))
        # 删除被采纳的楼层：墓碑保留、计数 -1、采纳清除
        self.assertEqual(self.client.delete(f"/api/forum/posts/{reply['id']}", headers=self.agent).json()["ok"], True)
        detail = self.client.get(f"/api/forum/topics/{topic['id']}", headers=self.human).json()
        self.assertEqual(detail["replyCount"], 1)
        self.assertIsNone(detail["acceptedPostId"])
        self.assertEqual(detail["posts"][1]["deleted"], True)
        self.assertEqual(detail["posts"][1]["body"], "")
        self.assertEqual(self.client.delete(f"/api/forum/posts/{reply['id']}", headers=self.agent).status_code, 404)
        # 取消采纳后回到 open（先采纳另一条验证）
        other_reply = self.client.post(f"/api/forum/topics/{topic['id']}/posts", headers=self.agent,
                                       json={"body": "或者看 systemd"}).json()
        self.client.post(f"/api/forum/topics/{topic['id']}/accept", headers=self.human,
                         json={"postId": other_reply["id"]})
        reopened = self.client.delete(f"/api/forum/topics/{topic['id']}/accept", headers=self.human).json()
        self.assertEqual((reopened["status"], reopened["acceptedPostId"]), ("open", None))

    def test_edit_lock_and_showcase_move(self):
        topic = self.new_topic(self.human)
        reply = self.client.post(f"/api/forum/topics/{topic['id']}/posts", headers=self.agent,
                                 json={"body": "v1 的回答"}).json()
        # 作者编辑主题与楼层
        updated = self.client.patch(f"/api/forum/topics/{topic['id']}", headers=self.human,
                                    json={"title": "（已解决前的）新标题", "tags": ["运维", "faq", "FAQ"]}).json()
        self.assertEqual(updated["title"], "（已解决前的）新标题")
        self.assertEqual(updated["tags"], ["运维", "faq", "FAQ"])
        self.assertIsNotNone(updated["editedAt"])
        self.client.patch(f"/api/forum/posts/{reply['id']}", headers=self.agent, json={"body": "v2 的回答"})
        posts = self.client.get(f"/api/forum/topics/{topic['id']}", headers=self.human).json()["posts"]
        self.assertEqual((posts[0]["body"], bool(posts[0]["editedAt"])), ("v2 的回答", True))
        # 管理员策划进精华区后：作者不可再编辑主题；管理员可
        featured = self.client.patch(f"/api/forum/topics/{topic['id']}", headers=self.admin,
                                     json={"board": "showcase", "curatorNote": "常见问题标准答案"}).json()
        self.assertEqual((featured["board"], featured["featuredBy"], featured["curatorNote"]),
                         ("showcase", "wilson", "常见问题标准答案"))
        self.assertEqual(self.client.patch(f"/api/forum/topics/{topic['id']}", headers=self.human,
                                           json={"title": "改回去"}).status_code, 403)
        # 精华区仍可被非作者回复（讨论在精华帖下继续），锁定后任何回复都被拒
        self.assertEqual(self.client.post(f"/api/forum/topics/{topic['id']}/posts", headers=self.agent,
                                          json={"body": "补充提问"}).status_code, 200)
        self.client.patch(f"/api/forum/topics/{topic['id']}", headers=self.admin, json={"status": "locked"})
        self.assertEqual(self.client.post(f"/api/forum/topics/{topic['id']}/posts", headers=self.admin,
                                          json={"body": "锁定后回复"}).status_code, 409)
        self.assertEqual(self.client.patch(f"/api/forum/posts/{reply['id']}", headers=self.agent,
                                           json={"body": "锁定后编辑"}).status_code, 409)
        # 管理员解锁并退回问答区：策划信息清空、作者恢复编辑权
        back = self.client.patch(f"/api/forum/topics/{topic['id']}", headers=self.admin,
                                 json={"status": "open", "board": "qna"}).json()
        self.assertEqual((back["status"], back["board"], back["featuredBy"], back["curatorNote"], back["featuredAt"]),
                         ("open", "qna", None, "", None))
        self.assertEqual(self.client.patch(f"/api/forum/topics/{topic['id']}", headers=self.human,
                                           json={"title": "恢复原题"}).status_code, 200)

    def test_topic_from_suggestion_and_delete(self):
        suggestion = self.client.post("/api/suggestions", headers=self.human,
                                      json={"category": "skill", "content": "建议把部署经验整理出来"}).json()
        self.assertEqual(self.client.post("/api/forum/topics", headers=self.admin,
                                          json={"title": "t", "body": "b", "suggestionId": 999}).status_code, 404)
        topic = self.client.post("/api/forum/topics", headers=self.admin,
                                 json={"board": "showcase", "title": "部署经验", "body": "沉淀内容",
                                       "suggestionId": suggestion["id"]}).json()
        self.assertEqual(topic["sourceSuggestionId"], suggestion["id"])
        # 作者可删自己的问答帖；删后列表与详情都不可见
        mine = self.new_topic(self.human)
        self.assertEqual(self.client.delete(f"/api/forum/topics/{mine['id']}", headers=self.human).status_code, 200)
        self.assertEqual(self.client.get(f"/api/forum/topics/{mine['id']}", headers=self.admin).status_code, 404)
        self.assertEqual(self.client.get("/api/forum/topics?board=qna", headers=self.human).json()["total"], 0)
        # 精华帖只有管理员能删
        self.assertEqual(self.client.delete(f"/api/forum/topics/{topic['id']}", headers=self.human).status_code, 403)
        self.assertEqual(self.client.delete(f"/api/forum/topics/{topic['id']}", headers=self.agent).status_code, 403)
        self.assertEqual(self.client.delete(f"/api/forum/topics/{topic['id']}", headers=self.admin).status_code, 200)

    def test_pagination_and_validation(self):
        for n in range(5):
            self.new_topic(self.human, title=f"问题 {n}", body=f"内容 {n}")
        first = self.client.get("/api/forum/topics?limit=2", headers=self.human).json()
        self.assertEqual(first["total"], 5)
        self.assertEqual(len(first["topics"]), 2)
        second = self.client.get(
            f"/api/forum/topics?limit=2&beforeActive={first['nextBeforeActive']}&beforeId={first['nextBeforeId']}",
            headers=self.human).json()
        third = self.client.get(
            f"/api/forum/topics?limit=2&beforeActive={second['nextBeforeActive']}&beforeId={second['nextBeforeId']}",
            headers=self.human).json()
        titles = [t["title"] for t in first["topics"] + second["topics"] + third["topics"]]
        self.assertEqual(len(set(titles)), 5)
        self.assertIsNone(third["nextBeforeId"])
        # 翻页游标必须成对
        self.assertEqual(self.client.get(f"/api/forum/topics?beforeId={first['nextBeforeId']}",
                                         headers=self.human).status_code, 422)
        # 字段校验
        for body in ({"title": " ", "body": "b"}, {"title": "t", "body": " " * 1},
                     {"title": "t", "body": "b", "tags": ["x" * 25]},
                     {"title": "t", "body": "b", "tags": [str(i) for i in range(6)]}):
            response = self.client.post("/api/forum/topics", headers=self.human, json=body)
            self.assertEqual(response.status_code, 422, str(body)[:60])
        topic = self.new_topic(self.human)
        self.assertEqual(self.client.post(f"/api/forum/topics/{topic['id']}/posts", headers=self.agent,
                                          json={"body": " "}).status_code, 422)
        self.assertEqual(self.client.get("/api/forum/topics?limit=101", headers=self.human).status_code, 422)

    def test_topic_detail_paging_and_capabilities(self):
        topic = self.new_topic(self.human, title="盖楼")
        for n in range(3):
            self.client.post(f"/api/forum/topics/{topic['id']}/posts", headers=self.agent, json={"body": f"{n}"})
        first = self.client.get(f"/api/forum/topics/{topic['id']}?limit=2", headers=self.human).json()
        self.assertEqual([p["body"] for p in first["posts"]], ["0", "1"])
        rest = self.client.get(f"/api/forum/topics/{topic['id']}?limit=2&afterId={first['nextAfterId']}",
                               headers=self.human).json()
        self.assertEqual([p["body"] for p in rest["posts"]], ["2"])
        self.assertIsNone(rest["nextAfterId"])
        me = self.client.get("/api/me", headers=self.admin).json()
        self.assertIn("forum.manage", me["adminCapabilities"])

    def test_categories(self):
        cats = self.client.get("/api/forum/categories", headers=self.agent).json()["categories"]
        ids = [c["id"] for c in cats]
        self.assertEqual(ids, ["bug", "feature", "room-tips", "files", "agent-setup", "xr", "other"])
        default = self.new_topic(title="默认分类", body="x")
        self.assertEqual(default["category"], "other")
        bug = self.new_topic(headers=self.agent, title="上传 500", body="复现…", category="bug")
        self.assertEqual(bug["category"], "bug")
        listed = self.client.get("/api/forum/topics?category=bug", headers=self.human).json()
        self.assertEqual([t["id"] for t in listed["topics"]], [bug["id"]])
        self.assertEqual(self.client.get("/api/forum/topics?category=nope", headers=self.human).status_code, 422)
        self.assertEqual(self.client.post("/api/forum/topics", headers=self.human,
                                          json={"title": "t", "body": "b", "category": "nope"}).status_code, 422)
        moved = self.client.patch(f"/api/forum/topics/{bug['id']}", headers=self.agent,
                                  json={"category": "files"})
        self.assertEqual(moved.status_code, 200, moved.text)
        self.assertEqual(moved.json()["category"], "files")
        counts = {c["id"]: c["counts"] for c in
                  self.client.get("/api/forum/categories", headers=self.human).json()["categories"]}
        self.assertEqual(counts["files"], {"qna": 1, "showcase": 0})
        self.assertEqual(counts["bug"], {"qna": 0, "showcase": 0})


if __name__ == "__main__":
    unittest.main()
