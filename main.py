"""
北邮 UCloud 未完成作业查询 — AstrBot 插件

支持指令查询和定时推送。
参考 ucloud_homework_v2.py 的认证与 API 逻辑，改为异步实现。
"""

import re
from datetime import datetime
from urllib.parse import quote

import httpx

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star
from astrbot.api import logger

# ──────────────────── 常量 ────────────────────

CAS_LOGIN_URL = "https://auth.bupt.edu.cn/authserver/login"
Ucloud_SERVICE = "https://ucloud.bupt.edu.cn"
OAUTH_TOKEN_URL = "https://apiucloud.bupt.edu.cn/ykt-basics/oauth/token"
UNDONE_API_URL = "https://apiucloud.bupt.edu.cn/ykt-site/site/student/undone"
SITES_API_URL = "https://apiucloud.bupt.edu.cn/ykt-site/site/student/sites"

BASIC_AUTH = "Basic cG9ydGFsOnBvcnRhbF9zZWNyZXQ="  # portal:portal_secret

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0"
)


# ──────────────────── UCloud API ────────────────────


async def cas_login(client: httpx.AsyncClient, username: str, password: str) -> tuple[str, str]:
    """完成北邮 CAS 认证，返回 (access_token, user_id)"""

    login_url = f"{CAS_LOGIN_URL}?service={Ucloud_SERVICE}"
    resp = await client.get(login_url, headers={"User-Agent": UA}, follow_redirects=False)
    if resp.status_code != 200:
        raise RuntimeError(f"无法访问 CAS 登录页 (HTTP {resp.status_code})")

    html = resp.text

    # 检查是否需要验证码
    if re.search(r"config\.captcha[^{]*\{[^}]*id:\s*'(.*?)'", html):
        raise RuntimeError("CAS 登录需要验证码，暂不支持自动处理")

    # 提取 execution token
    exec_match = re.search(r'<input\s+name="execution"\s+value="(.*?)"', html)
    if not exec_match:
        raise RuntimeError("未找到 CAS execution token，页面结构可能已变更")
    execution = exec_match.group(1)

    # POST 登录表单
    form_data = {
        "username": username,
        "password": password,
        "submit": "登录",
        "type": "username_password",
        "execution": execution,
        "_eventId": "submit",
    }
    resp = await client.post(
        login_url,
        data=form_data,
        headers={
            "User-Agent": UA,
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": login_url,
        },
        follow_redirects=False,
    )

    # 从 302 重定向中提取 ticket
    if resp.status_code not in (302, 301):
        if "您提供的用户名或者密码有误" in resp.text:
            raise RuntimeError("用户名或密码错误")
        if "需要验证码" in resp.text:
            raise RuntimeError("CAS 登录需要验证码")
        raise RuntimeError(f"CAS 登录未返回重定向 (HTTP {resp.status_code})")

    location = resp.headers.get("Location", "")
    ticket_match = re.search(r"[?&]ticket=([^&]+)", location)
    if not ticket_match:
        raise RuntimeError(f"CAS 重定向中未找到 ticket，Location: {location}")
    ticket = ticket_match.group(1)

    # 用 ticket 换取 OAuth token
    token_resp = await client.post(
        OAUTH_TOKEN_URL,
        content=f"ticket={quote(ticket)}&grant_type=third",
        headers={
            "Authorization": BASIC_AUTH,
            "Content-Type": "application/x-www-form-urlencoded",
            "tenant-id": "000000",
            "Referer": "https://ucloud.bupt.edu.cn/",
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
        },
    )

    if token_resp.status_code != 200:
        raise RuntimeError(f"OAuth token 请求失败 (HTTP {token_resp.status_code}): {token_resp.text[:200]}")

    token_data = token_resp.json()
    access_token = token_data.get("access_token")
    if not access_token:
        raise RuntimeError(f"OAuth 响应中无 access_token: {token_data}")

    user_id = token_data.get("user_id", "")
    refresh_token = token_data.get("refresh_token", "")

    # 用 refresh_token 切换到学生角色
    roles = token_data.get("roles", [])
    student_role_id = None
    for role in roles:
        if role.get("roleAliase") == "学生" or role.get("roleName") == "学生":
            student_role_id = role.get("roleId") or role.get("id")
            break

    if student_role_id and refresh_token:
        refresh_resp = await client.post(
            OAUTH_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "identity": student_role_id,
            },
            headers={
                "Authorization": BASIC_AUTH,
                "Content-Type": "application/x-www-form-urlencoded",
                "tenant-id": "000000",
                "Referer": "https://ucloud.bupt.edu.cn/",
                "User-Agent": UA,
            },
        )
        if refresh_resp.status_code == 200:
            refresh_data = refresh_resp.json()
            access_token = refresh_data.get("access_token", access_token)
            user_id = refresh_data.get("user_id", user_id)

    return access_token, user_id


async def get_undone_homework(client: httpx.AsyncClient, access_token: str, user_id: str) -> list:
    """获取未完成作业列表"""
    resp = await client.get(
        UNDONE_API_URL,
        params={"userId": user_id},
        headers={
            "Authorization": BASIC_AUTH,
            "Blade-Auth": access_token,
            "tenant-id": "000000",
            "Referer": "https://ucloud.bupt.edu.cn/",
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
        },
    )

    if resp.status_code != 200:
        raise RuntimeError(f"作业查询失败 (HTTP {resp.status_code}): {resp.text[:200]}")

    result = resp.json()
    if not result.get("success"):
        raise RuntimeError(f"作业查询返回失败: {result.get('msg', '未知错误')}")

    return result.get("data", {}).get("undoneList", [])


async def get_sites(client: httpx.AsyncClient, access_token: str, user_id: str) -> dict:
    """获取课程站点列表，返回 siteId -> siteName 映射"""
    resp = await client.get(
        SITES_API_URL,
        params={"userId": user_id},
        headers={
            "Authorization": BASIC_AUTH,
            "Blade-Auth": access_token,
            "tenant-id": "000000",
            "Referer": "https://ucloud.bupt.edu.cn/",
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
        },
    )
    if resp.status_code != 200:
        raise RuntimeError(f"课程列表查询失败 (HTTP {resp.status_code}): {resp.text[:200]}")

    result = resp.json()
    sites = result.get("data", [])
    mapping = {}
    for site in sites:
        sid = site.get("siteId") or site.get("id")
        name = site.get("siteName") or site.get("name") or site.get("title") or ""
        if sid is not None:
            mapping[sid] = name
    return mapping


def format_remaining(end_time_str: str) -> str:
    """计算剩余时间并格式化"""
    try:
        end_dt = datetime.strptime(end_time_str, "%Y-%m-%d %H:%M:%S")
        delta = end_dt - datetime.now()

        if delta.total_seconds() <= 0:
            return "已截止"

        days = delta.days
        hours, remainder = divmod(delta.seconds, 3600)
        minutes, _ = divmod(remainder, 60)

        parts = []
        if days > 0:
            parts.append(f"{days}天")
        if hours > 0:
            parts.append(f"{hours}时")
        parts.append(f"{minutes}分")
        return "".join(parts)
    except (ValueError, TypeError):
        return "未知"


def build_homework_message(homework_list: list, site_map: dict | None = None) -> str:
    """将作业列表格式化为可读文本

    Args:
        homework_list: undone API 返回的作业列表
        site_map: siteId -> siteName 的映射，由 get_sites() 生成
    """
    if not homework_list:
        return "没有未完成的作业！"

    # 调试：输出 site_map 和首条作业的 siteId
    debug_line = ""
    if site_map:
        debug_line += f"\n[DEBUG] site_map 前3条: {dict(list(site_map.items())[:3])}"
    if homework_list:
        debug_line += f"\n[DEBUG] 首条 siteId: {homework_list[0].get('siteId')}"
        # 统计各 siteId 出现次数
        from collections import Counter
        id_counts = Counter(h.get("siteId") for h in homework_list)
        debug_line += f"\n[DEBUG] siteId 分布: {dict(id_counts)}\n"

    lines = [f"未完成作业 ({len(homework_list)} 项){debug_line}\n"]

    for i, h in enumerate(homework_list, 1):
        # 优先用 siteId 从映射中找课程名，否则尝试作业条目自带的 siteName
        site_id = h.get("siteId")
        course = ""
        if site_map and site_id in site_map:
            course = site_map[site_id]
        if not course:
            course = h.get("siteName", "")
        if not course:
            course = "未知课程"

        title = h.get("activityName", "未知作业")
        deadline = h.get("endTime", "未知")
        remain = format_remaining(deadline)

        if remain == "已截止":
            remain = "⚠已截止"

        lines.append(f"{i}. [{course}] {title}")
        lines.append(f"   截止: {deadline} (剩余 {remain})")

    return "\n".join(lines)


# ──────────────────── 插件 ────────────────────


class Main(Star):
    """北邮 UCloud 未完成作业查询插件"""

    def __init__(self, context: Context, config: dict) -> None:
        super().__init__(context)
        self.config = config
        self._cron_job_id: str | None = None

    async def initialize(self) -> None:
        """插件初始化：注册定时任务"""
        cron_expr = self.config.get("cron_expression", "")
        push_session = self.config.get("push_session", "")

        if cron_expr and push_session:
            try:
                job = await self.context.cron_manager.add_basic_job(
                    name="ucloud_homework_push",
                    cron_expression=cron_expr,
                    handler=self._cron_push_homework,
                    description="定时推送 UCloud 未完成作业",
                    timezone="Asia/Shanghai",
                    payload={"session": push_session},
                    enabled=True,
                    persistent=False,
                )
                self._cron_job_id = job.job_id
                logger.info(f"UCloud 作业定时推送已注册: cron={cron_expr}, session={push_session}")
            except Exception as e:
                logger.error(f"注册 UCloud 作业定时推送失败: {e}")

    async def terminate(self) -> None:
        """插件卸载：移除定时任务"""
        if self._cron_job_id:
            try:
                await self.context.cron_manager.delete_job(self._cron_job_id)
            except Exception:
                pass

    # ── 指令 ──

    @filter.command("homework")
    async def homework(self, event: AstrMessageEvent):
        """查询 UCloud 未完成作业"""
        username = self.config.get("username", "")
        password = self.config.get("password", "")

        if not username or not password:
            yield event.plain_result("请先在插件配置中填写学号和密码。")
            return

        try:
            async with httpx.AsyncClient(verify=True, timeout=30) as client:
                access_token, user_id = await cas_login(client, username, password)
                homework_list = await get_undone_homework(client, access_token, user_id)
                site_map = await get_sites(client, access_token, user_id)

            msg = build_homework_message(homework_list, site_map)
            yield event.plain_result(msg)

        except RuntimeError as e:
            logger.error(f"UCloud 作业查询失败: {e}")
            yield event.plain_result(f"查询失败: {e}")
        except Exception as e:
            logger.error(f"UCloud 作业查询异常: {e}")
            yield event.plain_result(f"查询出错: {e}")

    # ── 定时推送 ──

    async def _cron_push_homework(self, session: str = "") -> None:
        """定时任务回调：查询并推送作业"""
        if not session:
            logger.warning("UCloud 定时推送失败: 未配置 push_session")
            return

        username = self.config.get("username", "")
        password = self.config.get("password", "")

        if not username or not password:
            logger.warning("UCloud 定时推送失败: 未配置学号或密码")
            return

        try:
            async with httpx.AsyncClient(verify=True, timeout=30) as client:
                access_token, user_id = await cas_login(client, username, password)
                homework_list = await get_undone_homework(client, access_token, user_id)
                site_map = await get_sites(client, access_token, user_id)

            msg = build_homework_message(homework_list, site_map)
            message_chain = MessageChain().message(msg)
            await self.context.send_message(session, message_chain)
            logger.info("UCloud 作业定时推送成功")

        except Exception as e:
            logger.error(f"UCloud 定时推送失败: {e}")
