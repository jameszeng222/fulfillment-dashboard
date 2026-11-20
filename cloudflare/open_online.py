"""Open the Cloudflare site with a login link issued by the local Windows user."""
import argparse
import webbrowser

from app import cloud_login, core
from app.ops_gateway import site_origin


def prepare_account():
    with core.ops() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT active,password_hash FROM users WHERE username=?", (cloud_login.USERNAME,)).fetchone()
        if row:
            if not row["active"] or row["password_hash"] != cloud_login.LOCAL_ACCOUNT_MARKER:
                raise RuntimeError("同名账号已存在，请检查线上账号配置")
            return
        db.execute("""INSERT INTO users(username,password_hash,display_name,role,created_at)
                      VALUES (?,?,?,'admin',?)""",
                   (cloud_login.USERNAME, cloud_login.LOCAL_ACCOUNT_MARKER, "履约线上操作员", core.now_iso()))


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--prepare", action="store_true")
    args = cli.parse_args()
    if not site_origin():
        cli.error("线上站点地址未配置")
    prepare_account()
    if not args.prepare:
        target = f"{site_origin()}/login#cloud_ticket={cloud_login.issue_ticket()}"
        if not webbrowser.open(target):
            raise RuntimeError("无法打开默认浏览器")


if __name__ == "__main__":
    main()
