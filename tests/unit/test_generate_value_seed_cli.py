"""scripts/generate_value_seed.py CLI 冒烟测试（设计 §7 未列 CLI，评审补充）。

覆盖人工验证过的关键路径：
* 以「仓根进 sys.path 的裸 services」身份加载脚本 → market_value 里 scores 的
  ..db 相对导入断链 → _import_scores_via_plugin_package 兜底装配（加载即触发）；
* main() 成功路径：种子 SQL 落盘 + 摘要 + 引号包裹的 wrangler 命令；
* 失败路径退出码：库不存在 / 模型不可用 → 1 且 stderr 有中文原因。
"""

import importlib.util
import json
from pathlib import Path

from conftest import import_plugin

dao = import_plugin("db.dao")
migrations = import_plugin("db.migrations")
formula = import_plugin("services.formula")

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "generate_value_seed.py"
_spec = importlib.util.spec_from_file_location("generate_value_seed_cli", _SCRIPT)
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)


def make_db(path: Path, *, with_model: bool) -> None:
    """建文件库：迁移 + （可选）已发布 comp 曲线与角色统计 + 一名 ST 球员。"""
    conn = dao.connect(str(path))
    try:
        migrations.run_migrations(conn)
        if with_model:
            params = {k: formula.COMP_DEFAULT_PARAMS[k] for k in ("a", "b", "fb", "mf", "fw")}
            dao.upsert_model_params(conn, "comp", params, n=29, r2=0.7234,
                                    trained_at="2026-11-07T10:00:00+00:00", version="2026-11-07.1")
            dao.set_meta(conn, "model_role_stats_json", json.dumps({"FW": {"n": 3, "resid_sd": 0.35}}))
        conn.execute(
            "INSERT INTO players (id, name, fc_id, club_id, position, age, ca, pa, "
            "status, market_value, synced_at) VALUES (1, 'P1', 100, 1, 'ST', 25, 80, 80, "
            "'normal', NULL, '2026-11-07T00:00:00')",
        )
        conn.commit()
    finally:
        conn.close()


class TestGenerateValueSeedCli:
    def test_import_falls_back_to_plugin_package(self):
        """裸 services 身份加载 → mv.fair_spread 经插件包兜底导入成功（双导入路径之 CLI 侧）。"""
        assert cli.mv.__name__ == "services.market_value"  # 裸身份，非插件包身份
        assert callable(cli.mv.fair_spread)  # 相对导入断链，由 _import_scores_via_plugin_package 兜底

    def test_success_writes_seed_sql_and_quoted_command(self, tmp_path, capsys):
        db = tmp_path / "advisor.db"
        make_db(db, with_model=True)
        out = (tmp_path / "seed.sql").resolve()
        rc = cli.main([str(db), "-o", str(out)])
        captured = capsys.readouterr()
        assert rc == 0
        header = out.read_text(encoding="utf-8").splitlines()[0]
        assert header.startswith("-- whl model-value seed | ")
        assert "model=2026-11-07.1" in header
        assert "rows=1" in header
        assert "WHERE id = 1;" in out.read_text(encoding="utf-8")
        assert f'--file="{out.as_posix()}"' in captured.out  # 路径带引号，含空格也可直接复制执行

    def test_missing_db_exits_1(self, tmp_path, capsys):
        rc = cli.main([str(tmp_path / "nope.db"), "-o", str(tmp_path / "seed.sql")])
        captured = capsys.readouterr()
        assert rc == 1
        assert "数据库不存在" in captured.err

    def test_model_unavailable_exits_1(self, tmp_path, capsys):
        db = tmp_path / "advisor.db"
        make_db(db, with_model=False)  # 有表无已发布 comp → ModelUnavailable
        rc = cli.main([str(db), "-o", str(tmp_path / "seed.sql")])
        captured = capsys.readouterr()
        assert rc == 1
        assert "模型不可用" in captured.err
