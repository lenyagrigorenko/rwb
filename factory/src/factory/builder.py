"""Builds and checks generated components in Docker (no local toolchains required)."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path

SDK = "mcr.microsoft.com/dotnet/sdk:10.0"
ERROR = re.compile(r"^(?P<file>/src/[^(]+)\((?P<line>\d+),(?P<col>\d+)\): error (?P<code>\w+): (?P<msg>.+?)(?: \[/src.*)?$")


@dataclass
class BuildError:
    file: str
    line: int
    code: str
    message: str


async def run(cmd: list[str], cwd: Path | None = None, timeout: int = 900) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(*cmd, cwd=cwd, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "timeout"
    return proc.returncode or 0, out.decode("utf-8", "replace")


async def dotnet(backend: Path, *args: str, timeout: int = 900) -> tuple[int, str]:
    return await run(["docker", "run", "--rm", "-v", f"{backend.resolve()}:/src", "-v", "nuget-cache:/root/.nuget/packages",
                      "-w", "/src", SDK, "dotnet", *args], timeout=timeout)


async def build_backend(backend: Path, solution: str) -> tuple[bool, list[BuildError], str]:
    code, out = await dotnet(backend, "build", solution, "-nologo", "-v", "q", "-clp:NoSummary")
    errors, seen = [], set()
    for line in out.splitlines():
        m = ERROR.match(line.strip())
        if m:
            key = (m["file"], m["line"], m["code"])
            if key in seen:
                continue
            seen.add(key)
            errors.append(BuildError(m["file"].removeprefix("/src/"), int(m["line"]), m["code"], m["msg"]))
    return code == 0 and not errors, errors, out


async def test_backend(backend: Path, solution: str, filter_: str | None = None) -> tuple[bool, str]:
    args = ["test", solution, "-nologo", "--logger", "trx;LogFileName=results.trx", "--logger", "console;verbosity=minimal",
            "--results-directory", "/src/TestResults"]
    if filter_:
        args += ["--filter", filter_]
    code, out = await dotnet(backend, *args, timeout=1200)
    return code == 0, out


ANDROID_BUILDER = "factory/android-builder:1"


async def build_apk(mobile: Path, api_url: str | None = None) -> tuple[bool, Path | None, str]:
    """assembleDebug in the Android builder image; returns the APK path."""
    args = ["gradle", "assembleDebug", "--no-daemon", "-q"]
    if api_url:
        args.append(f"-PapiUrl={api_url}")
    code, out = await run(["docker", "run", "--rm", "-v", f"{mobile.resolve()}:/src", "-v", "gradle-cache:/cache",
                           ANDROID_BUILDER, *args], timeout=1800)
    apk = mobile / "app" / "build" / "outputs" / "apk" / "debug" / "app-debug.apk"
    return code == 0 and apk.exists(), apk if apk.exists() else None, out[-3000:]
