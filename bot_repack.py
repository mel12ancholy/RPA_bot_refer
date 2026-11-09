#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
影刀 .bot 指令文件重打包工具（单文件测试版）

用途：解包 .bot -> 修改 package.json 中的 uuid / version -> 用影刀自带 Runtime 重签
      package.sigstore -> 重新打包为 .bot，用于绕过同组织内导入时的「存在版本冲突」。

原理：
  - .bot 本质是 zip。
  - 冲突判定键是「指令集 id(uuid) + version」，组织级唯一，所以改掉其中一个即可。
  - 影刀 5.23 起有应用签名校验，改完文件必须重签，否则打开时报「应用文件已损坏」。
  - 重签不是伪造签名：脚本调用本机 ShadowBot.Runtime.dll 的
    ShadowBot.Runtime.Packages.PackageHelper.WritePackageSigstore() 生成有效签名。

依赖（运行重签必须）：
  - Windows + 影刀客户端（提供 ShadowBot.Runtime.dll）
  - 任意 .NET SDK（8/9/10 均可；脚本自动探测 dotnet.exe 绝对路径，不依赖 PATH）
  缺少任一依赖时可用 --no-sign 只改包不签名，用于验证「导入层是否校验签名」。
  自动探测失败时用 --dotnet 手动指定；目标框架可用 --tfm 覆盖（默认按已装 SDK 选）。

用法：
  python bot_repack.py                          # 使用下方配置区
  python bot_repack.py --bot D:\\x\\my.bot --new-version 1.0.1
  python bot_repack.py --bot D:\\x\\my.bot --new-uuid abc123 --dry-run
  python bot_repack.py --batch D:\\bots --new-version 1.0.1

注意：仅限处理你自己/你公司有权分发的指令；绕过产品约束前请确认不违反适用许可协议。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

# ==================== 配置区：按需修改 ====================
BOT_FILE = r"D:\path\to\your.bot"   # 待处理的 .bot 文件（单文件测试用）
NEW_UUID = None                      # 新的指令集 id / uuid；None = 不改
NEW_VERSION = "1.0.1"                # 新版本号；None = 不改
OUTPUT_FILE = None                   # None = 原文件名 + _repacked.bot
RUNTIME_DLL = None                   # None = 自动探测已安装影刀的 ShadowBot.Runtime.dll
DOTNET_EXE = None                    # None = 自动探测 dotnet.exe（不依赖 PATH）
TFM = None                           # None = 按已装 SDK 自动选（如 net10.0）；可填 net8.0
DO_SIGN = True                       # True = 改完重签；False = 只改包
# ========================================================

CS_PROJECT = """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <OutputType>Exe</OutputType>
    <TargetFramework>{tfm}</TargetFramework>
    <ImplicitUsings>disable</ImplicitUsings>
    <Nullable>enable</Nullable>
  </PropertyGroup>
</Project>
"""

CS_PROGRAM = r'''
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Runtime.Loader;
using System.Text.Json;

class Program
{
    static void Main(string[] args)
    {
        var runtimePath = Path.GetFullPath(args[0]);
        var projectDir = Path.GetFullPath(args[1]);
        var runtimeDir = Path.GetDirectoryName(runtimePath)!;
        var shadowBotRoot = Directory.GetParent(runtimeDir)!.FullName;

        var probeDirectories = new List<string> { runtimeDir };
        foreach (var relativeRoot in new[]
        {
            Path.Combine("dotnet.win-x64", "shared", "Microsoft.WindowsDesktop.App"),
            Path.Combine("dotnet.win-x64", "shared", "Microsoft.NETCore.App")
        })
        {
            var root = Path.Combine(shadowBotRoot, relativeRoot);
            if (Directory.Exists(root))
                probeDirectories.AddRange(Directory.GetDirectories(root)
                    .OrderByDescending(path => path, StringComparer.OrdinalIgnoreCase));
        }
        probeDirectories.Add(Path.Combine(runtimeDir, "support_x64", "ProcessLauncher"));

        AssemblyLoadContext.Default.Resolving += (_, name) =>
        {
            foreach (var directory in probeDirectories)
            {
                var candidate = Path.Combine(directory, name.Name + ".dll");
                if (File.Exists(candidate))
                    return AssemblyLoadContext.Default.LoadFromAssemblyPath(candidate);
            }
            return null;
        };

        using var document = JsonDocument.Parse(File.ReadAllText(Path.Combine(projectDir, "package.json")));
        var rootElement = document.RootElement;
        var appId = rootElement.GetProperty("uuid").GetString()!;
        var appName = rootElement.GetProperty("name").GetString()!;

        var assembly = AssemblyLoadContext.Default.LoadFromAssemblyPath(runtimePath);
        var appInfoType = assembly.GetType("ShadowBot.Runtime.Packages.PackageAppInfo")!;
        var packageType = assembly.GetType("ShadowBot.Runtime.Packages.Package")!;
        var helperType = assembly.GetType("ShadowBot.Runtime.Packages.PackageHelper")!;

        var appInfo = Activator.CreateInstance(appInfoType)!;
        appInfoType.GetProperty("AppId")!.SetValue(appInfo, appId);
        appInfoType.GetProperty("Name")!.SetValue(appInfo, appName);

        var constructor = packageType.GetConstructor(
            new[] { typeof(string), appInfoType, typeof(bool?), typeof(string), typeof(bool) })!;
        var package = constructor.Invoke(new object?[]
        {
            Directory.GetParent(projectDir)!.FullName,
            appInfo,
            null,
            Path.GetFileName(projectDir),
            false
        });

        var test = helperType.GetMethod("TestPackageSigstore", BindingFlags.Public | BindingFlags.Static)!;
        var write = helperType.GetMethod("WritePackageSigstore", BindingFlags.Public | BindingFlags.Static)!;

        Console.WriteLine("before=" + test.Invoke(null, new[] { package }));
        if (args.Length >= 3 && args[2] == "write")
        {
            write.Invoke(null, new[] { package });
            Console.WriteLine("after=" + test.Invoke(null, new[] { package }));
        }
    }
}
'''


def find_runtime(explicit: str | None) -> Path:
    """定位 ShadowBot.Runtime.dll；显式路径优先，否则按版本选最新安装。"""
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not path.is_file():
            raise RuntimeError(f"指定的 Runtime DLL 不存在：{path}")
        return path

    roots = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "ShadowBot",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "ShadowBot",
    ]
    candidates: list[tuple[tuple[int, ...], Path]] = []
    for root in roots:
        if not root.is_dir():
            continue
        for child in root.iterdir():
            dll = child / "ShadowBot.Runtime.dll"
            if child.is_dir() and dll.is_file():
                nums = tuple(int(n) for n in re.findall(r"\d+", child.name)[:4])
                candidates.append((nums, dll))
    if not candidates:
        raise RuntimeError(
            "未找到 ShadowBot.Runtime.dll。请用 --runtime-dll 手动指定， "
            "例如 C:\\Program Files\\ShadowBot\\shadowbot-5.25.66\\ShadowBot.Runtime.dll"
        )
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def find_dotnet(explicit: str | None) -> tuple[Path, str]:
    """探测 dotnet.exe 与已装 SDK，返回 (exe, 自动目标框架)。不依赖 PATH。"""
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    which = shutil.which("dotnet")
    if which:
        candidates.append(Path(which))
    envs = [os.environ.get("DOTNET_ROOT"), os.environ.get("ProgramFiles"),
            os.environ.get("ProgramFiles(x86)"), os.environ.get("LOCALAPPDATA")]
    for env in envs:
        if env:
            candidates.append(Path(env) / "dotnet.exe")
            candidates.append(Path(env) / "dotnet" / "dotnet.exe")
    candidates.append(Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "dotnet" / "dotnet.exe")
    candidates.append(Path.home() / ".dotnet" / "dotnet.exe")

    seen: set[str] = set()
    for exe in candidates:
        try:
            key = str(exe.resolve()).lower()
        except OSError:
            continue
        if not exe.is_file() or key in seen:
            continue
        seen.add(key)
        try:
            result = subprocess.run(
                [str(exe), "--list-sdks"], capture_output=True, text=True, timeout=30
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode != 0:
            continue
        versions = [tuple(int(n) for n in re.findall(r"\d+", line)[:3])
                    for line in result.stdout.splitlines() if re.search(r"\d", line)]
        versions = [v for v in versions if v]
        if versions:
            tfm = f"net{max(v[0] for v in versions)}.0"
            print(f"dotnet: {exe}")
            print(f"SDK: {', '.join('.'.join(map(str, v)) for v in versions)} -> 使用 {tfm}")
            return exe, tfm

    raise RuntimeError(
        "未找到可用的 dotnet SDK。请确认已安装 .NET SDK，"
        '或用 --dotnet "C:/Program Files/dotnet/dotnet.exe" 手动指定；'
        "也可加 --no-sign 先只改包不签名。"
    )


def locate_package_dir(root: Path) -> Path:
    """在解压目录中找到含 package.json 的项目目录。"""
    direct = root / "package.json"
    if direct.is_file():
        return root
    files = list(root.rglob("package.json"))
    preferred = [p for p in files if p.parent.name == "xbot_robot"]
    if preferred:
        return preferred[0].parent
    if len(files) == 1:
        return files[0].parent
    raise RuntimeError(
        "无法唯一确定 package.json 所在目录，候选："
        + ", ".join(str(p.relative_to(root)) for p in files)
    )


def patch_package_json(package_dir: Path, new_uuid: str | None, new_version: str | None) -> None:
    path = package_dir / "package.json"
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    changed = []
    if new_uuid:
        old = data.get("uuid")
        data["uuid"] = new_uuid
        changed.append(f"uuid: {old} -> {new_uuid}")
    if new_version:
        old = data.get("version")
        data["version"] = new_version
        changed.append(f"version: {old} -> {new_version}")
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print("  修改: " + ("; ".join(changed) if changed else "无（仅重新打包）"))
    keys = [k for k in data if re.search(r"id|uuid|version", k, re.I)]
    print("  相关字段: " + ", ".join(f"{k}={data[k]!r}" for k in keys))


def resign(project_dir: Path, runtime: Path, backup_root: Path,
           dotnet: Path, tfm: str) -> None:
    """调用影刀自己的 Runtime 生成 package.sigstore。"""
    sigstore = project_dir / "package.sigstore"
    if sigstore.exists():
        backup_root.mkdir(parents=True, exist_ok=True)
        backup = backup_root / f"{project_dir.name}.package.sigstore.bak"
        shutil.copy2(sigstore, backup)
        print(f"  已备份原签名: {backup}")

    temp = Path(tempfile.mkdtemp(prefix="bot-sigstore-"))
    try:
        (temp / "SigstoreRepair.csproj").write_text(
            CS_PROJECT.replace("{tfm}", tfm), encoding="utf-8")
        (temp / "Program.cs").write_text(CS_PROGRAM, encoding="utf-8")
        result = subprocess.run(
            [str(dotnet), "run", "--project", str(temp / "SigstoreRepair.csproj"),
             "--configuration", "Release", "--", str(runtime), str(project_dir), "write"],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"重签失败（目标框架 {tfm}）：{result.stdout}\n{result.stderr}\n"
                "若提示缺少 net8.0 参考包，可试 --tfm net10.0；反之可试 --tfm net8.0。"
            )
        for line in result.stdout.splitlines():
            print("  " + line.strip())
        if "before=" not in result.stdout or "after=True" not in result.stdout:
            raise RuntimeError("重签未得到 after=True，请检查影刀版本与 Runtime 是否匹配。")
    finally:
        shutil.rmtree(temp, ignore_errors=True)


def repack(extract_root: Path, output: Path) -> None:
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for file in sorted(extract_root.rglob("*")):
            if file.is_file():
                archive.write(file, file.relative_to(extract_root).as_posix())


def process(bot: Path, new_uuid: str | None, new_version: str | None,
            output: Path | None, runtime: Path | None, do_sign: bool,
            dry_run: bool, backup_root: Path, dotnet: Path | None = None,
            tfm: str = "net10.0") -> None:
    print(f"\n[处理] {bot}")
    if not bot.is_file():
        raise FileNotFoundError(f"找不到 .bot 文件：{bot}")
    if not zipfile.is_zipfile(bot):
        raise RuntimeError(f"不是有效的 zip/.bot 文件：{bot}")

    with tempfile.TemporaryDirectory(prefix="bot-repack-") as tmp:
        root = Path(tmp)
        with zipfile.ZipFile(bot) as archive:
            archive.extractall(root)
        package_dir = locate_package_dir(root)
        print(f"  项目目录: {package_dir.relative_to(root)}")

        if dry_run:
            data = json.loads((package_dir / "package.json").read_text(encoding="utf-8-sig"))
            keys = [k for k in data if re.search(r"id|uuid|version|name", k, re.I)]
            print("  [dry-run] " + ", ".join(f"{k}={data[k]!r}" for k in keys))
            return

        patch_package_json(package_dir, new_uuid, new_version)
        if do_sign:
            resign(package_dir, runtime, backup_root, dotnet, tfm)
        else:
            print("  [警告] 跳过重签；若导入后打开报「应用文件已损坏」，请去掉 --no-sign。")

        target = output or bot.with_name(bot.stem + "_repacked.bot")
        target.parent.mkdir(parents=True, exist_ok=True)
        repack(root, target)
        print(f"  已生成: {target}")


def main() -> int:
    parser = argparse.ArgumentParser(description="修改并重新签名影刀 .bot 指令文件")
    parser.add_argument("--bot", default=BOT_FILE, help="待处理的 .bot 文件路径")
    parser.add_argument("--batch", help="批量处理该目录下所有 .bot 文件")
    parser.add_argument("--new-uuid", default=NEW_UUID, help="新的指令集 id / uuid")
    parser.add_argument("--new-version", default=NEW_VERSION, help="新的版本号")
    parser.add_argument("--output", default=OUTPUT_FILE, help="输出 .bot 路径（单文件模式）")
    parser.add_argument("--runtime-dll", default=RUNTIME_DLL, help="ShadowBot.Runtime.dll 路径")
    parser.add_argument("--dotnet", default=DOTNET_EXE, help="dotnet.exe 绝对路径（自动探测失败时用）")
    parser.add_argument("--tfm", default=TFM, help="目标框架，如 net8.0 / net10.0（默认按已装 SDK 选择）")
    parser.add_argument("--no-sign", action="store_true", help="跳过重签（实验用）")
    parser.add_argument("--dry-run", action="store_true", help="只看 package.json 关键字段，不改动")
    args = parser.parse_args()

    do_sign = DO_SIGN and not args.no_sign
    backup_root = Path.cwd() / "_sigstore_backups"

    try:
        runtime = find_runtime(args.runtime_dll) if do_sign and not args.dry_run else None
        if runtime:
            print(f"Runtime: {runtime}")
        dotnet = None
        tfm = args.tfm or TFM or "net10.0"
        if do_sign and not args.dry_run:
            dotnet, detected_tfm = find_dotnet(args.dotnet)
            tfm = args.tfm or TFM or detected_tfm
        if args.batch:
            directory = Path(args.batch).expanduser()
            bots = sorted(directory.glob("*.bot"))
            if not bots:
                raise FileNotFoundError(f"目录下没有 .bot 文件：{directory}")
            for bot in bots:
                process(bot, args.new_uuid, args.new_version, None,
                        runtime, do_sign, args.dry_run, backup_root, dotnet, tfm)
        else:
            bot = Path(args.bot).expanduser()
            output = Path(args.output).expanduser() if args.output else None
            process(bot, args.new_uuid, args.new_version, output,
                    runtime, do_sign, args.dry_run, backup_root, dotnet, tfm)
    except Exception as exc:
        print(f"\n[失败] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
