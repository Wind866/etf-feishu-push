"""etf-feishu-push 工程健康回归测试（纯标准库，不联网、不触碰 Secret）。"""
from __future__ import annotations

import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules"}


def read(path: str) -> str:
    with open(os.path.join(ROOT, path), encoding="utf-8") as fh:
        return fh.read()


class TestPythonSyntax(unittest.TestCase):
    def test_every_python_file_compiles(self):
        broken = []
        for dirpath, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for filename in filenames:
                if not filename.endswith(".py"):
                    continue
                path = os.path.join(dirpath, filename)
                with open(path, encoding="utf-8", errors="ignore") as fh:
                    try:
                        compile(fh.read(), path, "exec")
                    except SyntaxError as exc:
                        broken.append(f"{os.path.relpath(path, ROOT)}:{exc.lineno} {exc.msg}")
        self.assertEqual(broken, [], "存在语法错误：\n" + "\n".join(broken))


class TestDependencies(unittest.TestCase):
    """上游 breaking change 会直接打断每日 20:00 的推送，所有依赖都要有主版本上界。"""

    def test_requirements_have_upper_bounds(self):
        for name in ("requirements.txt", "requirements-optional.txt"):
            path = os.path.join(ROOT, name)
            if not os.path.exists(path):
                continue
            with self.subTest(file=name):
                for line in read(name).splitlines():
                    line = line.split("#", 1)[0].strip()
                    if not line or line.startswith("-"):
                        continue
                    self.assertRegex(line, r"<", f"{name} 缺少版本上界：{line}")

    def test_requirements_files_exist(self):
        self.assertTrue(os.path.exists(os.path.join(ROOT, "requirements.txt")))


class TestWorkflowContract(unittest.TestCase):
    """Worker 通过 workflow_dispatch 触发；失败必须可见、必须能告警。"""

    WORKFLOW = os.path.join(".github", "workflows", "daily-push.yml")

    def test_dispatch_declared(self):
        self.assertIn("workflow_dispatch", read(self.WORKFLOW))

    def test_missing_webhook_fails_loudly(self):
        """缺 FEISHU_WEBHOOK 必须 exit 1，不能静默跳过。"""
        src = read(self.WORKFLOW)
        self.assertIn("缺少 FEISHU_WEBHOOK", src)
        self.assertRegex(src, r"exit 1")

    def test_history_not_written_on_failure(self):
        """失败时不得回写 history.json，否则污染次日交易日判重。"""
        src = read(self.WORKFLOW)
        self.assertIn("if: success()", src)
        self.assertNotIn("Commit history\n        if: always()", src)

    def test_failure_alert_exists(self):
        self.assertIn("失败告警", read(self.WORKFLOW))


class TestRepoHygiene(unittest.TestCase):
    def test_gitignore_allows_tests(self):
        path = os.path.join(ROOT, ".gitignore")
        if not os.path.exists(path):
            self.skipTest("本仓库无 .gitignore")
        for line in read(".gitignore").splitlines():
            stripped = line.strip()
            if stripped.startswith("test_") or stripped in ("tests/", "tests"):
                self.fail(f".gitignore 会吞掉测试文件：{stripped}")


if __name__ == "__main__":
    unittest.main()
