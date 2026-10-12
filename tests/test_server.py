import http.client
import json
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

from test_core import make_session
from server import create_server
from core import LikeStore


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.session = make_session(self.root / "recordings")
        self.server = create_server("127.0.0.1", 0, self.root / "recordings", self.root / "state")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        conn.close()
        return result

    def test_serves_catalog_images_and_dynamic_events_without_raw_file_route(self):
        status, _, body = self.request("GET", "/api/sessions")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)[0]["frames"], 1)
        status, _, body = self.request("GET", f"/api/session/{self.session.name}")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["rows"][0]["index"], 0)
        status, _, body = self.request("GET", f"/api/image/{self.session.name}/frame/0")
        self.assertEqual((status, body), (200, b"frame"))
        status, _, body = self.request("GET", f"/api/event/{self.session.name}/0?ms=10")
        self.assertEqual(status, 200)
        self.assertTrue(body.startswith(b"\x89PNG\r\n\x1a\n"))
        status, _, _ = self.request("GET", f"/api/image/{self.session.name}/raw/0")
        self.assertEqual(status, 404)
        status, _, _ = self.request("GET", f"/{self.session.name}/events.bin")
        self.assertEqual(status, 404)

    def test_login_tracks_liker_and_second_click_cancels(self):
        LikeStore(self.root / "state" / "likes.sqlite3").create_account("tester_b", "1234")
        status, headers, _ = self.request("GET", f"/api/session/{self.session.name}")
        self.assertEqual(status, 200)
        visitor_cookie = headers["Set-Cookie"].split(";", 1)[0]
        origin = f"http://127.0.0.1:{self.port}"
        post_headers = {"Cookie": visitor_cookie, "Origin": origin,
                        "Content-Type": "application/json"}
        status, _, _ = self.request("POST", "/api/auth/login",
                                    body=json.dumps({"username": "tester_b", "password": "bad"}).encode(),
                                    headers=post_headers)
        self.assertEqual(status, 401)
        status, headers, body = self.request("POST", "/api/auth/login",
                                             body=json.dumps({"username": "tester_b", "password": "1234"}).encode(),
                                             headers=post_headers)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["user"]["username"], "tester_b")
        auth_cookie = headers["Set-Cookie"].split(";", 1)[0]
        cookies = f"{visitor_cookie}; {auth_cookie}"
        status, _, body = self.request(
            "POST", f"/api/like/{self.session.name}/0", body=b"{}",
            headers={"Cookie": cookies, "Origin": origin, "Content-Type": "application/json"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"liked": True, "count": 1})
        status, _, body = self.request("GET", f"/api/session/{self.session.name}", headers={"Cookie": cookies})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["likes"]["0"], {"count": 1, "mine": True})
        status, _, body = self.request("GET", f"/api/likes/{self.session.name}/0")
        self.assertEqual((status, json.loads(body)),
                         (200, {"count": 1, "users": ["tester_b"], "anonymous_count": 0}))
        status, _, body = self.request("POST", f"/api/like/{self.session.name}/0",
                                       body=b"{}", headers={"Cookie": cookies,
                                                             "Origin": origin,
                                                             "Content-Type": "application/json"})
        self.assertEqual((status, json.loads(body)), (200, {"liked": False, "count": 0}))

    def test_anonymous_button_clicks_toggle_one_shared_favorite(self):
        origin = f"http://127.0.0.1:{self.port}"
        first = str(uuid.uuid4())
        second = str(uuid.uuid4())
        for visitor, expected in [
            (first, {"liked": True, "count": 1}),
            (second, {"liked": False, "count": 0}),
            (second, {"liked": True, "count": 1}),
        ]:
            status, _, body = self.request("POST", f"/api/like/{self.session.name}/0",
                                           body=b"{}", headers={
                                               "Cookie": f"davis_visitor={visitor}",
                                               "Origin": origin, "Content-Type": "application/json",
                                           })
            self.assertEqual((status, json.loads(body)), (200, expected))

    def test_like_click_cancels_old_anonymous_vote_from_same_browser(self):
        store = LikeStore(self.root / "state" / "likes.sqlite3")
        store.create_account("tester_c", "1234")
        token, _ = store.login("tester_c", "1234", str(uuid.uuid4()))
        visitor = str(uuid.uuid4())
        store.toggle(self.session.name, 0, visitor)
        status, _, body = self.request("POST", f"/api/like/{self.session.name}/0",
                                       body=b"{}", headers={
                                           "Cookie": f"davis_auth={token}; davis_visitor={visitor}",
                                           "Origin": f"http://127.0.0.1:{self.port}",
                                           "Content-Type": "application/json",
                                       })
        self.assertEqual((status, json.loads(body)), (200, {"liked": False, "count": 0}))

    def test_serves_30_bin_metadata_and_images(self):
        status, _, body = self.request("GET", f"/api/bins/{self.session.name}/0")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual((data["center_us"], data["center_source"]), (1_000_000, "exposure_midpoint"))
        self.assertEqual(len(data["bins"]), 30)
        self.assertEqual(data["window_us"], 100_000)
        self.assertEqual(data["bins"][0]["start_offset_us"], -100_000)
        self.assertEqual(data["bins"][-1]["end_offset_us"], 0)
        status, _, body = self.request("GET", f"/api/bin/{self.session.name}/0/15")
        self.assertEqual(status, 200)
        self.assertTrue(body.startswith(b"\x89PNG\r\n\x1a\n"))
        status, _, _ = self.request("GET", f"/api/bin/{self.session.name}/0/30")
        self.assertEqual(status, 400)

    def test_hidden_session_is_absent_and_direct_url_is_unavailable(self):
        (self.root / "state" / "hidden_sessions.json").write_text(
            json.dumps([self.session.name]), encoding="utf-8")
        hidden_server = create_server("127.0.0.1", 0, self.root / "recordings", self.root / "state")
        hidden_thread = threading.Thread(target=hidden_server.serve_forever, daemon=True)
        hidden_thread.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", hidden_server.server_port, timeout=5)
            conn.request("GET", "/api/sessions")
            response = conn.getresponse()
            self.assertEqual((response.status, json.loads(response.read())), (200, []))
            conn.request("GET", f"/api/session/{self.session.name}")
            response = conn.getresponse()
            self.assertEqual(response.status, 404)
            response.read()
            conn.close()
        finally:
            hidden_server.shutdown()
            hidden_server.server_close()
            hidden_thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
