"""E2E 预检：逐项验证当前 config.yaml 所指环境的连通性，避免起服务后才发现配置问题。

凭证与地址一律读自 config.yaml（env 覆盖同样生效），本脚本不出现任何明文密钥；
对应配置未填时该项跳过（SKIP），不算失败。
"""
import base64
import os
import sys
from pathlib import Path

# 直接以 python e2e/preflight.py 运行时 sys.path[0]=e2e/，补仓库根以便 import app
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

RESULTS = []


def check(name: str, fn):
    try:
        detail = fn()
        if detail is None:
            RESULTS.append((name, "SKIP", "未配置"))
            print(f"[SKIP] {name}: 未配置")
        else:
            RESULTS.append((name, "OK", detail))
            print(f"[OK]   {name}: {detail}")
    except Exception as exc:  # noqa: BLE001
        RESULTS.append((name, "FAIL", str(exc)))
        print(f"[FAIL] {name}: {exc}")


def _config():
    from app.config import load_config

    return load_config(os.environ.get("ALARM_CONFIG"))


def check_nacos_db(cfg):
    from pymysql import connect

    source = cfg.analyse.db.nacos.config_db
    if not source.configured:
        return None
    host, port, database = source.as_pymysql_params()
    conn = connect(host=host, port=port, database=database, user=source.username,
                   password=source.password, charset="utf8mb4", connect_timeout=5, read_timeout=10)
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT DISTINCT tenant_id FROM config_info WHERE data_id = 'xizhao-system.yaml'")
            tenants = [row[0] for row in cursor.fetchall()]
            return f"xizhao-system.yaml tenant_id={tenants}"
    finally:
        conn.close()


def check_alarm_table(cfg):
    from pymysql import connect

    source = cfg.storage.database
    if not source.configured:
        return None
    host, port, database = source.as_pymysql_params()
    conn = connect(host=host, port=port, database=database, user=source.username,
                   password=source.password, charset="utf8mb4", connect_timeout=5, read_timeout=10)
    try:
        with conn.cursor() as cursor:
            cursor.execute("SHOW COLUMNS FROM alarm_analyze_result")
            cols = [row[0] for row in cursor.fetchall()]
            cursor.execute("SELECT COUNT(*), MAX(create_time) FROM alarm_analyze_result")
            count, latest = cursor.fetchone()
            missing = [c for c in ("problem_type", "fingerprint", "repeat_type", "root_id")
                       if c not in cols]
            return f"{database} columns={len(cols)} rows={count} latest={latest} 缺失列={missing or '无'}"
    finally:
        conn.close()


def check_redis(cfg):
    import redis

    if not cfg.storage.redis_url:
        return None
    client = redis.Redis.from_url(cfg.storage.redis_url, socket_timeout=5, decode_responses=True)
    pong = client.ping()
    db_size = client.dbsize()
    sample = client.get("alarm:analyse:msg:nonexistent-probe")  # 互操作性：读 Java 版 key 空间
    return f"ping={pong} dbsize={db_size} probe={sample}"


def check_gitlab(cfg):
    import httpx

    git = cfg.analyse.git
    if not git.api_ready:
        return None
    project_id = next(iter(git.projects.values()), None) or gitlab_search_first(git)
    if not project_id:
        raise RuntimeError("git.projects 为空且 search 定位不到项目")
    response = httpx.get(
        f"{git.base_url}/api/v4/projects/{project_id}/repository/branches",
        params={"per_page": "1"},
        headers={"PRIVATE-TOKEN": git.token}, timeout=10)
    branches = response.json() if response.status_code == 200 else []
    return f"status={response.status_code} branches采样={[b.get('name') for b in branches]}"


def gitlab_search_first(git):
    import httpx

    response = httpx.get(f"{git.base_url}/api/v4/projects", params={"per_page": "1"},
                         headers={"PRIVATE-TOKEN": git.token}, timeout=10)
    projects = response.json() if response.status_code == 200 else []
    return str(projects[0]["id"]) if projects else None


def check_gateway_read(cfg):
    import httpx

    external = cfg.analyse.external
    if not external.configured:
        return None
    workspace = external.resolve_workspace(next(iter(cfg.analyse.git.projects), None))
    response = httpx.get(
        external.url.rstrip("/") + "/code/file",
        params={"workspace": workspace, "path": "pom.xml",
                "ref": cfg.analyse.git.default_branch or "prod"},
        headers={"Authorization": f"Bearer {external.api_key}"}, timeout=10)
    body = response.json() if response.status_code == 200 else {}
    content = (body.get("data") or {}).get("content") or ""
    return f"status={response.status_code} workspace={workspace} content长度={len(content)}"


def check_llm(cfg):
    import httpx

    llm = cfg.analyse.llm
    if not (llm.enabled and llm.api_key):
        return None
    response = httpx.post(
        llm.endpoint,
        headers={"Authorization": f"Bearer {llm.api_key}"},
        json={"model": llm.resolved_model,
              "messages": [{"role": "user", "content": "回复两个字：在线"}],
              "max_tokens": 512},
        timeout=60)
    body = response.json()
    content = (body.get("choices") or [{}])[0].get("message", {}).get("content")
    return f"{llm.provider}/{llm.resolved_model} status={response.status_code} content={content!r}"


def check_feishu_token(cfg):
    import httpx

    feishu = cfg.feishu
    if not (feishu.app_id and feishu.app_secret):
        return None
    response = httpx.post(feishu.base_url + "/open-apis/auth/v3/tenant_access_token/internal",
                          json={"app_id": feishu.app_id, "app_secret": feishu.app_secret},
                          timeout=10)
    body = response.json()
    return f"code={body.get('code')} expire={body.get('expire')}s"


def main() -> int:
    cfg = _config()
    check("Nacos配置库(direct-db)", lambda: check_nacos_db(cfg))
    check("业务库.alarm_analyze_result", lambda: check_alarm_table(cfg))
    check("Redis", lambda: check_redis(cfg))
    check("GitLab API", lambda: check_gitlab(cfg))
    check("Agent网关读码", lambda: check_gateway_read(cfg))
    check("LLM", lambda: check_llm(cfg))
    check("飞书应用凭证", lambda: check_feishu_token(cfg))
    failed = [r for r in RESULTS if r[1] == "FAIL"]
    passed = sum(1 for r in RESULTS if r[1] == "OK")
    print(f"\n===> {passed}/{len(RESULTS)} 通过"
          f"（SKIP {sum(1 for r in RESULTS if r[1] == 'SKIP')}）")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
