from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
TEST_DATA_ROOT = Path(tempfile.mkdtemp(prefix="heartos-notebook-regression-")).resolve()
TEST_USERS_FILE = (TEST_DATA_ROOT / "users.json").resolve()
TEST_UPLOAD_DIR = (TEST_DATA_ROOT / "uploads").resolve()

os.environ["APP_USERS_FILE"] = str(TEST_USERS_FILE)
os.environ["APP_UPLOAD_DIR"] = str(TEST_UPLOAD_DIR)
os.environ["APP_PUBLIC_BASE_URL"] = "http://127.0.0.1:9010"
os.environ["APP_AUTH_MODE"] = "local"
os.environ["APP_DEFAULT_USERNAME"] = "admin"
os.environ["APP_DEFAULT_PASSWORD"] = "admin123"
os.environ["APP_DEFAULT_ADMIN_PHONE"] = "13800000000"

if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from fastapi.testclient import TestClient

from app import db as dbstore
from app import main as main_module


def _source_item(source_id: str, name: str) -> dict[str, object]:
    return {
        "id": source_id,
        "source_id": source_id,
        "name": name,
        "type": "file",
        "checked": True,
        "mime": "application/pdf",
        "fileId": source_id + ".pdf",
        "serverFileId": source_id + ".pdf",
        "fileUrl": "http://127.0.0.1:9010/api/files/" + source_id + ".pdf",
    }


def _result_item(result_id: str, name: str, source_id: str) -> dict[str, object]:
    return {
        "id": result_id,
        "name": name,
        "type": "csv",
        "mime": "text/csv;charset=utf-8",
        "content": "lead_i,lead_ii\n0.1,0.2\n",
        "source": "AI 自动数字化",
        "sourceName": name.replace(".csv", ".pdf"),
        "sourceId": source_id,
        "parentSourceId": source_id,
        "generatedBy": "auto_digitize",
        "createdAt": "2026-06-08 10:00:00",
    }


class NotebookPersistenceRegressionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(main_module.app)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(TEST_DATA_ROOT, ignore_errors=True)

    def setUp(self) -> None:
        self._reset_storage()
        self.headers = self._auth_headers()

    def _reset_storage(self) -> None:
        TEST_DATA_ROOT.mkdir(parents=True, exist_ok=True)
        TEST_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        dbstore.wipe_all_for_tests()
        for child in TEST_UPLOAD_DIR.iterdir():
            if child.is_file():
                child.unlink()
            elif child.is_dir():
                shutil.rmtree(child)

    def _auth_headers(self) -> dict[str, str]:
        resp = self.client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "admin123", "phone": ""},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        token = resp.json()["token"]
        return {"Authorization": "Bearer " + token}

    def _user_id(self) -> str:
        me = self.client.get("/api/auth/me", headers=self.headers)
        self.assertEqual(me.status_code, 200, me.text)
        return str(me.json()["user_id"])

    def _stored_notebook(self, user_id: str, notebook_id: str) -> dict[str, object]:
        stored = dbstore.notebook_get(user_id, notebook_id)
        self.assertIsNotNone(stored, f"notebook {notebook_id} not found in sqlite")
        return stored

    def _create_full_notebook(self, notebook_id: str, updated_at: int = 1000, analysis_updated_at: int = 1001) -> dict[str, object]:
        payload = {
            "id": notebook_id,
            "title": "批量数字化回归",
            "icon": "📔",
            "color": "#e8f0fe",
            "date": "2026-06-08",
            "sources": [
                _source_item("src_pdf_1", "ZS0021801453.pdf"),
                _source_item("src_pdf_2", "ZS0023703661.pdf"),
            ],
            "msgs": [],
            "events": [],
            "analysisFiles": [
                _result_item("rf_csv_1", "ZS0021801453_2.csv", "src_pdf_1"),
                _result_item("rf_csv_2", "ZS0023703661_2.csv", "src_pdf_2"),
            ],
            "analysisFilesUpdatedAt": analysis_updated_at,
            "sumHtml": "",
            "suggHtml": "",
            "updatedAt": updated_at,
        }
        resp = self.client.post("/api/notebooks", json=payload, headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        return payload

    def test_refresh_keeps_multiple_result_files_with_aggregate_storage(self) -> None:
        self._create_full_notebook("nb_multi_refresh", updated_at=2000, analysis_updated_at=2001)

        stale_resp = self.client.put(
            "/api/notebooks/nb_multi_refresh/result-files",
            json={
                "items": [_result_item("rf_csv_1", "ZS0021801453_2.csv", "src_pdf_1")],
                "updatedAt": 1999,
            },
            headers=self.headers,
        )
        self.assertEqual(stale_resp.status_code, 200, stale_resp.text)
        self.assertTrue(stale_resp.json().get("stale"))

        detail = self.client.get("/api/notebooks/nb_multi_refresh", headers=self.headers)
        self.assertEqual(detail.status_code, 200, detail.text)
        item = detail.json()["item"]
        self.assertEqual(len(item["sources"]), 2)
        self.assertEqual(len(item["analysisFiles"]), 2)
        self.assertEqual(item["sources"][0]["fileUrl"], "/api/files/src_pdf_1.pdf")
        # 分析文件 content 已外部化为 uploads 文件 + fileId 引用
        self.assertTrue(item["analysisFiles"][0]["fileUrl"].startswith("/api/files/"))
        self.assertEqual(item["analysisFiles"][0]["content"], "")

        refreshed = self.client.get("/api/notebooks/nb_multi_refresh/result-files", headers=self.headers)
        self.assertEqual(refreshed.status_code, 200, refreshed.text)
        self.assertEqual(len(refreshed.json()["items"]), 2)

        user_id = self._user_id()
        stored = self._stored_notebook(user_id, "nb_multi_refresh")
        self.assertEqual(len(stored["sources"]), 2)
        self.assertEqual(len(stored["analysisFiles"]), 2)
        self.assertEqual(stored["sources"][0]["fileUrl"], "/api/files/src_pdf_1.pdf")
        self.assertEqual(stored["sources"][1]["fileUrl"], "/api/files/src_pdf_2.pdf")

        entry = dbstore.bucket_entry_get("notebook_result_files", user_id, "nb_multi_refresh")
        self.assertIsNotNone(entry)
        self.assertEqual(len(entry["items"]), 2)

    def test_missing_fields_do_not_drop_existing_sources_or_analysis_files(self) -> None:
        self._create_full_notebook("nb_missing_fields", updated_at=3000, analysis_updated_at=3001)

        partial_save = {
            "id": "nb_missing_fields",
            "title": "批量数字化回归（重命名）",
            "icon": "📔",
            "color": "#e8f0fe",
            "date": "2026-06-08",
            "msgs": [{"role": "assistant", "content": "partial update"}],
            "events": [],
            "sumHtml": "",
            "suggHtml": "",
            "updatedAt": 3002,
        }
        resp = self.client.post("/api/notebooks", json=partial_save, headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)

        detail = self.client.get("/api/notebooks/nb_missing_fields", headers=self.headers)
        self.assertEqual(detail.status_code, 200, detail.text)
        item = detail.json()["item"]
        self.assertEqual(item["title"], "批量数字化回归（重命名）")
        self.assertEqual(len(item["sources"]), 2)
        self.assertEqual(len(item["analysisFiles"]), 2)

    def test_legacy_split_storage_migrates_into_aggregate_notebook(self) -> None:
        user_id = self._user_id()
        notebook_id = "nb_legacy_migration"
        dbstore.notebook_upsert(
            user_id,
            {
                "id": notebook_id,
                "title": "旧数据 notebook",
                "icon": "📔",
                "color": "#e8f0fe",
                "date": "2026-06-08",
                "sources": [],
                "msgs": [],
                "events": [],
                "analysisFiles": [],
                "analysisFilesUpdatedAt": 0,
                "sumHtml": "",
                "suggHtml": "",
                "updatedAt": 4100,
            },
        )
        dbstore.bucket_entry_set(
            "notebook_sources",
            user_id,
            notebook_id,
            [
                _source_item("src_pdf_1", "ZS0021801453.pdf"),
                _source_item("src_pdf_2", "ZS0023703661.pdf"),
            ],
            4200,
        )
        dbstore.bucket_entry_set(
            "notebook_result_files",
            user_id,
            notebook_id,
            [
                _result_item("rf_csv_1", "ZS0021801453_2.csv", "src_pdf_1"),
                _result_item("rf_csv_2", "ZS0023703661_2.csv", "src_pdf_2"),
            ],
            4300,
        )

        detail = self.client.get("/api/notebooks/" + notebook_id, headers=self.headers)
        self.assertEqual(detail.status_code, 200, detail.text)
        item = detail.json()["item"]
        self.assertEqual(len(item["sources"]), 2)
        self.assertEqual(len(item["analysisFiles"]), 2)
        self.assertGreaterEqual(int(item["analysisFilesUpdatedAt"]), 4300)
        self.assertEqual(item["sources"][0]["fileUrl"], "/api/files/src_pdf_1.pdf")

        stored = self._stored_notebook(user_id, notebook_id)
        self.assertEqual(len(stored["sources"]), 2)
        self.assertEqual(len(stored["analysisFiles"]), 2)
        self.assertEqual(stored["sources"][0]["fileUrl"], "/api/files/src_pdf_1.pdf")

    def test_absolute_file_urls_are_migrated_to_relative_api_paths(self) -> None:
        user_id = self._user_id()
        notebook_id = "nb_absolute_file_url_migration"
        source = _source_item("src_pdf_1", "ZS0021801453.pdf")
        result = _result_item("rf_csv_1", "ZS0021801453_2.csv", "src_pdf_1")
        result["fileId"] = "rf_csv_1.csv"
        result["serverFileId"] = "rf_csv_1.csv"
        result["fileUrl"] = "http://127.0.0.1:9010/api/files/rf_csv_1.csv"
        dbstore.notebook_upsert(
            user_id,
            {
                "id": notebook_id,
                "title": "绝对地址迁移",
                "icon": "📔",
                "color": "#e8f0fe",
                "date": "2026-06-08",
                "sources": [source],
                "msgs": [],
                "events": [],
                "analysisFiles": [result],
                "analysisFilesUpdatedAt": 6100,
                "sumHtml": "",
                "suggHtml": "",
                "updatedAt": 6101,
            },
        )

        detail = self.client.get("/api/notebooks/" + notebook_id, headers=self.headers)
        self.assertEqual(detail.status_code, 200, detail.text)
        item = detail.json()["item"]
        self.assertEqual(item["sources"][0]["fileUrl"], "/api/files/src_pdf_1.pdf")
        self.assertEqual(item["analysisFiles"][0]["fileUrl"], "/api/files/rf_csv_1.csv")

        stored = self._stored_notebook(user_id, notebook_id)
        self.assertEqual(stored["sources"][0]["fileUrl"], "/api/files/src_pdf_1.pdf")
        self.assertEqual(stored["analysisFiles"][0]["fileUrl"], "/api/files/rf_csv_1.csv")

    def test_huge_rich_message_html_is_trimmed_from_persistence(self) -> None:
        notebook_id = "nb_huge_rich_msg"
        huge_html = "<div>" + ("A" * 25000) + "</div>"
        payload = {
            "id": notebook_id,
            "title": "Huge Rich Message",
            "icon": "📔",
            "color": "#e8f0fe",
            "date": "2026-06-08",
            "sources": [
                _source_item("src_pdf_1", "ZS0021801453.pdf"),
            ],
            "msgs": [
                {
                    "role": "assistant",
                    "kind": "chat",
                    "type": "auto_digitize_preview",
                    "content": "自动数字化预览已生成",
                    "_html": "<div>" + ("A" * 25000) + "<img src='data:image/png;base64,xx'/></div>",
                    "createdAt": 5000,
                }
            ],
            "events": [],
            "analysisFiles": [
                _result_item("rf_csv_1", "ZS0021801453.csv", "src_pdf_1"),
            ],
            "analysisFilesUpdatedAt": 5001,
            "sumHtml": "",
            "suggHtml": "",
            "updatedAt": 5002,
        }
        resp = self.client.post("/api/notebooks", json=payload, headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)

        detail = self.client.get("/api/notebooks/" + notebook_id, headers=self.headers)
        self.assertEqual(detail.status_code, 200, detail.text)
        item = detail.json()["item"]
        self.assertEqual(len(item["msgs"]), 1)
        self.assertNotIn("_html", item["msgs"][0])
        self.assertEqual(item["msgs"][0]["content"], "自动数字化预览已生成")

        user_id = self._user_id()
        stored = self._stored_notebook(user_id, notebook_id)
        self.assertNotIn("_html", stored["msgs"][0])

    def test_inline_assets_are_externalized_to_uploads(self) -> None:
        """内联 base64 图片 / 大文本 / 分析内容在保存时应剥离为 uploads 文件 + fileId 引用，
        且通过 /api/files/{id} 能取回原始内容（前端 hydrate 链路依赖此契约）。"""
        import base64 as b64

        png_bytes = b64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
        )
        image_data_url = "data:image/png;base64," + b64.b64encode(png_bytes).decode()
        big_text = "lead_i,lead_ii\n" + ("0.123,0.456\n" * 4000)  # > 32KB
        analysis_text = "a,b\n1,2\n"
        payload = {
            "id": "nb_externalize",
            "title": "外部化回归",
            "icon": "📔",
            "color": "#e8f0fe",
            "date": "2026-06-11",
            "sources": [
                {
                    "id": "src_img_1",
                    "source_id": "src_img_1",
                    "name": "ecg.png",
                    "type": "png",
                    "checked": True,
                    "imageDataUrl": image_data_url,
                },
                {
                    "id": "src_csv_1",
                    "source_id": "src_csv_1",
                    "name": "waveform.csv",
                    "type": "csv",
                    "checked": True,
                    "content": big_text,
                },
            ],
            "msgs": [
                {
                    "id": "m1",
                    "role": "assistant",
                    "kind": "chat",
                    "content": "预览",
                    "previewMeta": {
                        "kind": "preview",
                        "title": "四图",
                        "images": [{"id": "img_0", "label": "I", "dataUrl": image_data_url}],
                    },
                    "createdAt": 7000,
                }
            ],
            "events": [],
            "analysisFiles": [
                {"id": "rf_1", "name": "result.csv", "type": "csv", "mime": "text/csv;charset=utf-8", "content": analysis_text}
            ],
            "analysisFilesUpdatedAt": 7001,
            "sumHtml": "",
            "suggHtml": "",
            "updatedAt": 7002,
        }
        resp = self.client.post("/api/notebooks", json=payload, headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)

        user_id = self._user_id()
        stored = self._stored_notebook(user_id, "nb_externalize")

        img_source = stored["sources"][0]
        self.assertNotIn("imageDataUrl", img_source)
        self.assertTrue(img_source["fileId"])
        fetched = self.client.get("/api/files/" + img_source["fileId"], headers=self.headers)
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.content, png_bytes)

        csv_source = stored["sources"][1]
        self.assertEqual(csv_source["content"], "")
        self.assertTrue(csv_source["fileId"])
        fetched = self.client.get("/api/files/" + csv_source["fileId"], headers=self.headers)
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.text, big_text)

        analysis = stored["analysisFiles"][0]
        self.assertEqual(analysis["content"], "")
        self.assertTrue(analysis["fileId"])
        fetched = self.client.get("/api/files/" + analysis["fileId"], headers=self.headers)
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.text, analysis_text)

        preview_image = stored["msgs"][0]["previewMeta"]["images"][0]
        self.assertNotIn("dataUrl", preview_image)
        self.assertTrue(preview_image["fileId"])
        fetched = self.client.get("/api/files/" + preview_image["fileId"], headers=self.headers)
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.content, png_bytes)

        # 再保存一次同样内容：hash 去重，不应产生重复文件（聊天图除外，uuid 命名）
        ids_before = {img_source["fileId"], csv_source["fileId"], analysis["fileId"]}
        payload["updatedAt"] = 7003
        resp = self.client.post("/api/notebooks", json=payload, headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        stored2 = self._stored_notebook(user_id, "nb_externalize")
        ids_after = {stored2["sources"][0]["fileId"], stored2["sources"][1]["fileId"], stored2["analysisFiles"][0]["fileId"]}
        self.assertEqual(ids_before, ids_after)

    def test_legacy_inline_assets_sweep(self) -> None:
        """启动清理：旧库里已有的内联资产被一次性外部化。"""
        user_id = self._user_id()
        big_text = "x,y\n" + ("1.0,2.0\n" * 5000)
        dbstore.notebook_upsert(
            user_id,
            {
                "id": "nb_legacy_inline",
                "title": "旧内联数据",
                "sources": [
                    {"id": "s1", "source_id": "s1", "name": "old.csv", "type": "csv", "checked": True, "content": big_text}
                ],
                "msgs": [],
                "events": [],
                "analysisFiles": [
                    {"id": "rf1", "name": "old_result.csv", "type": "csv", "content": "m,n\n3,4\n"}
                ],
                "analysisFilesUpdatedAt": 8000,
                "updatedAt": 8001,
            },
        )
        changed = main_module._externalize_all_legacy_inline_assets()
        self.assertGreaterEqual(changed, 1)
        stored = self._stored_notebook(user_id, "nb_legacy_inline")
        self.assertEqual(stored["sources"][0]["content"], "")
        self.assertTrue(stored["sources"][0]["fileId"])
        self.assertEqual(stored["analysisFiles"][0]["content"], "")
        # 幂等：标记已写，第二次不再扫
        self.assertEqual(main_module._externalize_all_legacy_inline_assets(), 0)

    def _register_user(self, phone: str, password: str = "abc12345") -> tuple[str, dict[str, str]]:
        send = self.client.post("/api/auth/send-code", json={"phone": phone, "purpose": "register"})
        self.assertEqual(send.status_code, 200, send.text)
        register = self.client.post(
            "/api/auth/register",
            json={
                "phone": phone,
                "password": password,
                "code": send.json()["debug_code"],
                "name": "用户" + phone[-4:],
                "organization": "测试医院",
                "user_type": "doctor",
                "use_case": "test",
            },
        )
        self.assertEqual(register.status_code, 200, register.text)
        out = register.json()
        return str(out["user_id"]), {"Authorization": "Bearer " + out["token"]}

    def test_admin_toggle_rules(self) -> None:
        """管理员勾选规则：可互相授予/取消；超级管理员不可取消；不能改自己。"""
        user_id, user_headers = self._register_user("13700001111")

        # 普通用户无权访问管理接口
        resp = self.client.post(f"/api/admin/users/{user_id}/admin", json={"is_admin": True}, headers=user_headers)
        self.assertEqual(resp.status_code, 403)

        # 超级管理员授予其管理员身份
        resp = self.client.post(f"/api/admin/users/{user_id}/admin", json={"is_admin": True}, headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(dbstore.user_get(user_id=user_id)["is_admin"])

        # 新管理员立即可访问管理接口（is_admin 实时读库）
        resp = self.client.get("/api/admin/users", headers=user_headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        items = resp.json()["items"]
        super_item = next(i for i in items if i["user_id"] == "u_admin")
        self.assertTrue(super_item["is_super_admin"])

        # 不能修改自己的身份
        resp = self.client.post(f"/api/admin/users/{user_id}/admin", json={"is_admin": False}, headers=user_headers)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("自己", resp.json()["detail"])

        # 超级管理员身份不可取消
        resp = self.client.post("/api/admin/users/u_admin/admin", json={"is_admin": False}, headers=user_headers)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("超级管理员", resp.json()["detail"])

        # 超级管理员可以取消其他管理员
        resp = self.client.post(f"/api/admin/users/{user_id}/admin", json={"is_admin": False}, headers=self.headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertFalse(dbstore.user_get(user_id=user_id)["is_admin"])

        # 被取消后立即失去管理接口访问权
        resp = self.client.get("/api/admin/users", headers=user_headers)
        self.assertEqual(resp.status_code, 403)

    def test_user_persists_in_sqlite_after_register(self) -> None:
        send = self.client.post(
            "/api/auth/send-code",
            json={"phone": "13900001111", "purpose": "register"},
        )
        self.assertEqual(send.status_code, 200, send.text)
        code = send.json().get("debug_code")
        self.assertTrue(code)

        register = self.client.post(
            "/api/auth/register",
            json={
                "phone": "13900001111",
                "password": "abc12345",
                "code": code,
                "name": "回归测试用户",
                "organization": "回归测试医院",
                "user_type": "doctor",
                "use_case": "regression",
            },
        )
        self.assertEqual(register.status_code, 200, register.text)
        user_id = register.json()["user_id"]

        stored = dbstore.user_get(user_id=user_id)
        self.assertIsNotNone(stored)
        self.assertEqual(stored["phone"], "13900001111")
        self.assertEqual(stored["organization"], "回归测试医院")

        login = self.client.post(
            "/api/auth/login",
            json={"username": "13900001111", "password": "abc12345", "phone": ""},
        )
        self.assertEqual(login.status_code, 200, login.text)


if __name__ == "__main__":
    unittest.main()
