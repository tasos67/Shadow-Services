"""Development smoke test: syntax check + bot construction + cog loading.

Usage:  python check.py
"""
from __future__ import annotations

import asyncio
import importlib
import pathlib
import py_compile
import sys
import traceback

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))


def compile_all() -> bool:
    ok = True
    for path in pathlib.Path(".").rglob("*.py"):
        if "data" in path.parts or "logs" in path.parts or "__pycache__" in path.parts:
            continue
        try:
            py_compile.compile(str(path), doraise=True)
        except py_compile.PyCompileError as exc:
            ok = False
            print(f"SYNTAX FAIL: {path}\n  {exc.msg}")
    if ok:
        print("syntax: all files compile")
    return ok


async def smoke() -> bool:
    import main as main_module
    from database import db

    bot = main_module.AllInOneBot()
    try:
        await db.init()
        cog_dir = pathlib.Path("cogs")
        for path in sorted(cog_dir.glob("*.py")):
            if path.name.startswith("_"):
                continue
            try:
                await bot.load_extension(f"cogs.{path.stem}")
            except Exception:
                print(f"COG LOAD FAIL: {path.stem}")
                traceback.print_exc()
                return False
        bot._load_persistent_views()
        print(f"cogs: {len(bot.cogs)} loaded, views: {len(bot.persistent_views)} registered")
        for command in bot.tree.get_commands():
            if hasattr(command, "commands"):
                for sub in command.commands:
                    print(f"  /{command.name} {sub.name} - {sub.description}")
            else:
                print(f"  /{command.name} - {command.description}")
    except Exception:
        traceback.print_exc()
        return False
    finally:
        try:
            await db.close()
        except Exception:
            pass
        try:
            await asyncio.wait_for(bot.close(), timeout=10)
        except Exception:
            pass
    return True


def main() -> int:
    if not compile_all():
        return 1
    result = asyncio.run(smoke())
    print("smoke test:", "PASSED" if result else "FAILED")
    return 0 if result else 1


if __name__ == "__main__":
    raise SystemExit(main())
