# -*- coding: utf-8 -*-
"""KBase 备份/恢复工具（D 运维）。

lite 档（SQLite + Chroma + 本地文件）一条命令全量备份/恢复：
    python scripts/backup.py backup  --config config/kbase.yaml [--out backups]
    python scripts/backup.py restore --archive backups/kbase-20260715-093000.tar.gz --config config/kbase.yaml

设计要点：
- SQLite 用 sqlite3 的在线 backup API 拷快照——服务不停机也能拿到一致副本
  （普通 cp 可能截到写一半的页）；其余（chroma/files/uploads）直接打包。
  严格一致性要求高的场景（正在大批量导入时）仍建议停服备份。
- 恢复必须停服执行：解包覆盖 data_dir 后重启服务即可（迁移是幂等的，
  旧版本备份恢复到新版本代码上，启动时自动补列/建表）。
- standard 档（PG + Qdrant）此脚本只备文件目录，DB 与向量库用各自工具：
    pg_dump -Fc kbase > kbase.dump          # PostgreSQL
    恢复: pg_restore -d kbase --clean kbase.dump
    Qdrant: POST /collections/{name}/snapshots （官方快照 API，逐 collection）
  详细剧本见 docs/manual/运维手册.md。
"""
import argparse
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import time
from pathlib import Path

import yaml

SQLITE_NAME = "kbase.sqlite"


def _load_data_dir(config_path: str) -> Path:
    cfg = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
    data_dir = cfg.get("data_dir")
    if not data_dir:
        sys.exit(f"config 缺少 data_dir: {config_path}")
    p = Path(data_dir)
    # 相对路径相对于 config 所在目录的上一级（仓库根），与服务启动语义一致
    if not p.is_absolute():
        p = (Path(config_path).resolve().parent.parent / p).resolve()
    return p


def do_backup(config_path: str, out_dir: str) -> None:
    data_dir = _load_data_dir(config_path)
    if not data_dir.exists():
        sys.exit(f"data_dir 不存在: {data_dir}")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    archive = out / f"kbase-{stamp}.tar.gz"

    with tempfile.TemporaryDirectory() as tmp:
        snapshot = Path(tmp) / "data"
        snapshot.mkdir()
        # 1) SQLite 在线一致快照（服务运行中也安全）
        db_file = data_dir / SQLITE_NAME
        if db_file.exists():
            src = sqlite3.connect(str(db_file))
            dst = sqlite3.connect(str(snapshot / SQLITE_NAME))
            with dst:
                src.backup(dst)
            src.close()
            dst.close()
            print(f"[1/3] SQLite 快照完成: {SQLITE_NAME}")
        else:
            print(f"[1/3] 未见 {SQLITE_NAME}（standard 档 PG 请用 pg_dump，见脚本头注释）")
        # 2) 其余目录整拷（chroma 向量、原始文件、上传暂存等）
        for item in data_dir.iterdir():
            if item.name == SQLITE_NAME:
                continue
            target = snapshot / item.name
            if item.is_dir():
                shutil.copytree(item, target)
            else:
                shutil.copy2(item, target)
        print("[2/3] 数据目录拷贝完成")
        # 3) 打包
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(snapshot, arcname="data")
        print(f"[3/3] 备份完成: {archive} "
              f"({archive.stat().st_size / 1024 / 1024:.1f} MB)")


def do_restore(archive_path: str, config_path: str, yes: bool) -> None:
    data_dir = _load_data_dir(config_path)
    archive = Path(archive_path)
    if not archive.exists():
        sys.exit(f"备份包不存在: {archive}")
    print(f"即将用 {archive.name} 覆盖 {data_dir}")
    print("!! 恢复必须先停止 KBase 服务，否则运行中的连接会写坏恢复后的库")
    if not yes:
        answer = input("确认已停服并继续？输入 yes: ").strip().lower()
        if answer != "yes":
            sys.exit("已取消")
    # 先把现场挪到 .pre-restore 备份位，失败可回滚，不直接删
    if data_dir.exists():
        keep = data_dir.with_name(
            data_dir.name + f".pre-restore-{time.strftime('%Y%m%d-%H%M%S')}")
        data_dir.rename(keep)
        print(f"原 data_dir 已挪至: {keep}（确认恢复无误后可手动删除）")
    data_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(tmp, filter="data")
        shutil.move(str(Path(tmp) / "data"), str(data_dir))
    print(f"恢复完成: {data_dir}，重启 KBase 服务即可（迁移幂等自动补齐）")


def main() -> None:
    parser = argparse.ArgumentParser(description="KBase 备份/恢复（lite 档全量）")
    sub = parser.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backup", help="全量备份 data_dir 为 tar.gz")
    b.add_argument("--config", default="config/kbase.yaml")
    b.add_argument("--out", default="backups")
    r = sub.add_parser("restore", help="停服后从备份包恢复 data_dir")
    r.add_argument("--archive", required=True)
    r.add_argument("--config", default="config/kbase.yaml")
    r.add_argument("--yes", action="store_true", help="跳过交互确认（自动化用）")
    args = parser.parse_args()
    if args.cmd == "backup":
        do_backup(args.config, args.out)
    else:
        do_restore(args.archive, args.config, args.yes)


if __name__ == "__main__":
    main()
