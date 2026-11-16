"""转发代理（EM_PROXY_URL）回归测试：不联网，用本地 mock 服务器验证协议。"""
from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

from etfpush import sources

EM_ULIST = "https://push2.eastmoney.com/api/qt/ulist.np/get"
TENCENT = "https://qt.gtimg.cn/q=sh000001"

CANNED = json.dumps({"data": {"diff": [{"f12": "510300", "f62": 1.0}]}})


class _MockProxy(BaseHTTPRequestHandler):
    """记录收到的请求并按 script 返回；断言在主线程做。"""

    script = []          # [(expect_token, expect_path_hint, status, body)]
    requests_log = []

    def do_GET(self):
        _MockProxy.requests_log.append({
            "path": self.path,
            "token": self.headers.get("x-proxy-token"),
        })
        expect_token, expect_hint, status, body = _MockProxy.script.pop(0)
        if expect_token is not None and self.headers.get("x-proxy-token") != expect_token:
            status, body = 403, b"forbidden"
        if expect_hint and expect_hint not in self.path:
            status, body = 400, b"bad url"
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # 静默
        pass


class TestProxyChannel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _MockProxy)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        _MockProxy.requests_log = []
        _MockProxy.script = []

    def _env(self, token="tok-123"):
        return {"EM_PROXY_URL": f"http://127.0.0.1:{self.port}/em",
                "EM_PROXY_TOKEN": token}

    def test_em_request_goes_through_proxy_with_token(self):
        _MockProxy.script = [( "tok-123", "url=https%3A%2F%2Fpush2.eastmoney.com",
                               200, CANNED.encode())]
        with patch.dict("os.environ", self._env()):
            text = sources.http_get(EM_ULIST, params={"secids": "1.510300"}, retries=1)
        self.assertIn("f62", text)
        # 代理只收到一次请求，且带上了共享密钥与编码后的目标 URL
        self.assertEqual(len(_MockProxy.requests_log), 1)
        self.assertEqual(_MockProxy.requests_log[0]["token"], "tok-123")
        self.assertIn("url=https%3A%2F%2Fpush2.eastmoney.com",
                      _MockProxy.requests_log[0]["path"])

    def test_non_em_host_skips_proxy(self):
        # 腾讯探测源不走代理：mock 里不排任何剧本，收到请求即失败
        with patch.dict("os.environ", self._env()):
            with patch.object(sources, "_via_requests", return_value="v_sh000001=\"1\""), \
                 patch.object(sources, "_via_curl", side_effect=RuntimeError("no curl")):
                text = sources.http_get(TENCENT, retries=1)
        self.assertEqual(text, "v_sh000001=\"1\"")
        self.assertEqual(_MockProxy.requests_log, [])

    def test_proxy_failure_falls_back_to_direct(self):
        # 代理 403（密钥不对）→ 自动回退直连，结果仍然可用
        _MockProxy.script = [("", "", 403, b"forbidden")]
        with patch.dict("os.environ", self._env(token="tok-123")), \
             patch.object(sources, "_via_requests", return_value=CANNED), \
             patch.object(sources, "_via_curl", side_effect=RuntimeError("no curl")):
            text = sources.http_get(EM_ULIST, retries=1)
        self.assertIn("f62", text)
        self.assertEqual(len(_MockProxy.requests_log), 1)

    def test_no_proxy_env_keeps_original_channels(self):
        # 未配置代理：东财请求也不应碰 mock 服务器
        with patch.dict("os.environ", {"EM_PROXY_URL": "", "EM_PROXY_TOKEN": ""}), \
             patch.object(sources, "_via_requests", return_value=CANNED), \
             patch.object(sources, "_via_curl", side_effect=RuntimeError("no curl")):
            text = sources.http_get(EM_ULIST, retries=1)
        self.assertIn("f62", text)
        self.assertEqual(_MockProxy.requests_log, [])

    def test_proxy_empty_response_is_rejected(self):
        # 代理返回 200 空响应：视为失败（不当作有效数据），走回退
        _MockProxy.script = [("tok-123", "", 200, b"")]
        with patch.dict("os.environ", self._env()), \
             patch.object(sources, "_via_requests", return_value=CANNED), \
             patch.object(sources, "_via_curl", side_effect=RuntimeError("no curl")):
            text = sources.http_get(EM_ULIST, retries=1)
        self.assertIn("f62", text)


if __name__ == "__main__":
    unittest.main()
