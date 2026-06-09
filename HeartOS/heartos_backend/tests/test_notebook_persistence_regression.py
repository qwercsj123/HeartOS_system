from __future__ import annotations

import json
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
        if TEST_USERS_FILE.exists():
            TEST_USERS_FILE.unlink()
        TEST_DATA_ROOT.mkdir(parents=True, exist_ok=True)
        TEST_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        for path in (
            main_module.NOTEBOOKS_PATH,
            main_module.NOTEBOOK_SOURCES_PATH,
            main_module.NOTEBOOK_RESULT_FILES_PATH,
            main_module.NOTEBOOK_TOMBSTONES_PATH,
            main_module.FEEDBACK_PATH,
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}", encoding="utf-8")
        for child in TEST_UPLOAD_DIR.iterdir():
            if child.is_file():
                child.unlink()
            elif child.is_dir():
                shutil.rmtree(child)
        main_module.FILE_META_PATH.write_text("{}", encoding="utf-8")

    def _auth_headers(self) -> dict[str, str]:
        resp = self.client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "admin123", "phone": ""},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        token = resp.json()["token"]
        return {"Authorization": "Bearer " + token}

    def _read_json(self, path: Path) -> dict[str, object]:
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def _user_id(self) -> str:
        me = self.client.get("/api/auth/me", headers=self.headers)
        self.assertEqual(me.status_code, 200, me.text)
        return str(me.json()["user_id"])

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
        self.assertEqual(item["analysisFiles"][0]["fileUrl"], "")

        refreshed = self.client.get("/api/notebooks/nb_multi_refresh/result-files", headers=self.headers)
        self.assertEqual(refreshed.status_code, 200, refreshed.text)
        self.assertEqual(len(refreshed.json()["items"]), 2)

        user_id = self._user_id()
        notebooks_db = self._read_json(main_module.NOTEBOOKS_PATH)
        stored_items = notebooks_db.get(user_id, [])
        self.assertTrue(stored_items)
        stored = next(item for item in stored_items if item["id"] == "nb_multi_refresh")
        self.assertEqual(len(stored["sources"]), 2)
        self.assertEqual(len(stored["analysisFiles"]), 2)
        self.assertEqual(stored["sources"][0]["fileUrl"], "/api/files/src_pdf_1.pdf")
        self.assertEqual(stored["sources"][1]["fileUrl"], "/api/files/src_pdf_2.pdf")

        result_files_db = self._read_json(main_module.NOTEBOOK_RESULT_FILES_PATH)
        self.assertEqual(len(result_files_db[user_id]["nb_multi_refresh"]["items"]), 2)

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
        main_module.NOTEBOOKS_PATH.write_text(
            json.dumps(
                {
                    user_id: [
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
                        }
                    ]
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        main_module.NOTEBOOK_SOURCES_PATH.write_text(
            json.dumps(
                {
                    user_id: {
                        notebook_id: {
                            "items": [
                                _source_item("src_pdf_1", "ZS0021801453.pdf"),
                                _source_item("src_pdf_2", "ZS0023703661.pdf"),
                            ],
                            "updatedAt": 4200,
                        }
                    }
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        main_module.NOTEBOOK_RESULT_FILES_PATH.write_text(
            json.dumps(
                {
                    user_id: {
                        notebook_id: {
                            "items": [
                                _result_item("rf_csv_1", "ZS0021801453_2.csv", "src_pdf_1"),
                                _result_item("rf_csv_2", "ZS0023703661_2.csv", "src_pdf_2"),
                            ],
                            "updatedAt": 4300,
                        }
                    }
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        detail = self.client.get("/api/notebooks/" + notebook_id, headers=self.headers)
        self.assertEqual(detail.status_code, 200, detail.text)
        item = detail.json()["item"]
        self.assertEqual(len(item["sources"]), 2)
        self.assertEqual(len(item["analysisFiles"]), 2)
        self.assertGreaterEqual(int(item["analysisFilesUpdatedAt"]), 4300)
        self.assertEqual(item["sources"][0]["fileUrl"], "/api/files/src_pdf_1.pdf")
        self.assertEqual(item["analysisFiles"][0]["fileUrl"], "")

        notebooks_db = self._read_json(main_module.NOTEBOOKS_PATH)
        stored = next(entry for entry in notebooks_db[user_id] if entry["id"] == notebook_id)
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
        main_module.NOTEBOOKS_PATH.write_text(
            json.dumps(
                {
                    user_id: [
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
                        }
                    ]
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        detail = self.client.get("/api/notebooks/" + notebook_id, headers=self.headers)
        self.assertEqual(detail.status_code, 200, detail.text)
        item = detail.json()["item"]
        self.assertEqual(item["sources"][0]["fileUrl"], "/api/files/src_pdf_1.pdf")
        self.assertEqual(item["analysisFiles"][0]["fileUrl"], "/api/files/rf_csv_1.csv")

        notebooks_db = self._read_json(main_module.NOTEBOOKS_PATH)
        stored = next(entry for entry in notebooks_db[user_id] if entry["id"] == notebook_id)
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
                    "_html": huge_html,
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
        notebooks_db = self._read_json(main_module.NOTEBOOKS_PATH)
        stored = next(entry for entry in notebooks_db[user_id] if entry["id"] == notebook_id)
        self.assertNotIn("_html", stored["msgs"][0])


if __name__ == "__main__":
    unittest.main()
