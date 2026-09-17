"""按服务名从 Nacos 解析数据源配置。

告警的服务名即 Nacos 上的服务，其数据源配置就在 Nacos 配置中心里。两种获取方式：
- direct-db（默认，推荐）：直连 Nacos 配置库读 config_info 表，
  SQL LIKE 模糊搜 dataId 灵活、不依赖 Nacos 服务可用性，建议只读账号；
- open-api：Nacos HTTP 接口 /nacos/v1/cs/configs。

解析出的 YAML 兼容三种数据源写法：spring.datasource（单库）→ druid（含 master 从结构）
→ dynamic.datasource.master（多库取主库）。${conf.mysql.ip} 等占位符用公共配置
（xizhao-common.yaml）压平后的值替换。结果按服务缓存（默认 30 分钟，含负缓存）。
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from urllib.parse import quote
from urllib.request import urlopen

import pymysql
import yaml

from app.config import AppConfig, DbSourceConfig, NacosConfig
from app.core.errors import ConfigError

log = logging.getLogger(__name__)

CONFIG_API = "/nacos/v1/cs/configs"

# ${key} 占位符，如 ${conf.mysql.ip}
_PLACEHOLDER_PATTERN = re.compile(r"\$\{([^}]+)}")


@dataclass
class _CacheEntry:
    db_source: DbSourceConfig | None  # None = 负缓存：已解析过但无可用数据源
    resolved_at: float


class NacosConfigResolver:
    def __init__(self, config: AppConfig):
        self._nacos: NacosConfig = config.analyse.db.nacos
        self._cache: dict[str, _CacheEntry] = {}
        self._cache_lock = threading.Lock()

    def resolve_datasource(self, service: str | None) -> DbSourceConfig | None:
        """按服务名解析其数据库连接；服务配置不存在或未配数据源返回 None。"""
        nacos = self._nacos
        if not nacos.enabled or not service:
            return None
        with self._cache_lock:
            entry = self._cache.get(service)
            if entry is not None and not self._expired(entry):
                return entry.db_source
        db_source = self._do_resolve(service, nacos)
        with self._cache_lock:
            self._cache[service] = _CacheEntry(db_source, time.monotonic())
        return db_source

    def _expired(self, entry: _CacheEntry) -> bool:
        minutes = self._nacos.cache_minutes
        return minutes <= 0 or time.monotonic() - entry.resolved_at > minutes * 60

    # ------------------------------------------------------------------ 解析主流程

    def _do_resolve(self, service: str, nacos: NacosConfig) -> DbSourceConfig | None:
        namespace = nacos.namespace
        common_props = self._fetch_common_props(namespace, nacos)

        # dataId 模板多候选，逗号分隔依次尝试
        for pattern in (nacos.data_id_pattern or "").split(","):
            data_id = pattern.strip().replace("{service}", service)
            db_source = self._try_resolve(namespace, nacos, data_id, common_props)
            if db_source is not None:
                return db_source

        # 模板未命中：按服务名模糊搜配置兜底。
        # 仓库命名（ccd-java-<X>）与 dataId 命名（xizhao-<X>/ccd-<X>）不一致，
        # 全名搜不到时剥掉已知前缀用业务核心词再搜。
        for keyword in self._search_keywords(service):
            for data_id in self._search_data_ids(namespace, nacos, keyword):
                db_source = self._try_resolve(namespace, nacos, data_id, common_props)
                if db_source is not None:
                    return db_source

        log.warning("Nacos 未找到服务数据源配置, service=%s, pattern=%s", service, nacos.data_id_pattern)
        return None

    @staticmethod
    def _search_keywords(service: str) -> list[str]:
        keywords = [service]
        stripped = service
        for prefix in ("ccd-java-", "xizhao-platform-", "xizhao-", "ccd-", "platform-"):
            if stripped.startswith(prefix):
                stripped = stripped[len(prefix):]
        if stripped and stripped not in keywords:
            keywords.append(stripped)
        return keywords

    def _try_resolve(self, namespace: str, nacos: NacosConfig, data_id: str,
                     common_props: dict) -> DbSourceConfig | None:
        content = self._fetch_config(namespace, nacos, data_id)
        if not content:
            return None
        db_source = self._parse_datasource(content, common_props)
        if db_source is not None:
            log.info("Nacos 数据源解析成功, dataId=%s", data_id)
        else:
            log.info("Nacos 配置已拉取但未解析出数据源, dataId=%s", data_id)
        return db_source

    # ------------------------------------------------------------------ 配置获取：direct-db / open-api 双通道

    def _fetch_config(self, namespace: str, nacos: NacosConfig, data_id: str) -> str | None:
        if nacos.direct_db:
            content = self._fetch_config_from_db(namespace, nacos, data_id)
            if content is not None:
                return content
        return self._fetch_config_from_api(namespace, nacos, data_id)

    def _fetch_config_from_db(self, namespace: str, nacos: NacosConfig, data_id: str) -> str | None:
        """直连 Nacos 配置库读 config_info 表（每次短连接：低频调用，免维护长连接失效问题）。"""
        try:
            host, port, database = nacos.config_db.as_pymysql_params()
            timeout_s = max(1, nacos.timeout_ms // 1000)
            connection = pymysql.connect(
                host=host, port=port, database=database,
                user=nacos.config_db.username, password=nacos.config_db.password,
                charset="utf8mb4", connect_timeout=timeout_s, read_timeout=timeout_s,
            )
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT content FROM config_info WHERE data_id = %s AND tenant_id = %s",
                        (data_id, namespace or ""),
                    )
                    row = cursor.fetchone()
                    return row[0] if row else None
            finally:
                connection.close()
        except ConfigError as exc:
            log.error("Nacos 配置库 URL 非法，direct-db 通道不可用: %s", exc)
            return None
        except Exception as exc:  # noqa: BLE001 - 连接/查询失败转 open-api 通道
            log.warning("Nacos 配置库读取失败，转 open-api, dataId=%s, err=%s", data_id, exc)
            return None

    def _fetch_config_from_api(self, namespace: str, nacos: NacosConfig, data_id: str) -> str | None:
        server_addr = nacos.server_addr
        if not server_addr:
            log.warning("open-api 通道缺少 server_addr（analyse.db.nacos.server-addr）")
            return None
        url = (server_addr if server_addr.startswith("http") else "http://" + server_addr) + CONFIG_API
        url += f"?dataId={quote(data_id)}&group={quote(nacos.group or 'DEFAULT_GROUP')}"
        if namespace:
            url += f"&tenant={quote(namespace)}"
        if nacos.username:
            url += f"&username={quote(nacos.username)}&password={quote(nacos.password)}"
        try:
            with urlopen(url, timeout=max(1, nacos.timeout_ms // 1000)) as resp:
                body = resp.read().decode("utf-8")
                # Nacos 配置不存在时返回 404 status 或空 body
                return body if body else None
        except Exception as exc:  # noqa: BLE001
            log.warning("Nacos 配置拉取异常, dataId=%s, err=%s", data_id, exc)
            return None

    def _search_data_ids(self, namespace: str, nacos: NacosConfig, keyword: str) -> list[str]:
        if nacos.direct_db:
            data_ids = self._search_data_ids_from_db(namespace, nacos, keyword)
            if data_ids:
                return data_ids
        return self._search_data_ids_from_api(namespace, nacos, keyword)

    def _search_data_ids_from_db(self, namespace: str, nacos: NacosConfig, keyword: str) -> list[str]:
        data_ids: list[str] = []
        try:
            host, port, database = nacos.config_db.as_pymysql_params()
            timeout_s = max(1, nacos.timeout_ms // 1000)
            connection = pymysql.connect(
                host=host, port=port, database=database,
                user=nacos.config_db.username, password=nacos.config_db.password,
                charset="utf8mb4", connect_timeout=timeout_s, read_timeout=timeout_s,
            )
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT data_id FROM config_info WHERE tenant_id = %s AND data_id LIKE %s LIMIT 20",
                        (namespace or "", f"%{keyword}%"),
                    )
                    data_ids = [row[0] for row in cursor.fetchall()]
            finally:
                connection.close()
        except Exception as exc:  # noqa: BLE001
            log.warning("Nacos 配置库 dataId 搜索失败, keyword=%s, err=%s", keyword, exc)
        return data_ids

    def _search_data_ids_from_api(self, namespace: str, nacos: NacosConfig, keyword: str) -> list[str]:
        data_ids: list[str] = []
        server_addr = nacos.server_addr
        if not server_addr:
            return data_ids
        url = (server_addr if server_addr.startswith("http") else "http://" + server_addr)
        url += f"{CONFIG_API}?search=blur&dataId={quote('*' + keyword + '*')}&group=&pageNo=1&pageSize=20"
        if namespace:
            url += f"&tenant={quote(namespace)}"
        try:
            with urlopen(url, timeout=max(1, nacos.timeout_ms // 1000)) as resp:
                page = yaml.safe_load(resp.read().decode("utf-8"))
            items = (page or {}).get("pageItems") or []
            for item in items:
                data_id = (item or {}).get("dataId")
                if data_id and str(data_id).endswith(".yaml") and keyword.lower() in str(data_id).lower():
                    data_ids.append(str(data_id))
        except Exception as exc:  # noqa: BLE001
            log.warning("Nacos 配置 search 失败, keyword=%s, err=%s", keyword, exc)
        return data_ids

    # ------------------------------------------------------------------ YAML 解析与占位符替换

    def _fetch_common_props(self, namespace: str, nacos: NacosConfig) -> dict:
        """拉取公共配置并压平成 key 路径 → value（conf.mysql.ip → 10.x.x.x）。"""
        flat: dict = {}
        for common_data_id in (nacos.common_data_ids or "").split(","):
            common_data_id = common_data_id.strip()
            if not common_data_id:
                continue
            content = self._fetch_config(namespace, nacos, common_data_id)
            if not content:
                continue
            try:
                loaded = yaml.safe_load(content)
                if isinstance(loaded, dict):
                    _flatten("", loaded, flat)
            except yaml.YAMLError as exc:
                log.warning("公共配置解析失败, dataId=%s, err=%s", common_data_id, exc)
        return flat

    @staticmethod
    def _parse_datasource(config_yaml: str, common_props: dict) -> DbSourceConfig | None:
        try:
            root = yaml.safe_load(config_yaml)
        except yaml.YAMLError as exc:
            log.warning("Nacos 配置 YAML 解析失败: %s", exc)
            return None
        if not isinstance(root, dict):
            return None
        spring = root.get("spring")
        if not isinstance(spring, dict):
            return None
        datasource = spring.get("datasource")
        if not isinstance(datasource, dict):
            return None

        # 查找顺序：单库 → druid → druid.master（主从结构 url 在 master 下）→ dynamic.master（多库取主库）
        candidates: list[dict] = [datasource]
        druid = datasource.get("druid")
        if isinstance(druid, dict):
            candidates.append(druid)
            if isinstance(druid.get("master"), dict):
                candidates.append(druid["master"])
        dynamic = datasource.get("dynamic")
        if isinstance(dynamic, dict) and isinstance(dynamic.get("datasource"), dict):
            master = dynamic["datasource"].get("master")
            if isinstance(master, dict):
                candidates.append(master)

        for props in candidates:
            url = props.get("url") or props.get("jdbc-url")
            if not (isinstance(url, str) and url.strip()):
                continue
            source = DbSourceConfig(url=_resolve_placeholders(url, common_props))
            username = props.get("username")
            password = props.get("password")
            source.username = _resolve_placeholders(str(username), common_props) if username is not None else ""
            source.password = _resolve_placeholders(str(password), common_props) if password is not None else ""
            return source
        return None


def _flatten(prefix: str, node: dict, out: dict) -> None:
    for key, value in node.items():
        full_key = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            _flatten(full_key, value, out)
        elif value is not None:
            out[full_key] = value


def _resolve_placeholders(text: str, common_props: dict) -> str:
    """递归替换 ${key}（key 在公共配置里存在才替换，避免破坏普通 ${...} 字符串）。"""
    if not text or not common_props or "${" not in text:
        return text

    def _replace(match: re.Match) -> str:
        value = common_props.get(match.group(1))
        return str(value) if value is not None else match.group(0)

    return _PLACEHOLDER_PATTERN.sub(_replace, text)
