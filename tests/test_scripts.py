"""把 `_verify_*.py` 集成脚本纳入 pytest

为什么这么做
------------
那 5 个脚本合计约 300 条断言，覆盖了端到端场景（真实装配、MCP 子进程、
CLI 命令分发等），手工跑起来输出可读、方便人看。把它们重写成 pytest 函数
只会重复一遍同样的断言，还要维护两份。

所以这里用子进程把它们跑一遍，只断言"退出码为 0"——
**一份断言真相，两种运行方式**：
- 想看细节：`python _verify_fixes.py`
- 想跑全量回归：`python -m pytest`
"""

import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 纯离线脚本：不联网、不花 API 费用
OFFLINE_SCRIPTS = [
    "_verify_fixes.py",
    "_verify_boot.py",
    "_verify_permissions.py",
    "_verify_tracing.py",
    "_verify_streaming.py",
]

# 会真的拉起一个 MCP 子进程（本地 poetry server），稍慢但完全离线
RUNTIME_SCRIPTS = ["_verify_mcp_runtime.py"]


def _run_script(name: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, name],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        encoding="utf-8",
        errors="replace",
    )


@pytest.mark.parametrize("script", OFFLINE_SCRIPTS)
def test_offline_verification_script(script):
    path = os.path.join(ROOT, script)
    assert os.path.isfile(path), f"缺少脚本 {script}"

    result = _run_script(script)
    tail = "\n".join((result.stdout or "").splitlines()[-15:])
    assert result.returncode == 0, (
        f"{script} 失败（退出码 {result.returncode}）\n"
        f"--- 输出尾部 ---\n{tail}\n"
        f"--- stderr ---\n{(result.stderr or '')[-800:]}"
    )


@pytest.mark.parametrize("script", RUNTIME_SCRIPTS)
def test_runtime_verification_script(script):
    path = os.path.join(ROOT, script)
    assert os.path.isfile(path), f"缺少脚本 {script}"

    result = _run_script(script)
    combined = (result.stdout or "") + (result.stderr or "")
    # MCP server 会往 stderr 打 INFO 日志，所以不能只看 returncode
    assert "验证通过" in combined, (
        f"{script} 未报告通过\n{combined[-1500:]}"
    )
