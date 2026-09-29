#!/usr/bin/env python3
"""构建守卫：捕捉「本地能跑、生产起不来」的注解前向引用。

背景（2026-09-30 真实事故）：`_presence_level(body: PresenceUpdate)` 被写在 `PresenceUpdate`
类**之前**。本地 venv 是 Python 3.14（PEP 649 起注解**惰性求值**），导入毫无问题；生产是
Python 3.11，注解在**函数定义时立即求值** → `NameError: name 'PresenceUpdate' is not defined`
→ 服务启动即崩、线上 502。语法检查（py_compile）也抓不到：函数注解是运行时求值，不是编译期。

所以这里做一件 py_compile 做不到的事：按**定义顺序**检查每个函数签名里引用的类型名，
凡是「引用了后面才定义（或从未定义）的名字」就报错。

用法: python3 deploy/check_annotations.py <文件或目录>...
退出码非 0 表示有问题（构建脚本据此中止）。
"""
import ast
import sys
from pathlib import Path


def defined_at(tree: ast.AST) -> dict[str, int]:
    """名字 → 最早定义行号（模块级：类/函数/赋值/导入）。"""
    out: dict[str, int] = {}

    def note(name: str, lineno: int) -> None:
        if name not in out or lineno < out[name]:
            out[name] = lineno

    for node in tree.body:                      # 只看模块级，函数体内的注解另论
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            note(node.name, node.lineno)
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    note(tgt.id, node.lineno)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                note((alias.asname or alias.name).split(".")[0], node.lineno)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            note(node.target.id, node.lineno)
    return out


def annotations_of(fn: ast.AST) -> list[ast.expr]:
    a = fn.args
    out = []
    for arg in [*a.posonlyargs, *a.args, *a.kwonlyargs]:
        if arg.annotation is not None:
            out.append(arg.annotation)
    if a.vararg is not None and a.vararg.annotation is not None:
        out.append(a.vararg.annotation)
    if a.kwarg is not None and a.kwarg.annotation is not None:
        out.append(a.kwarg.annotation)
    if fn.returns is not None:
        out.append(fn.returns)
    return out


def check(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    where = defined_at(tree)
    problems = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for ann in annotations_of(node):
            for sub in ast.walk(ann):
                if not isinstance(sub, ast.Name) or not sub.id[:1].isupper():
                    continue
                at = where.get(sub.id)
                if at is None or at > node.lineno:
                    problems.append(
                        f"{path}:{node.lineno} {node.name}() 的解注引用了 `{sub.id}`，"
                        f"但它定义在 {'第 ' + str(at) + ' 行' if at else '更晚/从未定义'}"
                        f"—— Python 3.11 会在此处 NameError"
                    )
    return problems


def main(argv: list[str]) -> int:
    targets: list[Path] = []
    for raw in argv:
        p = Path(raw)
        if p.is_dir():
            targets.extend(sorted(p.rglob("*.py")))
        elif p.exists():
            targets.append(p)
    targets = [t for t in targets if "__pycache__" not in t.parts]
    if not targets:
        print("check_annotations: 没有可检查的文件", file=sys.stderr)
        return 0
    problems = [msg for t in targets for msg in check(t)]
    if problems:
        print("注解前向引用检查未通过（生产是 Python 3.11，会在启动时崩）：", file=sys.stderr)
        for msg in problems:
            print("  ✗ " + msg, file=sys.stderr)
        return 1
    print(f"注解前向引用检查通过（{len(targets)} 个文件）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
