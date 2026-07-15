"""备份/恢复脚本（D 运维）：备份产出 tar.gz、恢复还原数据且旧现场保留
为 .pre-restore-*（可回滚）。"""
import sqlite3

import yaml

from scripts.backup import do_backup, do_restore


def _make_env(tmp_path):
    """仿真仓库布局：config/kbase.yaml + data_dir（sqlite + files/）。"""
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    data = repo / "data"
    (data / "files").mkdir(parents=True)
    (data / "files" / "a.pdf").write_bytes(b"%PDF fake")
    db = sqlite3.connect(str(data / "kbase.sqlite"))
    db.execute("CREATE TABLE t (v TEXT)")
    db.execute("INSERT INTO t VALUES ('original')")
    db.commit()
    db.close()
    cfg = repo / "config" / "kbase.yaml"
    cfg.write_text(yaml.safe_dump({"data_dir": str(data)}), encoding="utf-8")
    return repo, data, cfg


def test_backup_then_restore_roundtrip(tmp_path):
    repo, data, cfg = _make_env(tmp_path)
    out = tmp_path / "backups"

    do_backup(str(cfg), str(out))
    archives = list(out.glob("kbase-*.tar.gz"))
    assert len(archives) == 1 and archives[0].stat().st_size > 0

    # 篡改现场：改 DB、删文件——模拟事故
    db = sqlite3.connect(str(data / "kbase.sqlite"))
    db.execute("UPDATE t SET v='corrupted'")
    db.commit()
    db.close()
    (data / "files" / "a.pdf").unlink()

    do_restore(str(archives[0]), str(cfg), yes=True)

    # 数据还原
    db = sqlite3.connect(str(data / "kbase.sqlite"))
    assert db.execute("SELECT v FROM t").fetchone()[0] == "original"
    db.close()
    assert (data / "files" / "a.pdf").read_bytes() == b"%PDF fake"
    # 旧现场保留可回滚
    pre = list(data.parent.glob("data.pre-restore-*"))
    assert len(pre) == 1
    db = sqlite3.connect(str(pre[0] / "kbase.sqlite"))
    assert db.execute("SELECT v FROM t").fetchone()[0] == "corrupted"
    db.close()
