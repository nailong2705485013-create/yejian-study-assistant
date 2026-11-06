"""先从清华 PyPI 镜像安装；缺少包或指定版本时自动回退官方 PyPI。"""

import os
import subprocess
import sys


MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"
OFFICIAL = "https://pypi.org/simple"


def install(index: str, arguments: list[str]) -> subprocess.CompletedProcess:
    # 仅为安装子进程设置来源，避免已有 extra-index-url 使两个源同时竞争。
    environment = {
        key: value for key, value in os.environ.items()
        if key.upper() not in {
            "PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "PIP_NO_INDEX", "PIP_FIND_LINKS"
        }
    }
    environment["PIP_CONFIG_FILE"] = os.devnull
    environment["PYTHONUTF8"] = "1"
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
         "--index-url", index, *arguments],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
        errors="replace",
    )
    print(result.stdout, end="", flush=True)
    return result


def main() -> int:
    arguments = sys.argv[1:]
    if not arguments or arguments == ["--help"]:
        print("用法：用虚拟环境的 Python 运行 install_packages.py 包名 [其他 pip 参数]")
        print("先使用清华源；缺包或缺少指定版本时，再使用官方 PyPI。")
        return 0

    result = install(MIRROR, arguments)
    if result.returncode:
        missing = (
            "No matching distribution found", "Could not find a version that satisfies"
        )
        if any(message in result.stdout for message in missing):
            print("清华源未找到所需包或版本，回退官方 PyPI。", flush=True)
            result = install(OFFICIAL, arguments)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
