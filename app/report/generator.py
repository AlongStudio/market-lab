"""每日静态 HTML 报告生成。

把当日统计快照序列化为 JSON 内嵌进自包含 HTML,写到 REPORTS_DIR(NAS 挂载目录),
外网经 NAS 文件服务下载离线查询。同时更新 latest.html。
"""
import json
import logging
from datetime import date
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy.orm import Session

from app.config import settings
from app.report.snapshot import build_snapshot

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=select_autoescape(["html"]),
)


def generate_report(db: Session) -> Path:
    """生成当日报告 HTML,返回写入的文件路径(日期文件)。"""
    snap = build_snapshot(db)
    data_json = json.dumps(snap, ensure_ascii=False)
    today = date.today()
    html = _env.get_template("report.html").render(
        report_date=today.isoformat(),
        data_json=data_json,
    )

    out_dir = Path(settings.REPORTS_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    dated = out_dir / f"market-lab-{today.strftime('%Y%m%d')}.html"
    dated.write_text(html, encoding="utf-8")
    (out_dir / "latest.html").write_text(html, encoding="utf-8")
    logger.info("report written: %s", dated)
    return dated
