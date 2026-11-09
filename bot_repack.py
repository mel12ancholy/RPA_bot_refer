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
  - 不同影刀版本的 Package 构造签名可能不同，C# 端会先试社区已知签名，
    不匹配时自动枚举构造函数/方法并打印诊断信息。

依赖（运行重签必须）：
  - Windows + 影刀客户端（提供 ShadowBot.Runtime.dll）
  - 任意 .NET SDK（8/9/10 均可；脚本自动探测 dotnet.exe 绝对路径，不依赖 PATH）
  缺少任一依赖时可用 --no-sign 只改包不签名，用于验证「导入层是否校验签名」。
  自动探测失败时用 --dotnet 手动指定；目标框架可用 --tfm 覆盖（默认按已装 SDK 选）。

用法：
  python bot_repack.py                          # 处理所在文件夹全部 .bot，输出到 repacked/
  python bot_repack.py --bot D:\\x\\my.bot --new-version 1.0.1
  python bot_repack.py --bot D:\\x\\my.bot --new-uuid abc123 --dry-run
  python bot_repack.py --batch D:\\bots --new-version 1.0.1

  打包成 exe 与完整参数说明见 README.md。

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
import uuid as uuidlib
from datetime import datetime
from pathlib import Path

# ==================== 配置区：按需修改 ====================
BOT_FILE = r"D:\影刀指令库\影刀_测试_26.11.1.bot"   # 【兼容保留】仅 --bot 单文件模式使用；无参数时忽略
NEW_UUID = "auto"                    # "auto" = 每次生成全新 uuid（推荐）；None = 不改
NEW_CODE = "auto"                    # "auto" = 自动识别并替换 activity_xxx；None = 不改
OLD_CODE = None                      # 显式指定旧编码（配合 new-code）；None = 自动识别
NEW_VERSION = "auto"                 # "auto" = 原版本末位 +1；也可填 "26.11.2"；None = 不改
OUTPUT_FILE = None                   # None = 原文件名 + _repacked.bot
SCAN_DIR = None                      # None = exe 所在文件夹；可填绝对路径
OUTPUT_DIR = "repacked"              # 输出子目录名（相对扫描目录）
RUNTIME_DLL = r"E:\shadowbot\ShadowBot Private\shadowbot-5.25.66\ShadowBot.Runtime.dll"                   # 低优先级回退；共享目录部署时优先用 exe 同目录的 DLL
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
    static int Main(string[] args)
    {
        try
        {
            Run(args);
            return 0;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine("SIGSTORE_ERROR: " + ex.GetBaseException().Message);
            Console.Error.WriteLine(ex.ToString());
            return 2;
        }
    }

    static void Run(string[] args)
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

        if (args.Length >= 4)
        {
            foreach (var extra in args[3].Split(';', StringSplitOptions.RemoveEmptyEntries))
                if (Directory.Exists(extra) && !probeDirectories.Contains(extra))
                    probeDirectories.Add(extra);
        }

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
        var appId = rootElement.GetProperty("uuid").GetString() ?? "";
        var appName = rootElement.TryGetProperty("name", out var nameProp)
            ? (nameProp.GetString() ?? "") : "";

        var assembly = AssemblyLoadContext.Default.LoadFromAssemblyPath(runtimePath);
        var appInfoType = assembly.GetType("ShadowBot.Runtime.Packages.PackageAppInfo");
        var packageType = assembly.GetType("ShadowBot.Runtime.Packages.Package");
        var helperType = assembly.GetType("ShadowBot.Runtime.Packages.PackageHelper");
        if (appInfoType == null || packageType == null || helperType == null)
            throw new Exception("类型缺失: PackageAppInfo=" + (appInfoType != null)
                + ", Package=" + (packageType != null)
                + ", PackageHelper=" + (helperType != null));

        var constructors = packageType.GetConstructors(
            BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance);
        Console.WriteLine("constructors=" + string.Join(" | ", constructors.Select(
            c => string.Join(",", c.GetParameters().Select(p => p.ParameterType.Name + " " + p.Name)))));
        Console.WriteLine("sigstore methods=" + string.Join(" | ", helperType
            .GetMethods(BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static)
            .Where(m => m.Name.IndexOf("Sigstore", StringComparison.OrdinalIgnoreCase) >= 0)
            .Select(m => m.Name + "(" + string.Join(",", m.GetParameters()
                .Select(p => p.ParameterType.Name)) + ")")));

        var appInfo = Activator.CreateInstance(appInfoType)!;
        SetStringProperty(appInfoType, appInfo, new[] { "AppId", "Id", "Uuid" }, appId);
        SetStringProperty(appInfoType, appInfo, new[] { "Name", "AppName", "PackageName" }, appName);

        var parentDir = Directory.GetParent(projectDir)!.FullName;
        var dirName = Path.GetFileName(projectDir);
        object package;
        try
        {
            package = CreatePackage(packageType, appInfoType, appInfo, parentDir, dirName);
        }
        catch (Exception first) when (IsCorrupt(first))
        {
            var signature = Path.Combine(projectDir, "package.sigstore");
            if (!File.Exists(signature)) throw;
            var parked = Path.Combine(Path.GetTempPath(),
                "botsig-" + Guid.NewGuid().ToString("N") + ".bak");
            File.Move(signature, parked);
            Console.WriteLine("warn=构造报损坏，临时移走 package.sigstore 后重试: " + parked);
            package = CreatePackage(packageType, appInfoType, appInfo, parentDir, dirName);
        }

        var test = FindSigstoreMethod(helperType, "TestPackageSigstore");
        var write = FindSigstoreMethod(helperType, "WritePackageSigstore");

        object? before;
        try { before = test.Invoke(null, new[] { package }); }
        catch (Exception ex) { before = "error:" + ex.GetBaseException().Message; }
        Console.WriteLine("before=" + before);
        if (args.Length >= 3 && args[2] == "write")
        {
            write.Invoke(null, new[] { package });
            object? after;
            try { after = test.Invoke(null, new[] { package }); }
            catch (Exception ex) { after = "error:" + ex.GetBaseException().Message; }
            Console.WriteLine("after=" + after);
        }
    }

    static bool IsCorrupt(Exception ex)
    {
        var text = ex.ToString();
        return text.IndexOf("损坏", StringComparison.Ordinal) >= 0
            || text.IndexOf("corrupt", StringComparison.OrdinalIgnoreCase) >= 0
            || text.IndexOf("PackageUnreadable", StringComparison.OrdinalIgnoreCase) >= 0;
    }

    static void SetStringProperty(Type type, object instance, string[] candidates, string value)
    {
        foreach (var name in candidates)
        {
            var prop = type.GetProperty(name,
                BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance);
            if (prop != null && prop.CanWrite && prop.PropertyType == typeof(string))
            {
                prop.SetValue(instance, value);
                Console.WriteLine("appInfo." + name + " set");
                return;
            }
        }
        Console.WriteLine("warn=string property not found: " + string.Join("/", candidates));
    }

    static object CreatePackage(Type packageType, Type appInfoType, object appInfo,
                                string parentDir, string dirName)
    {
        var exact5 = packageType.GetConstructor(
            new[] { typeof(string), appInfoType, typeof(bool?), typeof(string), typeof(bool) });
        if (exact5 != null)
        {
            Console.WriteLine("ctor used=exact 5-arg signature");
            Console.WriteLine("ctor args=rootPath=" + parentDir + " | targetPath=" + dirName);
            return exact5.Invoke(new object?[] { parentDir, appInfo, null, dirName, false })!;
        }

        var exact4 = packageType.GetConstructor(
            new[] { typeof(string), appInfoType, typeof(bool?), typeof(string) });
        if (exact4 != null)
        {
            Console.WriteLine("ctor used=exact 4-arg signature (5.25.x)");
            Console.WriteLine("ctor args=rootPath=" + parentDir + " | targetPath=" + dirName);
            return exact4.Invoke(new object?[] { parentDir, appInfo, null, dirName })!;
        }
        Console.WriteLine("warn=no known signature, probing constructors");

        var constructors = packageType.GetConstructors(
            BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance);
        var errors = new List<string>();
        foreach (var ctor in constructors.OrderByDescending(c => c.GetParameters().Length))
        {
            var ps = ctor.GetParameters();
            var vals = new object?[ps.Length];
            int stringCount = 0;
            bool skip = false;
            for (int i = 0; i < ps.Length; i++)
            {
                var t = ps[i].ParameterType;
                if (t == appInfoType || (t.IsAssignableFrom(appInfoType) && t != typeof(object)))
                    vals[i] = appInfo;
                else if (t == typeof(string))
                    vals[i] = stringCount++ == 0 ? parentDir : dirName;
                else if (Nullable.GetUnderlyingType(t) != null) vals[i] = null;
                else if (t == typeof(bool)) vals[i] = false;
                else if (t == typeof(int)) vals[i] = 0;
                else if (t == typeof(long)) vals[i] = 0L;
                else if (t.IsValueType) { skip = true; break; }
                else vals[i] = null;
            }
            if (skip) continue;
            try
            {
                var instance = ctor.Invoke(vals);
                Console.WriteLine("ctor used=" + string.Join(",",
                    ps.Select(p => p.ParameterType.Name + " " + p.Name)));
                Console.WriteLine("ctor args=" + string.Join(" | ", ps.Select(
                    (p, idx) => p.Name + "=" + (vals[idx] == null ? "<null>" : vals[idx]!.ToString()))));
                return instance!;
            }
            catch (Exception ex)
            {
                errors.Add(string.Join(",", ps.Select(p => p.ParameterType.Name))
                    + " -> " + ex.GetBaseException().Message);
            }
        }
        throw new Exception("无法构造 Package。尝试结果：" + string.Join(" ; ", errors));
    }

    static MethodInfo FindSigstoreMethod(Type helperType, string name)
    {
        var candidates = helperType.GetMethods(
            BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static)
            .Where(m => m.Name.IndexOf("Sigstore", StringComparison.OrdinalIgnoreCase) >= 0)
            .ToList();
        var method = candidates.FirstOrDefault(m => m.Name == name && m.GetParameters().Length == 1);
        if (method == null)
            throw new Exception("未找到单参数静态方法 " + name + "；候选："
                + string.Join(" | ", candidates.Select(m => m.Name + "("
                    + string.Join(",", m.GetParameters().Select(p => p.ParameterType.Name)) + ")")));
        return method;
    }
}
'''


def decode_console(data: bytes | None) -> str:
    """中文 Windows 上 dotnet 输出可能是 UTF-8 或 GBK，逐个尝试，避免解码崩在 reader 线程。"""
    if not data:
        return ""
    for encoding in ("utf-8", "gbk", "cp936"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def run_console(cmd: list[str], timeout: int) -> tuple[int, str, str]:
    """字节捕获 + 手动解码，返回 (returncode, stdout, stderr)。"""
    result = subprocess.run(cmd, capture_output=True, timeout=timeout)
    return result.returncode, decode_console(result.stdout), decode_console(result.stderr)


def runtime_candidates(explicit: str | None, base_dir: Path) -> list[Path]:
    """按优先级返回 ShadowBot.Runtime.dll 候选；重签失败时由 resign() 逐个回退。

    优先级：--runtime-dll 显式 > exe 同目录 > exe 同目录/runtime/ > 配置区 > 本机安装目录。
    """
    result: list[Path] = []

    def add(path: Path | None) -> None:
        if path is None:
            return
        try:
            resolved = path.expanduser().resolve()
        except OSError:
            return
        if resolved.is_file() and resolved not in result:
            result.append(resolved)

    if explicit:
        given = Path(explicit).expanduser().resolve()
        if not given.is_file():
            raise RuntimeError(f"指定的 Runtime DLL 不存在：{given}")
        add(given)

    add(base_dir / "ShadowBot.Runtime.dll")
    add(base_dir / "runtime" / "ShadowBot.Runtime.dll")
    if RUNTIME_DLL:
        add(Path(RUNTIME_DLL))

    installed: list[tuple[tuple[int, ...], Path]] = []
    for env_key, fallback in (("ProgramFiles", r"C:\Program Files"),
                              ("ProgramFiles(x86)", r"C:\Program Files (x86)")):
        root = Path(os.environ.get(env_key, fallback)) / "ShadowBot"
        if not root.is_dir():
            continue
        for child in root.iterdir():
            dll = child / "ShadowBot.Runtime.dll"
            if child.is_dir() and dll.is_file():
                nums = tuple(int(n) for n in re.findall(r"\d+", child.name)[:4])
                installed.append((nums, dll))
    for _, dll in sorted(installed, key=lambda item: item[0], reverse=True):
        add(dll)

    if not result:
        raise RuntimeError(
            "未找到 ShadowBot.Runtime.dll。请把该文件放到 exe 同目录（建议连同同目录其它 "
            "ShadowBot*.dll 一起拷贝，或整套放进 exe 同目录的 runtime/ 子文件夹），"
            "也可用 --runtime-dll 指定本机安装路径。"
        )
    return result


def runtime_probe_dirs(runtimes: list[Path]) -> list[str]:
    """收集候选目录与影刀自带 .NET 运行时目录，供 C# 端解析程序集依赖。"""
    result: list[str] = []

    def add(path: Path) -> None:
        if path.is_dir() and str(path) not in result:
            result.append(str(path))

    for dll in runtimes:
        root = dll.parent
        add(root)
        add(root / "support_x64" / "ProcessLauncher")
        for base in (root, root.parent):
            for sub in ("dotnet.win-x64/shared/Microsoft.WindowsDesktop.App",
                        "dotnet.win-x64/shared/Microsoft.NETCore.App"):
                shared = base / sub
                if shared.is_dir():
                    for version_dir in shared.iterdir():
                        if version_dir.is_dir():
                            add(version_dir)
    return result


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
            code, out, _ = run_console([str(exe), "--list-sdks"], 30)
        except (OSError, subprocess.SubprocessError):
            continue
        if code != 0:
            continue
        versions = [tuple(int(n) for n in re.findall(r"\d+", line)[:3])
                    for line in out.splitlines() if re.search(r"\d", line)]
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


def app_dir() -> Path:
    """打包成 exe 后用 exe 所在目录，源码运行时用脚本所在目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def bump_version(value) -> str | None:
    """版本末位 +1，如 26.11.0 -> 26.11.1；非数字则保持原版本。"""
    if value is None:
        return None
    parts = str(value).split(".")
    try:
        parts[-1] = str(int(parts[-1]) + 1)
        return ".".join(parts)
    except ValueError:
        return None


def unique_output_path(out_dir: Path, filename: str) -> Path:
    """同名已存在时自动加 _2/_3，方便多次运行累积多份不同 uuid 的副本。"""
    target = out_dir / filename
    if not target.exists():
        return target
    stem, suffix = Path(filename).stem, Path(filename).suffix
    index = 2
    while True:
        candidate = out_dir / f"{stem}_{index}{suffix}"
        if not candidate.exists():
            return candidate
        index += 1


TEXT_SUFFIXES = {".json", ".py", ".txt", ".xml", ".yaml", ".yml", ".md", ".js",
                 ".ini", ".cfg", ".config", ".flow", ".bat", ".ps1"}
SKIP_FILES = {"package.sigstore"}


def iter_text_files(root: Path):
    """只遍历可安全做字符串替换的文本文件；二进制/加密流程自动跳过。"""
    for file in sorted(root.rglob("*")):
        if not file.is_file() or file.name in SKIP_FILES:
            continue
        if file.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            text = file.read_text(encoding="utf-8-sig")
        except (UnicodeDecodeError, OSError):
            continue
        yield file, text


def guess_activity_code(root: Path) -> str | None:
    """从包内文本中找出出现最多的 activity_xxx 作为指令集编码。"""
    counts: dict[str, int] = {}
    for _, text in iter_text_files(root):
        for code in set(re.findall(r"\bactivity_[A-Za-z0-9_]+\b", text)):
            counts[code] = counts.get(code, 0) + text.count(code)
    return max(counts, key=counts.get) if counts else None


def replace_identity(root: Path, old_uuid: str | None, new_uuid: str | None,
                     old_code: str | None, new_code: str | None) -> None:
    """全包替换身份标识：只改 package.json 的 uuid 会留下内部引用不一致。"""
    pairs = []
    if old_uuid and new_uuid and old_uuid != new_uuid:
        pairs.append((old_uuid, new_uuid, "uuid"))
    if old_code and new_code and old_code != new_code:
        pairs.append((old_code, new_code, "编码"))
    if not pairs:
        return
    for file, text in iter_text_files(root):
        updated, hits = text, []
        for old, new, label in pairs:
            count = updated.count(old)
            if count:
                updated = updated.replace(old, new)
                hits.append(f"{label}x{count}")
        if hits:
            file.write_text(updated, encoding="utf-8")
            print(f"  替换 {file.relative_to(root).as_posix()}: {', '.join(hits)}")


def rename_identity_paths(root: Path, old_uuid: str | None, new_uuid: str | None,
                          old_code: str | None, new_code: str | None) -> None:
    """指令集编码常出现在目录名/文件名里（如 xbot_extensions/<code>/），同步重命名。"""
    pairs = []
    if old_uuid and new_uuid and old_uuid != new_uuid:
        pairs.append((old_uuid, new_uuid))
    if old_code and new_code and old_code != new_code:
        pairs.append((old_code, new_code))
    if not pairs:
        return
    entries = sorted([p for p in root.rglob("*")
                      if any(old in p.name for old, _ in pairs)],
                     key=lambda p: len(p.parts), reverse=True)
    for path in entries:
        new_name = path.name
        for old, new in pairs:
            new_name = new_name.replace(old, new)
        if new_name == path.name:
            continue
        target = path.with_name(new_name)
        if target.exists():
            continue
        path.rename(target)
        print(f"  重命名: {path.relative_to(root).as_posix()} -> {target.name}")


def inspect_bot(root: Path, package_dir: Path) -> None:
    """列出包结构与身份标识，用于判断还需要改哪些字段。"""
    data = json.loads((package_dir / "package.json").read_text(encoding="utf-8-sig"))
    scalars = {k: v for k, v in data.items() if not isinstance(v, (dict, list))}
    print("  package.json 标量字段: " + ", ".join(f"{k}={v!r}" for k, v in scalars.items()))
    files = [f for f in sorted(root.rglob("*")) if f.is_file()]
    print(f"  包内文件({len(files)}):")
    for file in files[:60]:
        print(f"    {file.relative_to(root).as_posix()}")
    if len(files) > 60:
        print(f"    ... 另有 {len(files) - 60} 个")
    code = guess_activity_code(root)
    print(f"  疑似指令集编码: {code or '未找到 activity_ 前缀字符串'}")
    old_uuid = data.get("uuid")
    if old_uuid:
        locations = [f.relative_to(root).as_posix()
                     for f, text in iter_text_files(root) if old_uuid in text]
        print(f"  旧 uuid 出现位置: {', '.join(locations) if locations else '未出现在文本文件中'}")


def patch_package_json(package_dir: Path, new_uuid: str | None, new_version: str | None) -> None:
    path = package_dir / "package.json"
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    changed = []
    if new_uuid:
        old = data.get("uuid")
        if old != new_uuid:
            data["uuid"] = new_uuid
            changed.append(f"uuid: {old} -> {new_uuid}")
    if new_version:
        old = data.get("version")
        if old != new_version:
            data["version"] = new_version
            changed.append(f"version: {old} -> {new_version}")
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print("  修改: " + ("; ".join(changed) if changed else "无（仅重新打包）"))
    keys = [k for k in data if re.search(r"id|uuid|version", k, re.I)]
    print("  相关字段: " + ", ".join(f"{k}={data[k]!r}" for k in keys))


def resign(project_dir: Path, runtimes: list[Path], backup_root: Path,
           dotnet: Path, tfm: str) -> None:
    """逐个尝试 Runtime 候选，用影刀自己的 API 重新生成 package.sigstore。"""
    sigstore = project_dir / "package.sigstore"
    if sigstore.exists():
        backup_root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = backup_root / f"{project_dir.name}.package.sigstore.{stamp}.bak"
        shutil.copy2(sigstore, backup)
        print(f"  已备份原签名: {backup}")

    probe_arg = ";".join(runtime_probe_dirs(runtimes)) or "-"
    temp = Path(tempfile.mkdtemp(prefix="bot-sigstore-"))
    try:
        (temp / "SigstoreRepair.csproj").write_text(
            CS_PROJECT.replace("{tfm}", tfm), encoding="utf-8")
        (temp / "Program.cs").write_text(CS_PROGRAM, encoding="utf-8")
        failures: list[str] = []
        for runtime in runtimes:
            print(f"  尝试 Runtime: {runtime}")
            code, out, err = run_console(
                [str(dotnet), "run", "--project", str(temp / "SigstoreRepair.csproj"),
                 "--configuration", "Release", "--",
                 str(runtime), str(project_dir), "write", probe_arg],
                300,
            )
            for line in out.splitlines():
                stripped = line.strip()
                if stripped.startswith("CSC : warning"):
                    continue
                print("  " + stripped)
            if code == 0 and "before=" in out and "after=True" in out:
                return
            reason = (err.strip() or out.strip() or f"exit={code}")[:800]
            failures.append(f"- {runtime}: {reason}")
            print("  [失败] 该 Runtime 重签未成功，尝试下一个候选")
            if not sigstore.exists():
                backups = sorted(backup_root.glob(f"{project_dir.name}.package.sigstore.*.bak"))
                if backups:
                    shutil.copy2(backups[-1], sigstore)
        raise RuntimeError(
            "重签失败，所有 Runtime 候选均未通过：\n" + "\n".join(failures) +
            "\n建议把影刀版本目录下的全部 DLL 拷到 exe 同目录的 runtime/ 文件夹，"
            "或用 --runtime-dll 指定本机安装路径。"
        )
    finally:
        shutil.rmtree(temp, ignore_errors=True)


def repack(extract_root: Path, output: Path) -> None:
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for file in sorted(extract_root.rglob("*")):
            if file.is_file():
                archive.write(file, file.relative_to(extract_root).as_posix())


def process(bot: Path, new_uuid: str | None, new_version: str | None,
            output: Path | None, runtimes: list[Path], do_sign: bool,
            dry_run: bool, backup_root: Path, dotnet: Path | None = None,
            tfm: str = "net10.0", inspect: bool = False,
            old_code: str | None = None, new_code: str | None = None) -> None:
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

        original = json.loads((package_dir / "package.json").read_text(encoding="utf-8-sig"))
        old_uuid = original.get("uuid")

        if inspect:
            inspect_bot(root, package_dir)
            return
        if dry_run:
            keys = [k for k in original if re.search(r"id|uuid|version|name", k, re.I)]
            print("  [dry-run] " + ", ".join(f"{k}={original[k]!r}" for k in keys))
            return

        if new_version == "auto":
            new_version = bump_version(original.get("version"))
            if new_version:
                print(f"  自动递增版本: {original.get('version')} -> {new_version}")
        if new_uuid == "auto":
            new_uuid = str(uuidlib.uuid4())
            print(f"  自动生成新 uuid: {new_uuid}")
        if new_code == "auto":
            old_code = old_code or guess_activity_code(root)
            if not old_code:
                print("  [警告] 未识别到 activity_ 指令集编码，本次仅替换 uuid")
                new_code = None
            else:
                new_code = "activity_" + uuidlib.uuid4().hex[:8]
                print(f"  自动识别指令集编码: {old_code} -> {new_code}")
        elif new_code and not old_code:
            old_code = guess_activity_code(root)
            if old_code:
                print(f"  自动识别旧指令集编码: {old_code}")

        replace_identity(root, old_uuid, new_uuid, old_code, new_code)
        rename_identity_paths(root, old_uuid, new_uuid, old_code, new_code)
        package_dir = locate_package_dir(root)
        patch_package_json(package_dir, new_uuid, new_version)
        if do_sign:
            resign(package_dir, runtimes, backup_root, dotnet, tfm)
        else:
            print("  [警告] 跳过重签；若导入后打开报「应用文件已损坏」，请去掉 --no-sign。")

        target = output or bot.with_name(bot.stem + "_repacked.bot")
        target.parent.mkdir(parents=True, exist_ok=True)
        repack(root, target)
        print(f"  已生成: {target}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="修改并重新签名影刀 .bot 指令文件；无参数时处理程序所在文件夹下全部 .bot")
    parser.add_argument("--bot", help="单文件模式：指定 .bot 路径")
    parser.add_argument("--batch", help="【兼容】等同 --scan-dir")
    parser.add_argument("--scan-dir", default=SCAN_DIR,
                        help="扫描目录（默认 exe 所在文件夹）")
    parser.add_argument("--output-dir", default=OUTPUT_DIR,
                        help="输出子目录（默认 repacked，相对扫描目录）")
    parser.add_argument("--output", default=OUTPUT_FILE, help="输出 .bot 路径（仅单文件模式）")
    parser.add_argument("--new-uuid", default=NEW_UUID, help='新 uuid；"auto" = 自动生成')
    parser.add_argument("--new-version", default=NEW_VERSION, help='"auto" = 版本末位 +1')
    parser.add_argument("--runtime-dll", default=None,
                        help="手动指定（最高优先级）；默认依次尝试同目录、配置区、本机安装目录")
    parser.add_argument("--dotnet", default=DOTNET_EXE, help="dotnet.exe 绝对路径")
    parser.add_argument("--tfm", default=TFM, help="目标框架，如 net8.0 / net10.0")
    parser.add_argument("--old-code", default=OLD_CODE, help="旧指令集编码")
    parser.add_argument("--new-code", default=NEW_CODE, help='"auto" = 自动识别并替换')
    parser.add_argument("--inspect", action="store_true", help="列出包结构与身份标识，不改动")
    parser.add_argument("--no-sign", action="store_true", help="跳过重签（实验用）")
    parser.add_argument("--dry-run", action="store_true", help="只看关键字段，不改动")
    args = parser.parse_args()

    no_write = args.dry_run or args.inspect
    do_sign = DO_SIGN and not args.no_sign and not no_write
    backup_root = app_dir() / "_sigstore_backups"

    try:
        runtimes = runtime_candidates(args.runtime_dll, app_dir()) if do_sign else []
        if runtimes:
            print(f"Runtime 候选（按顺序尝试，共 {len(runtimes)} 个）:")
            for item in runtimes:
                print(f"  - {item}")
        dotnet = None
        tfm = args.tfm or TFM or "net10.0"
        if do_sign:
            dotnet, detected_tfm = find_dotnet(args.dotnet)
            tfm = args.tfm or TFM or detected_tfm

        if args.bot:
            bot = Path(args.bot).expanduser()
            output = Path(args.output).expanduser() if args.output else None
            process(bot, args.new_uuid, args.new_version, output,
                    runtimes, do_sign, args.dry_run, backup_root, dotnet, tfm,
                    args.inspect, args.old_code, args.new_code)
            return 0

        scan_target = args.scan_dir or args.batch
        scan_dir = (Path(scan_target).expanduser().resolve()
                    if scan_target else app_dir())
        out_dir = Path(args.output_dir).expanduser()
        if not out_dir.is_absolute():
            out_dir = scan_dir / out_dir
        if not no_write:
            out_dir.mkdir(parents=True, exist_ok=True)

        bots = sorted(p for p in scan_dir.glob("*.bot") if p.is_file())
        if not bots:
            raise FileNotFoundError(f"目录下没有 .bot 文件：{scan_dir}")
        print(f"扫描目录: {scan_dir}")
        print(f"输出目录: {out_dir}")
        print(f"找到 {len(bots)} 个 .bot 文件")

        failures: list[tuple[str, str]] = []
        for bot in bots:
            target = unique_output_path(out_dir, bot.name) if not no_write else None
            try:
                process(bot, args.new_uuid, args.new_version, target,
                        runtimes, do_sign, args.dry_run, backup_root, dotnet, tfm,
                        args.inspect, args.old_code, args.new_code)
            except Exception as exc:
                failures.append((bot.name, str(exc)))
                print(f"  [失败] {bot.name}: {exc}", file=sys.stderr)

        if no_write:
            return 0
        print(f"\n完成：成功 {len(bots) - len(failures)}/{len(bots)}，输出目录 {out_dir}")
        if failures:
            for name, reason in failures:
                print(f"  失败：{name} -> {reason}", file=sys.stderr)
            return 1
        return 0
    except Exception as exc:
        print(f"\n[失败] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
