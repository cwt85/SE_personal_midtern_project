"""以同一個 Cookie jar 模擬瀏覽器的多個分頁，驗證角色不互相覆蓋。"""

from datetime import datetime
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from auth import SESSION_TTL_SECONDS, create_session_token, hash_password
from db import get_db
from main import SESSION_COOKIES, WORKSPACES, app


def cursor(rows):
    """只替換資料庫回應，HTTP 路由、表單與 Cookie 驗證皆執行真實程式。"""
    result = MagicMock()
    result.fetchone = AsyncMock(return_value=rows[0] if rows else None)
    result.fetchall = AsyncMock(return_value=rows)
    return result


class SessionIsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.accounts = {
            role: {
                "id": index,
                "role": role,
                "display_name": f"測試{role}",
                "password_hash": hash_password(f"{role}1234"),
            }
            for index, role in enumerate(SESSION_COOKIES, start=1)
        }

    def setUp(self):
        self.db = MagicMock()
        self.db.execute = AsyncMock()
        self.db.transaction.return_value = AsyncMock()

        async def fake_db():
            yield self.db

        # 不啟動 PostgreSQL lifespan，避免測試碰觸使用者的實際資料。
        app.dependency_overrides[get_db] = fake_db
        self.addCleanup(app.dependency_overrides.clear)
        self.client = TestClient(app, follow_redirects=False)
        self.addCleanup(self.client.close)

    def reply_with(self, *row_sets):
        self.db.execute.side_effect = [cursor(rows) for rows in row_sets]

    def login(self, role):
        self.reply_with([self.accounts[role]])
        response = self.client.post(
            "/login", data={"email": f"{role}@demo.local", "password": f"{role}1234"}
        )
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], WORKSPACES[role])
        self.assertIn(SESSION_COOKIES[role], self.client.cookies)
        session_cookie = response.headers.get_list("set-cookie")[0]
        self.assertIn("HttpOnly", session_cookie)
        self.assertIn("SameSite=lax", session_cookie)

    def workspace(self, role):
        self.reply_with([], [])
        response = self.client.get(WORKSPACES[role])
        self.assertEqual(response.status_code, 200)
        self.assertIn(f"測試{role}", response.text)
        other_role = "editor" if role == "author" else "author"
        self.assertNotIn(f"測試{other_role}", response.text)
        self.assertIn(f'name="role" value="{role}"', response.text)
        self.assertIn("no-store", response.headers["cache-control"])
        return response

    def test_both_login_orders_keep_each_workspace_identity(self):
        for order in [("author", "editor"), ("editor", "author")]:
            with self.subTest(order=order):
                self.client.cookies.clear()
                self.login(order[0])
                first_token = self.client.cookies[SESSION_COOKIES[order[0]]]
                self.login(order[1])
                self.assertEqual(self.client.cookies[SESSION_COOKIES[order[0]]], first_token)
                self.workspace("author")
                self.workspace("editor")

    def test_submit_refresh_review_and_refresh_author(self):
        self.login("editor")
        self.login("author")
        self.reply_with([{"id": 1, "name": "測試分類"}], [{"id": 10}])
        submitted = self.client.post("/author/submissions/new", data={
            "title": "跨角色測試稿件", "content": "測試內容", "category_id": 1, "action": "submit",
        })
        self.assertEqual(submitted.status_code, 303)
        insert_params = self.db.execute.call_args.args[1]
        self.assertEqual(insert_params[0], self.accounts["author"]["id"])
        self.assertEqual(insert_params[4], "in_review")

        submission = {
            "id": 10, "title": "跨角色測試稿件", "content": "測試內容", "status": "in_review",
            "category_name": "測試分類", "author_name": "測試author", "revision_note": None,
            "submitted_at": datetime.now(), "updated_at": datetime.now(),
        }
        # 追蹤送審後的跳轉，確認作者與編輯重新整理皆可正常顯示。
        self.reply_with([submission])
        redirect = self.client.get(submitted.headers["location"])
        self.assertEqual(redirect.status_code, 303)
        self.reply_with([submission], [{"status": "in_review", "total": 1}])
        author_page = self.client.get(redirect.headers["location"])
        self.assertEqual(author_page.status_code, 200)
        self.assertIn("跨角色測試稿件", author_page.text)
        self.reply_with([submission], [{"status": "in_review", "total": 1}])
        editor_page = self.client.get("/editor/reviews")
        self.assertEqual(editor_page.status_code, 200)
        self.assertIn("跨角色測試稿件", editor_page.text)

        self.reply_with([submission], [{"id": 10}], [])
        reviewed = self.client.post("/editor/reviews/10", data={"action": "approve"})
        self.assertEqual(reviewed.status_code, 303)
        review_params = self.db.execute.call_args.args[1]
        self.assertEqual(review_params[:3], (10, self.accounts["editor"]["id"], "approved"))
        submission["status"] = "approved"
        self.reply_with([submission], [{"status": "approved", "total": 1}])
        refreshed = self.client.get("/author/submissions")
        self.assertEqual(refreshed.status_code, 200)
        self.assertIn('class="status approved"', refreshed.text)

    def test_logout_only_removes_selected_role(self):
        for role, other in [("author", "editor"), ("editor", "author")]:
            with self.subTest(role=role):
                self.login(role)
                self.login(other)
                token = self.client.cookies[SESSION_COOKIES[other]]
                response = self.client.post("/logout", data={"role": role})
                self.assertEqual(response.status_code, 303)
                self.assertNotIn(SESSION_COOKIES[role], self.client.cookies)
                self.assertEqual(self.client.cookies[SESSION_COOKIES[other]], token)
                self.assertEqual(self.client.get(WORKSPACES[role]).status_code, 303)
                self.workspace(other)

    def test_invalid_logout_does_not_clear_any_session(self):
        self.login("author")
        self.login("editor")
        for data, expected_status in [({}, 422), ({"role": "admin"}, 400)]:
            response = self.client.post("/logout", data=data)
            self.assertEqual(response.status_code, expected_status)
            self.assertNotIn("set-cookie", response.headers)
        self.workspace("author")
        self.workspace("editor")

    def test_login_page_and_home_allow_both_workspaces(self):
        self.assertEqual(self.client.get("/").headers["location"], "/login")
        self.login("author")
        self.assertEqual(self.client.get("/").headers["location"], WORKSPACES["author"])
        self.assertEqual(self.client.get("/login").status_code, 200)
        self.login("editor")
        self.assertEqual(self.client.get("/").headers["location"], "/login")
        page = self.client.get("/login")
        for role in SESSION_COOKIES:
            self.assertIn(WORKSPACES[role], page.text)
            self.assertIn(f"測試{role}", page.text)

    def test_failed_login_keeps_existing_sessions(self):
        self.login("author")
        self.login("editor")
        for rows in [[], [self.accounts["author"]]]:
            self.reply_with(rows)
            response = self.client.post("/login", data={"email": "wrong@example.com", "password": "wrong"})
            self.assertEqual(response.status_code, 401)
            self.assertNotIn("set-cookie", response.headers)
        self.workspace("author")
        self.workspace("editor")

    def test_other_role_cannot_authorize_get_or_post(self):
        for role, other in [("author", "editor"), ("editor", "author")]:
            with self.subTest(role=role):
                self.client.cookies.clear()
                self.login(role)
                self.db.execute.reset_mock()
                response = self.client.get(WORKSPACES[other])
                self.assertEqual(response.status_code, 303)
                self.assertEqual(response.headers["location"], "/login")
                if other == "editor":
                    response = self.client.post("/editor/reviews/10", data={"action": "approve"})
                else:
                    response = self.client.post("/author/submissions/new", data={
                        "title": "越權", "content": "測試", "category_id": 1,
                    })
                self.assertEqual(response.status_code, 303)
                self.db.execute.assert_not_called()

    def test_wrong_role_tampered_and_expired_tokens_are_rejected(self):
        author = self.accounts["author"]
        token = create_session_token(author["id"], "author", author["display_name"])
        with patch("auth.time.time", return_value=0):
            expired = create_session_token(2, "editor", "過期編輯")
        for invalid in [token, token + "changed", expired]:
            with self.subTest(token_type=invalid[-10:]):
                self.client.cookies.clear()
                self.client.cookies.set(SESSION_COOKIES["editor"], invalid)
                self.assertEqual(self.client.get("/editor/reviews").status_code, 303)

    def test_one_role_expiring_does_not_expire_the_other(self):
        with patch("auth.time.time", return_value=100):
            author_token = create_session_token(1, "author", "測試author")
        with patch("auth.time.time", return_value=200):
            editor_token = create_session_token(2, "editor", "測試editor")
        self.client.cookies.set(SESSION_COOKIES["author"], author_token)
        self.client.cookies.set(SESSION_COOKIES["editor"], editor_token)
        with patch("auth.time.time", return_value=SESSION_TTL_SECONDS + 150):
            self.assertEqual(self.client.get(WORKSPACES["author"]).status_code, 303)
            self.workspace("editor")

    def test_legacy_cookie_is_ignored_and_removed_after_login(self):
        self.client.cookies.set(
            "submission_session", create_session_token(1, "author", "舊作者"), domain="testserver.local"
        )
        self.assertEqual(self.client.get(WORKSPACES["author"]).status_code, 303)
        self.assertEqual(self.client.get(WORKSPACES["editor"]).status_code, 303)
        self.login("editor")
        self.assertNotIn("submission_session", self.client.cookies)
        self.workspace("editor")
        self.assertEqual(self.client.get(WORKSPACES["author"]).status_code, 303)


if __name__ == "__main__":
    unittest.main()
