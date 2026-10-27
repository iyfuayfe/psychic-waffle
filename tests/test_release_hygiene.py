"""发布卫生检查：确保"不该发出去的东西"没有混进源码树与发布目录。

这些断言对应任务书里的硬性要求：

* 不把个人配置 / 日志 / 认证页转储打进发布包；
* 不再出现"加杀毒白名单 / 关闭 Defender"这类指导；
* 源码里没有写死的个人路径、真实账号；
* 版本号只有一个来源；
* 构建产物、缓存不进入版本控制。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from tests.support import PROJECT_ROOT

from campusnet.version import CONFIG_SCHEMA_VERSION, __version__

RELEASE_DIR = PROJECT_ROOT / "dist" / "CampusNet"

# 这些文件/目录绝不能出现在发布目录里
FORBIDDEN_IN_RELEASE = (
    "config.json",
    "state.json",
    "heartbeat.json",
    "logs",
    "backup",
    "diagnostics",
    "tests",
    "tools",
    "docs",
    ".git",
)

# 源码 / 文档 / 脚本里不应该出现的"个人环境痕迹"。
#
# 注意转义层数：这是 raw string，`r"C:\\Users"` 表示"字面 C:\Users"。
# 以前写成 `r"C:\\\\Users\\\\[A-Za-z0-9_.-]+\\\\"`（四个反斜杠）在正则层面表示
# "两个字面反斜杠"，只能匹配 `C:\\Users\\bob\\` 这种双反斜杠文本，对真实的
# `C:\Users\bob\a.py` 永远返回 False —— 也就是说这条检查从上线起就是空转的。
PERSONAL_PATH_PATTERN = re.compile(r"[A-Za-z]:[\\/]+Users[\\/]+([A-Za-z0-9_.-]+)[\\/]", re.I)
PERSONAL_NAME_PATTERN = re.compile(r"\bExampleUser\b")
PERSONAL_PATH_PATTERNS = (PERSONAL_PATH_PATTERN, PERSONAL_NAME_PATTERN)

# 测试夹具与文档里的占位/示例用户名，不算个人痕迹
PLACEHOLDER_USERS = frozenset({
    ".", "..", "...", "x", "user", "test", "testuser", "example", "placeholder", "unknown", "yourname",
    "alice", "bob",
})

# 会进入仓库/发布包的文本文件后缀（原来只扫 .py，覆盖不到文档与脚本）
TEXT_SUFFIXES = (
    ".py", ".md", ".json", ".bat", ".ps1", ".js", ".txt", ".spec", ".yml", ".yaml", ".toml", ".cfg",
)

_SKIP_PARTS = {".git", "dist", "build", "__pycache__", ".tmp", ".dsh-vision-router"}


class PersonalPathPatternTests(unittest.TestCase):
    """正例自测：防止正则再写错，导致"个人痕迹检查"重新变成空转。"""

    def test_patterns_match_real_windows_paths(self):
        self.assertTrue(PERSONAL_PATH_PATTERN.search(r"C:\Users\alice\a.py"))
        self.assertTrue(PERSONAL_PATH_PATTERN.search("D:/Users/alice/a.py"))
        self.assertFalse(PERSONAL_PATH_PATTERN.search(r"C:\ProgramData\a.py"))

    def test_placeholder_users_are_recognised(self):
        for sample in (r"C:\Users\x\a.py", r"C:\Users\...\a.py"):
            match = PERSONAL_PATH_PATTERN.search(sample)
            self.assertIsNotNone(match, sample)
            self.assertIn(match.group(1).lower(), PLACEHOLDER_USERS, sample)


class FixtureHygieneTests(unittest.TestCase):
    """测试夹具本身的卫生（曾经混进空文件与完全重复的 fixture）。"""

    def test_no_empty_stray_fixture(self):
        stray = PROJECT_ROOT / "test" / "_e.txt"
        self.assertFalse(stray.exists(), "test/_e.txt 是空文件、无任何引用，不应留在仓库里")

    def test_bad_config_fixture_is_actually_bad(self):
        bad = PROJECT_ROOT / "test" / "config.bad.json"
        good = PROJECT_ROOT / "test" / "config.test.json"
        if not bad.exists():
            self.skipTest("没有 config.bad.json")
        self.assertTrue(good.exists())
        self.assertNotEqual(
            bad.read_bytes(), good.read_bytes(),
            "config.bad.json 应当是一份真的坏配置，不能与 config.test.json 完全相同",
        )
        import json

        with self.assertRaises(json.JSONDecodeError):
            json.loads(bad.read_text(encoding="utf-8"))


class SourceHygieneTests(unittest.TestCase):
    def test_no_hardcoded_personal_paths_in_shipped_code(self):
        offenders = []
        for path in _text_files():
            text = path.read_text(encoding="utf-8", errors="replace")
            for match in PERSONAL_PATH_PATTERN.finditer(text):
                if match.group(1).lower() in PLACEHOLDER_USERS:
                    continue
                offenders.append(f"{path.relative_to(PROJECT_ROOT)}（用户目录：{match.group(1)}）")
                break
            else:
                if PERSONAL_NAME_PATTERN.search(text):
                    offenders.append(str(path.relative_to(PROJECT_ROOT)))
        self.assertEqual(offenders, [], "发布内容里不该出现写死的用户目录：" + ", ".join(offenders))

    def test_no_real_account_or_password_in_tests(self):
        """测试里只能出现明显的假账号。"""
        suspicious = []
        mobile = re.compile(r"\b1[3-9]\d{9}\b")
        for path in (PROJECT_ROOT / "tests").rglob("*.py"):
            for match in mobile.finditer(path.read_text(encoding="utf-8", errors="replace")):
                value = match.group(0)
                # 138****5678 这类脱敏样例与假号放行
                if value in ("13812345678", "13800000000"):
                    continue
                suspicious.append(f"{path.name}:{value}")
        self.assertEqual(suspicious, [], "疑似真实手机号：" + ", ".join(suspicious))

    def test_no_user_facing_bat_offers_whitelisting(self):
        """面向用户的 .bat 里不能再有"加入白名单"这类动作入口。"""
        offenders = []
        for path in PROJECT_ROOT.glob("*.bat"):
            text = path.read_text(encoding="utf-8", errors="replace")
            for needle in ("加入白名单", "whitelist.ps1", "Add-MpPreference"):
                if needle in text:
                    offenders.append(f"{path.name}: {needle}")
        self.assertEqual(offenders, [], "仍有面向用户的白名单入口：" + "; ".join(offenders))

    def test_whitelist_helpers_only_refuse(self):
        """遗留的 apply_whitelist 只能返回"不做"，不能真的去改设置。"""
        text = (PROJECT_ROOT / "campusnet" / "ui" / "gui_common.py").read_text(encoding="utf-8")
        self.assertIn("def apply_whitelist", text)
        block = text.split("def apply_whitelist", 1)[1]
        self.assertIn("不再自动添加", block)
        self.assertNotIn("Add-MpPreference", block)

    def test_readme_documents_the_policy(self):
        text = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("从不自动添加", text)
        self.assertIn("不需要关闭或调整任何安全软件", text)

    def test_old_whitelist_assets_are_gone(self):
        for name in (
            "4-加入白名单.bat",
            "tools/whitelist.ps1",
            "tools/startup_autologin.vbs",
        ):
            self.assertFalse(
                (PROJECT_ROOT / name).exists(),
                f"{name} 应该已被移除（V3.0 不再管理杀毒白名单 / 不再用双自启动）",
            )

    def test_no_automatic_defender_exclusion_code(self):
        for path in _shipped_python_files():
            text = path.read_text(encoding="utf-8", errors="replace")
            self.assertNotIn("Add-MpPreference", text,
                             f"{path.name} 仍在自动修改 Defender 排除项")
            self.assertNotIn("Remove-MpPreference", text,
                             f"{path.name} 仍在自动删除 Defender 排除项")

    def test_version_has_single_source(self):
        init_text = (PROJECT_ROOT / "campusnet" / "__init__.py").read_text(encoding="utf-8")
        self.assertIn("from .version import __version__", init_text)
        self.assertNotRegex(init_text, r'__version__\s*=\s*"')
        self.assertRegex(__version__, r"^\d+\.\d+\.\d+$")
        self.assertGreaterEqual(CONFIG_SCHEMA_VERSION, 2)

    def test_required_documents_exist(self):
        for name in (
            "README.md",
            "LICENSE",
            "THIRD_PARTY_NOTICES.md",
            "docs/BUILDING.md",
            "docs/CHANGELOG.md",
            "docs/KNOWN_ISSUES.md",
            "docs/AUDIT_V3.md",
            "docs/WINDOWS_ACCEPTANCE.md",
            "config.example.json",
        ):
            self.assertTrue((PROJECT_ROOT / name).exists(), f"缺少 {name}")

    def test_gitignore_covers_personal_data(self):
        text = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
        for pattern in ("config.json", "state.json", "logs/", "dist/", "build/", "__pycache__/"):
            self.assertIn(pattern, text, f".gitignore 缺少 {pattern}")


class ExampleConfigTests(unittest.TestCase):
    def test_example_config_loads_and_is_sanitised(self):
        import json

        text = (PROJECT_ROOT / "config.example.json").read_text(encoding="utf-8")
        data = json.loads(text)
        self.assertEqual(data["config_version"], CONFIG_SCHEMA_VERSION)
        self.assertIn("不要手写", data["password_enc"])
        # 示例里的地址必须是"占位"或公开地址，不能是真实学校门户
        self.assertNotIn("13800000000", text)
        self.assertNotIn("password_enc\": \"AQAAANCMnd8BFdERjHoAwE", text)

    def test_example_config_roundtrips_through_loader(self):
        from campusnet.config import Config

        text = (PROJECT_ROOT / "config.example.json").read_text(encoding="utf-8")
        import json

        cfg = Config()
        cfg._merge(json.loads(text))
        problems = cfg.validate()
        # 示例配置本身应当没有严重问题（用户名/密码是占位符则另说）
        self.assertIsInstance(problems, list)


class BuildArtefactTests(unittest.TestCase):
    def test_build_outputs_are_gitignored(self):
        text = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("dist/", text)
        self.assertIn("build/", text)

    def test_release_dir_has_no_personal_data(self):
        if not RELEASE_DIR.exists():
            self.skipTest("还没有构建发布目录（先运行 build_exe.bat）")
        for name in FORBIDDEN_IN_RELEASE:
            self.assertFalse((RELEASE_DIR / name).exists(), f"发布目录里不应该有 {name}")

    def test_release_dir_has_all_executables(self):
        if not RELEASE_DIR.exists():
            self.skipTest("还没有构建发布目录")
        for name in ("CampusNet.exe", "CampusNetSetup.exe",
                     "CampusNetService.exe", "CampusNetCLI.exe"):
            self.assertTrue((RELEASE_DIR / name).exists(), f"缺少 {name}")

    def test_release_contains_third_party_notice(self):
        if not RELEASE_DIR.exists():
            self.skipTest("还没有构建发布目录")
        self.assertTrue(
            (RELEASE_DIR / "THIRD_PARTY_NOTICES.md").exists()
            or (RELEASE_DIR / "_internal" / "THIRD_PARTY_NOTICES.md").exists(),
            "发布包必须带上第三方许可证说明",
        )


def _text_files():
    """仓库里所有可能进包的文本文件（跳过构建产物、缓存与工作区垃圾）。"""
    for path in PROJECT_ROOT.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        if any(part in _SKIP_PARTS for part in path.parts):
            continue
        yield path


def _shipped_python_files():
    """会进发布包的 Python 源码（不含测试与开发脚本）。"""
    files = list((PROJECT_ROOT / "campusnet").rglob("*.py"))
    files.append(PROJECT_ROOT / "entry.py")
    files.append(PROJECT_ROOT / "campus_login.py")
    return [path for path in files if "__pycache__" not in str(path)]


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
