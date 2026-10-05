"""AstrBot 待办事项与提醒插件。

本版本重点：
- 统一“列表序号”和“永久 ID”解析：1 表示当前列表第 1 项，#12 表示永久 ID 12。
- 修复 /提醒 编号 时间：提醒绑定到指定待办，不再覆盖待办数据。
- 管理面板仅允许配置中的管理员 QQ 号唤出。
- 群聊菜单支持文字 / 图片 / 文字+图片三种发送方式。
- 使用 AstrBot 官方的 unified_msg_origin 做会话隔离，支持主动提醒。
- 使用 data/plugin_data 保存数据，并对 JSON 写入做原子替换。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from html import escape
from typing import Any, Dict, Iterable, List, Optional, Tuple

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register

try:
    from astrbot.core.config.astrbot_config import AstrBotConfig
except Exception:  # pragma: no cover - 兼容部分旧版本
    from astrbot.api import AstrBotConfig  # type: ignore


PLUGIN_ID = "astrbot_plugin_one_agent"
DATA_VERSION = 2
PRIORITY_ICONS = {"高": "🔴", "中": "🟡", "低": "🟢"}
PRIORITY_ORDER = {"高": 0, "中": 1, "低": 2}
VALID_PRIORITIES = {"高", "中", "低"}
VALID_SEND_MODES = {"text", "image", "both"}


def _resolve_data_dir() -> str:
    try:
        from astrbot.api.star import StarTools

        path = str(StarTools.get_data_dir(PLUGIN_ID))
        os.makedirs(path, exist_ok=True)
        return path
    except Exception as exc:  # pragma: no cover - 仅兼容回退
        logger.warning("[todo] StarTools 不可用，回退默认数据目录: %s", exc)
        path = os.path.join("/AstrBot/data/plugin_data", PLUGIN_ID)
        os.makedirs(path, exist_ok=True)
        return path


@dataclass
class TodoItem:
    content: str
    priority: str = "中"
    creator: str = ""
    assigned_to: str = ""
    created_at: str = field(default_factory=lambda: dt.datetime.now().isoformat(timespec="seconds"))
    completed: bool = False
    completed_at: Optional[str] = None
    id: int = 0
    reminder_at: Optional[str] = None
    reminder_origin: Optional[str] = None
    reminder_text: Optional[str] = None

    def __post_init__(self) -> None:
        if self.priority not in VALID_PRIORITIES:
            self.priority = "中"
        self.content = self.content.strip()

    @property
    def reminder_set(self) -> bool:
        return bool(self.reminder_at)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "content": self.content,
            "priority": self.priority,
            "creator": self.creator,
            "assigned_to": self.assigned_to,
            "created_at": self.created_at,
            "completed": self.completed,
            "completed_at": self.completed_at,
            "reminder_at": self.reminder_at,
            "reminder_origin": self.reminder_origin,
            "reminder_text": self.reminder_text,
            # 保留旧字段，方便旧数据/外部工具读取。
            "reminder_set": self.reminder_set,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TodoItem":
        return cls(
            content=str(data.get("content", "")),
            priority=str(data.get("priority", "中")),
            creator=str(data.get("creator", "")),
            assigned_to=str(data.get("assigned_to", "")),
            created_at=str(data.get("created_at") or dt.datetime.now().isoformat(timespec="seconds")),
            completed=bool(data.get("completed", False)),
            completed_at=data.get("completed_at"),
            id=int(data.get("id", 0) or 0),
            reminder_at=data.get("reminder_at"),
            reminder_origin=data.get("reminder_origin"),
            reminder_text=data.get("reminder_text"),
        )


def _now() -> dt.datetime:
    return dt.datetime.now()


def _parse_time_only(value: str) -> Optional[Tuple[int, int]]:
    value = value.strip().lower()
    value = value.replace("：", ":")
    m = re.fullmatch(r"(\d{1,2})(?::(\d{1,2}))?", value)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
        return None

    # 8点 / 8点30分 / 晚上8点半 / 下午 2:30
    value = re.sub(r"\s+", "", value)
    pm = False
    if value.startswith(("晚上", "下午", "傍晚")):
        pm = True
        value = re.sub(r"^(晚上|下午|傍晚)", "", value)
    elif value.startswith("中午"):
        pm = True
        value = value[2:]
    elif value.startswith("早上"):
        value = value[2:]
    elif value.startswith("凌晨"):
        value = value[2:]

    m = re.fullmatch(r"(\d{1,2})点(?:(\d{1,2})(?:分)?)?(半)?", value)
    if not m:
        return None
    hour = int(m.group(1))
    minute = 30 if m.group(3) else int(m.group(2) or 0)
    if pm and hour < 12:
        hour += 12
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return hour, minute
    return None


def parse_reminder_datetime(raw: str, now: Optional[dt.datetime] = None) -> Optional[dt.datetime]:
    """解析常见提醒时间。

    支持：18:30、18点30分、晚上8点、今天 18:00、明天 08:00、2026-10-06 18:00。
    仅给时间时，如果该时间已过去，则默认顺延到次日。
    """
    text = raw.strip().replace("T", " ")
    if not text:
        return None
    now = now or _now()

    if text.startswith("明天"):
        parsed = _parse_time_only(text[2:].strip())
        if not parsed:
            return None
        hour, minute = parsed
        return (now + dt.timedelta(days=1)).replace(hour=hour, minute=minute, second=0, microsecond=0)

    if text.startswith("今天"):
        parsed = _parse_time_only(text[2:].strip())
        if not parsed:
            return None
        hour, minute = parsed
        return now.replace(hour=hour, minute=minute, second=0, microsecond=0)

    # yyyy-mm-dd HH:MM / yyyy/mm/dd HH:MM
    m = re.fullmatch(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})\s+(.+)", text)
    if m:
        parsed = _parse_time_only(m.group(4))
        if not parsed:
            return None
        try:
            return dt.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), parsed[0], parsed[1])
        except ValueError:
            return None

    # mm-dd HH:MM
    m = re.fullmatch(r"(\d{1,2})[-/](\d{1,2})\s+(.+)", text)
    if m:
        parsed = _parse_time_only(m.group(3))
        if not parsed:
            return None
        try:
            candidate = dt.datetime(now.year, int(m.group(1)), int(m.group(2)), parsed[0], parsed[1])
        except ValueError:
            return None
        if candidate <= now:
            candidate = candidate.replace(year=now.year + 1)
        return candidate

    parsed = _parse_time_only(text)
    if not parsed:
        return None
    hour, minute = parsed
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += dt.timedelta(days=1)
    return candidate


@register(
    PLUGIN_ID,
    "one_agent",
    "待办事项、提醒、图片菜单与管理员控制中心",
    "2.2.0",
    "https://github.com/ysgl870-web/astrbot_plugin_one_agent",
)
class TodoManager(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config
        self.data_dir = _resolve_data_dir()
        self.data_file = os.path.join(self.data_dir, "todo_data.json")
        self.backup_file = os.path.join(self.data_dir, "todo_data.corrupt.json")
        self.data: Dict[str, Any] = self._load_data()
        self._clear_confirmations: Dict[str, float] = {}
        self._scheduler_task: Optional[asyncio.Task[Any]] = None
        try:
            self._scheduler_task = asyncio.create_task(self._reminder_loop())
        except RuntimeError:
            logger.warning("[todo] 当前没有可用事件循环，提醒调度器将在插件重载后启动。")

    # ---------- 配置 ----------
    def cfg(self, key: str, default: Any = None) -> Any:
        try:
            value = self.config.get(key, default)
        except Exception:
            value = getattr(self.config, key, default)
        return default if value is None else value

    def is_enabled(self) -> bool:
        return bool(self.cfg("enabled", True))

    def admin_ids(self) -> set[str]:
        values = self.cfg("admin_qq_ids", [])
        if isinstance(values, str):
            values = re.split(r"[,，\s]+", values)
        result = set()
        for item in values or []:
            normalized = re.sub(r"[^0-9]", "", str(item))
            if normalized:
                result.add(normalized)
        return result

    def is_admin(self, event: AstrMessageEvent) -> bool:
        sender = re.sub(r"[^0-9]", "", str(event.get_sender_id() or ""))
        return sender in self.admin_ids()

    def _admin_denied_message(self, event: AstrMessageEvent) -> Optional[str]:
        if not self.is_admin(event):
            return "⛔ 你没有管理员权限。\n请在 AstrBot 插件配置的“管理员 QQ 号”中添加你的 QQ 号。"
        return None

    def admin_required(self, event: AstrMessageEvent) -> Optional[str]:
        if not bool(self.cfg("admin_only_panel", True)):
            return None
        return self._admin_denied_message(event)

    # ---------- 数据 ----------
    def _empty_data(self) -> Dict[str, Any]:
        return {"version": DATA_VERSION, "sessions": {}}

    def _load_data(self) -> Dict[str, Any]:
        if not os.path.exists(self.data_file):
            return self._empty_data()
        try:
            with open(self.data_file, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            if isinstance(raw, dict) and isinstance(raw.get("sessions"), dict):
                raw.setdefault("version", DATA_VERSION)
                return raw
            # 兼容旧版本：旧格式为 {group_id: [todo, ...]}
            if isinstance(raw, dict):
                return {"version": DATA_VERSION, "sessions": {"legacy:" + str(k): v for k, v in raw.items() if isinstance(v, list)}}
        except Exception as exc:
            logger.error("[todo] 数据读取失败: %s", exc)
            try:
                with open(self.data_file, "rb") as src, open(self.backup_file, "wb") as dst:
                    dst.write(src.read())
                logger.warning("[todo] 已备份损坏数据到 %s", self.backup_file)
            except Exception:
                pass
        return self._empty_data()

    def _save_data(self) -> None:
        payload = json.dumps(self.data, ensure_ascii=False, indent=2)
        directory = os.path.dirname(self.data_file)
        fd, temp_path = tempfile.mkstemp(prefix=".todo-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(temp_path, self.data_file)
        except Exception as exc:
            logger.error("[todo] 数据保存失败: %s", exc)
            try:
                os.remove(temp_path)
            except OSError:
                pass

    def _session_key(self, event: AstrMessageEvent) -> str:
        try:
            umo = str(event.unified_msg_origin)
            if umo:
                return "session:" + umo
        except Exception:
            pass
        try:
            group = str(event.get_group_id() or "")
            if group:
                return "group:" + group
        except Exception:
            pass
        return "private:" + str(event.get_sender_id() or "unknown")

    def _legacy_key(self, event: AstrMessageEvent) -> Optional[str]:
        try:
            group = str(event.get_group_id() or "")
            return "legacy:" + group if group else None
        except Exception:
            return None

    def _load_session_raw(self, session_key: str, event: Optional[AstrMessageEvent] = None) -> List[Dict[str, Any]]:
        sessions = self.data.setdefault("sessions", {})
        raw = sessions.get(session_key)
        if isinstance(raw, list):
            return raw
        if event is not None:
            legacy = self._legacy_key(event)
            if legacy and isinstance(sessions.get(legacy), list):
                return sessions[legacy]
        return []

    def _get_todos(self, event: AstrMessageEvent) -> List[TodoItem]:
        key = self._session_key(event)
        raw = self._load_session_raw(key, event)
        todos: List[TodoItem] = []
        for item in raw:
            if isinstance(item, dict) and item.get("content") is not None:
                try:
                    todos.append(TodoItem.from_dict(item))
                except Exception as exc:
                    logger.warning("[todo] 跳过无效待办：%s", exc)
        return todos

    def _set_todos(self, event: AstrMessageEvent, todos: Iterable[TodoItem]) -> None:
        key = self._session_key(event)
        sessions = self.data.setdefault("sessions", {})
        sessions[key] = [item.to_dict() for item in todos]
        legacy = self._legacy_key(event)
        if legacy and legacy != key:
            sessions.pop(legacy, None)
        self._save_data()

    def _next_id(self, todos: List[TodoItem]) -> int:
        return max((item.id for item in todos), default=0) + 1

    def _sorted(self, todos: List[TodoItem]) -> List[TodoItem]:
        return sorted(todos, key=lambda x: (x.completed, PRIORITY_ORDER.get(x.priority, 1), x.created_at, x.id))

    def _resolve_item(self, todos: List[TodoItem], token: str) -> Tuple[Optional[TodoItem], str]:
        value = token.strip()
        if not value:
            return None, "请提供编号，例如：完成 1；精确 ID 可写成：完成 #12。"
        display = self._sorted(todos)
        if value.startswith("#"):
            try:
                wanted = int(value[1:])
            except ValueError:
                return None, "编号格式错误，例如：完成 #12。"
            for item in todos:
                if item.id == wanted:
                    return item, ""
            return None, f"没有找到永久 ID #{wanted}。"
        try:
            position = int(value)
        except ValueError:
            return None, "编号必须是数字，例如：完成 1。"
        if position < 1 or position > len(display):
            return None, f"列表中没有第 {position} 项。"
        return display[position - 1], ""

    def _format_item(self, item: TodoItem, position: Optional[int] = None) -> str:
        index = f"{position}. " if position is not None else ""
        status = "✅" if item.completed else "⬜"
        reminder = f" · ⏰ {self._format_dt(item.reminder_at)}" if item.reminder_at else ""
        return f"{index}{status} {PRIORITY_ICONS.get(item.priority, '⚪')} {item.content}  #{item.id}{reminder}"

    @staticmethod
    def _format_dt(value: Optional[str]) -> str:
        if not value:
            return ""
        try:
            return dt.datetime.fromisoformat(value).strftime("%m-%d %H:%M")
        except Exception:
            return value

    # ---------- 菜单 ----------
    def _menu_text(self, event: AstrMessageEvent) -> str:
        todos = self._sorted(self._get_todos(event))
        pending = sum(not item.completed for item in todos)
        done = len(todos) - pending
        title = str(self.cfg("menu_title", "📋 待办管理中心"))
        lines = [
            title,
            "━━━━━━━━━━━━━━━━",
            f"📊 进度：{done}/{len(todos)} 已完成" if todos else "📊 当前暂无待办",
            "",
            "📝 日常操作",
            "• 待办 内容 [高/中/低]   添加任务",
            "• 待办列表              查看任务",
            "• 完成 编号              标记完成",
            "• 删除 编号              删除任务",
            "• 提醒 编号 时间        设置提醒",
            "• 取消提醒 编号          取消提醒",
            "• 今日待办              查看今日",
            "• 完成率                查看统计",
            "",
            "🧩 更多",
            "• 自助代办              自动分配",
            "• 管理面板              管理员专用",
            "",
            "编号说明：1 表示当前列表第 1 项；#12 表示永久 ID 12。",
        ]
        if bool(self.cfg("menu_include_summary", True)) and todos:
            lines.extend(["", "📌 当前待办（前 8 项）"])
            for idx, item in enumerate(todos[:8], 1):
                lines.append(self._format_item(item, idx))
            if len(todos) > 8:
                lines.append(f"… 共 {len(todos)} 项，使用“待办列表”查看全部")
        return "\n".join(lines)

    async def _menu_image(self, event: AstrMessageEvent) -> Optional[Any]:
        todos = self._sorted(self._get_todos(event))
        pending = sum(not item.completed for item in todos)
        done = len(todos) - pending
        cards = []
        for idx, item in enumerate(todos[:10], 1):
            status = "已完成" if item.completed else "进行中"
            reminder = f"　⏰ {self._format_dt(item.reminder_at)}" if item.reminder_at else ""
            cards.append({
                "index": idx,
                "status": status,
                "priority": item.priority,
                "content": escape(item.content),
                "id": item.id,
                "reminder": reminder,
            })
        title = escape(str(self.cfg("menu_title", "待办管理中心")))
        html = f"""
        <div style="width:760px;padding:28px 34px;font-family:Arial,'Microsoft YaHei',sans-serif;background:#f7f8fc;color:#202431;box-sizing:border-box;">
          <div style="background:linear-gradient(135deg,#202735,#3d4658);color:#fff;border-radius:22px;padding:28px 30px;margin-bottom:18px;">
            <div style="font-size:34px;font-weight:800;margin-bottom:8px;">{title}</div>
            <div style="font-size:18px;opacity:.86;">清晰、简洁、适合群聊阅读的任务面板</div>
            <div style="display:flex;gap:12px;margin-top:20px;">
              <span style="background:rgba(255,255,255,.14);padding:9px 14px;border-radius:12px;">全部 {len(todos)}</span>
              <span style="background:rgba(255,255,255,.14);padding:9px 14px;border-radius:12px;">完成 {done}</span>
              <span style="background:rgba(255,255,255,.14);padding:9px 14px;border-radius:12px;">进行中 {pending}</span>
            </div>
          </div>
          <div style="background:#fff;border-radius:20px;padding:20px 22px;margin-bottom:16px;border:1px solid #eceef4;">
            <div style="font-size:21px;font-weight:750;margin-bottom:12px;">常用指令</div>
            <div style="font-size:17px;line-height:1.75;">
              <b>待办 内容</b>　添加任务　　<b>待办列表</b>　查看任务<br>
              <b>完成 编号</b>　标记完成　　<b>删除 编号</b>　删除任务<br>
              <b>提醒 编号 时间</b>　设置提醒　　<b>取消提醒 编号</b>　取消提醒
            </div>
          </div>
        """
        if cards:
            html += '<div style="background:#fff;border-radius:20px;padding:20px 22px;border:1px solid #eceef4;">'
            html += '<div style="font-size:21px;font-weight:750;margin-bottom:14px;">当前待办</div>'
            for card in cards:
                badge_bg = {"高": "#fff0f0", "中": "#fff8e8", "低": "#eefaf1"}.get(card["priority"], "#f4f5f7")
                html += f'''
                <div style="display:flex;align-items:center;gap:12px;padding:12px 0;border-bottom:1px solid #f0f1f5;">
                  <div style="width:34px;height:34px;border-radius:10px;background:#f1f2f6;display:flex;align-items:center;justify-content:center;font-weight:700;">{card["index"]}</div>
                  <div style="flex:1;font-size:18px;">
                    <div style="font-weight:650;">{card["content"]}</div>
                    <div style="font-size:14px;color:#8a909d;margin-top:4px;">#{card["id"]} · {card["status"]}{card["reminder"]}</div>
                  </div>
                  <div style="background:{badge_bg};padding:6px 10px;border-radius:10px;font-size:14px;">{card["priority"]}优先级</div>
                </div>'''
            if len(todos) > 10:
                html += '<div style="padding-top:12px;color:#8a909d;font-size:14px;">仅显示前 10 项，使用“待办列表”查看全部</div>'
            html += '</div>'
        else:
            html += '<div style="background:#fff;border-radius:20px;padding:34px;text-align:center;color:#8a909d;border:1px solid #eceef4;font-size:19px;">还没有待办，使用“待办 内容”创建第一项任务。</div>'
        html += '<div style="padding-top:16px;text-align:center;color:#8a909d;font-size:14px;">编号：1=当前列表序号　#12=永久 ID</div></div>'
        try:
            return await self.html_render(html, {}, options={"type": "png", "full_page": True})
        except Exception as exc:
            logger.warning("[todo] 菜单图片生成失败：%s", exc)
            return None

    @filter.command("代办菜单")
    async def menu(self, event: AstrMessageEvent, args: str = ""):
        if not self.is_enabled():
            return
        mode = str(self.cfg("menu_send_mode", "image")).lower()
        if mode not in VALID_SEND_MODES:
            mode = "image"
        text = self._menu_text(event)
        image_url = None
        if mode in {"image", "both"}:
            image_url = await self._menu_image(event)
        if mode in {"text", "both"} or image_url is None:
            yield event.plain_result(text)
        if image_url and mode in {"image", "both"}:
            yield event.image_result(image_url)

    # ---------- 管理面板 ----------
    @filter.command("管理面板")
    async def admin_panel(self, event: AstrMessageEvent, args: str = ""):
        if not self.is_enabled():
            return
        denied = self.admin_required(event)
        if denied:
            yield event.plain_result(denied)
            return
        todos = self._get_todos(event)
        pending = sum(not x.completed for x in todos)
        admin_ids = sorted(self.admin_ids())
        lines = [
            "🛠️ 待办管理面板",
            "━━━━━━━━━━━━━━━━",
            f"状态：{'运行中' if self.is_enabled() else '已关闭'}",
            f"当前会话：{len(todos)} 个待办 / {pending} 个未完成",
            f"管理员 QQ：{', '.join(admin_ids) if admin_ids else '未配置'}",
            "",
            "⚙️ 当前功能",
            f"自动代办：{'开启' if self.cfg('auto_assign', True) else '关闭'}",
            f"提醒：{'开启' if self.cfg('reminder_enabled', True) else '关闭'}",
            f"菜单发送：{self.cfg('menu_send_mode', 'image')}",
            f"清空二次确认：{'开启' if self.cfg('clear_require_confirm', True) else '关闭'}",
            "",
            "🔐 管理员命令",
            "管理面板　查看管理中心",
            "管理设置　查看详细配置",
            "清空待办 → 确认清空　执行危险操作",
        ]
        yield event.plain_result("\n".join(lines))

    @filter.command("管理设置")
    async def settings(self, event: AstrMessageEvent, args: str = ""):
        if not self.is_enabled():
            return
        denied = self.admin_required(event)
        if denied:
            yield event.plain_result(denied)
            return
        keys = [
            ("enabled", "插件启用"),
            ("default_priority", "默认优先级"),
            ("max_todos", "每会话最大待办"),
            ("max_content_length", "单条内容最大长度"),
            ("auto_assign", "自助代办"),
            ("reminder_enabled", "提醒功能"),
            ("reminder_check_interval", "提醒检查间隔"),
            ("reminder_send_mode", "提醒发送方式"),
            ("menu_send_mode", "菜单发送方式"),
            ("menu_include_summary", "菜单附带待办摘要"),
            ("clear_require_confirm", "清空二次确认"),
            ("clear_confirm_seconds", "清空确认有效期"),
            ("admin_only_panel", "管理面板仅管理员"),
        ]
        lines = ["⚙️ 插件详细配置", "━━━━━━━━━━━━━━━━"]
        for key, label in keys:
            value = self.cfg(key)
            lines.append(f"{label}：{value}")
        lines.append("管理员 QQ 号：" + (", ".join(sorted(self.admin_ids())) or "未配置"))
        lines.append("\n配置请直接在 AstrBot WebUI → 插件 → 本插件 → 配置中修改。")
        yield event.plain_result("\n".join(lines))

    # ---------- 添加 ----------
    @filter.command("待办")
    async def add_todo(self, event: AstrMessageEvent, args: str = ""):
        if not self.is_enabled():
            return
        content = args.strip()
        if not content:
            yield event.plain_result("❌ 用法：待办 内容 [高/中/低]\n例如：待办 完成周报 高")
            return
        max_len = max(20, int(self.cfg("max_content_length", 200)))
        priority = str(self.cfg("default_priority", "中"))
        tokens = content.split()
        if tokens and tokens[0] in VALID_PRIORITIES:
            priority = tokens.pop(0)
            content = " ".join(tokens).strip()
        elif tokens and tokens[-1] in VALID_PRIORITIES:
            priority = tokens.pop()
            content = " ".join(tokens).strip()
        if not content:
            yield event.plain_result("❌ 待办内容不能为空。")
            return
        if len(content) > max_len:
            yield event.plain_result(f"❌ 待办内容过长，最多 {max_len} 个字符。")
            return
        todos = self._get_todos(event)
        if len(todos) >= int(self.cfg("max_todos", 200)):
            yield event.plain_result(f"❌ 当前会话待办已达到上限 {self.cfg('max_todos', 200)}。")
            return
        item = TodoItem(content=content, priority=priority, creator=str(event.get_sender_id() or ""), id=self._next_id(todos))
        todos.append(item)
        self._set_todos(event, todos)
        yield event.plain_result(f"✅ 已添加待办 #{item.id}\n{self._format_item(item)}")

    # ---------- 列表 / 统计 ----------
    @filter.command("待办列表")
    async def list_todos(self, event: AstrMessageEvent):
        if not self.is_enabled():
            return
        todos = self._sorted(self._get_todos(event))
        if not todos:
            yield event.plain_result("📝 当前没有待办事项。使用：待办 内容")
            return
        lines = ["📝 待办事项", "━━━━━━━━━━━━━━━━"]
        for idx, item in enumerate(todos, 1):
            lines.append(self._format_item(item, idx))
        done = sum(x.completed for x in todos)
        lines.extend(["━━━━━━━━━━━━━━━━", f"📊 已完成 {done}/{len(todos)}", "编号：数字=当前列表序号，#数字=永久 ID"])
        yield event.plain_result("\n".join(lines))

    @filter.command("今日待办")
    async def today_todos(self, event: AstrMessageEvent):
        if not self.is_enabled():
            return
        today = _now().date().isoformat()
        todos = self._sorted(self._get_todos(event))
        items = [x for x in todos if (x.created_at or "")[:10] == today or ((x.completed_at or "")[:10] == today)]
        if not items:
            yield event.plain_result("📅 今天还没有待办。")
            return
        lines = [f"📅 今日待办 · {today}", "━━━━━━━━━━━━━━━━"]
        for idx, item in enumerate(items, 1):
            lines.append(self._format_item(item, idx))
        yield event.plain_result("\n".join(lines))

    @filter.command("完成率")
    async def completion_rate(self, event: AstrMessageEvent):
        if not self.is_enabled():
            return
        todos = self._get_todos(event)
        if not todos:
            yield event.plain_result("📊 当前没有待办，完成率 0%。")
            return
        done = sum(x.completed for x in todos)
        rate = done / len(todos) * 100
        filled = int(rate // 5)
        bar = "█" * filled + "░" * (20 - filled)
        yield event.plain_result(
            f"📊 完成统计\n━━━━━━━━━━━━━━━━\n"
            f"总任务：{len(todos)}\n已完成：{done}\n未完成：{len(todos)-done}\n完成率：{rate:.1f}%\n进度：|{bar}|"
        )

    # ---------- 完成 / 删除 ----------
    @filter.command("完成")
    async def complete_todo(self, event: AstrMessageEvent, args: str = ""):
        if not self.is_enabled():
            return
        todos = self._get_todos(event)
        item, error = self._resolve_item(todos, args)
        if not item:
            yield event.plain_result(f"❌ {error}")
            return
        if item.completed:
            yield event.plain_result(f"⚠️ 待办 #{item.id} 已经完成。")
            return
        item.completed = True
        item.completed_at = _now().isoformat(timespec="seconds")
        self._set_todos(event, todos)
        yield event.plain_result(f"🎉 已完成\n{self._format_item(item)}")

    @filter.command("删除")
    async def delete_todo(self, event: AstrMessageEvent, args: str = ""):
        if not self.is_enabled():
            return
        todos = self._get_todos(event)
        item, error = self._resolve_item(todos, args)
        if not item:
            yield event.plain_result(f"❌ {error}")
            return
        todos.remove(item)
        self._set_todos(event, todos)
        yield event.plain_result(f"🗑️ 已删除待办 #{item.id}：{item.content}")

    # ---------- 提醒 ----------
    async def _set_reminder(self, event: AstrMessageEvent, args: str) -> str:
        parts = args.strip().split(maxsplit=1)
        if len(parts) != 2:
            return "❌ 用法：提醒 编号 时间\n例如：提醒 1 18:30\n也支持：提醒 #12 明天 08:00"
        token, raw_time = parts
        todos = self._get_todos(event)
        item, error = self._resolve_item(todos, token)
        if not item:
            return f"❌ {error}"
        when = parse_reminder_datetime(raw_time)
        if when is None:
            return "❌ 时间无法识别。示例：18:30、晚上8点、今天 18:30、明天 08:00。"
        if item.completed:
            return "⚠️ 这个待办已经完成，不需要设置提醒。"
        item.reminder_at = when.isoformat(timespec="seconds")
        item.reminder_origin = str(event.unified_msg_origin)
        item.reminder_text = str(self.cfg("reminder_prefix", "⏰ 待办提醒"))
        self._set_todos(event, todos)
        return f"⏰ 已为待办 #{item.id} 设置提醒：{when.strftime('%Y-%m-%d %H:%M')}\n{item.content}"

    @filter.command("提醒")
    async def reminder(self, event: AstrMessageEvent, args: str = ""):
        if not self.is_enabled() or not bool(self.cfg("reminder_enabled", True)):
            yield event.plain_result("ℹ️ 提醒功能当前未开启。")
            return
        yield event.plain_result(await self._set_reminder(event, args))

    @filter.command("定时提醒")
    async def reminder_alias(self, event: AstrMessageEvent, args: str = ""):
        if not self.is_enabled() or not bool(self.cfg("reminder_enabled", True)):
            yield event.plain_result("ℹ️ 提醒功能当前未开启。")
            return
        yield event.plain_result(await self._set_reminder(event, args))

    @filter.command("取消提醒")
    async def cancel_reminder(self, event: AstrMessageEvent, args: str = ""):
        if not self.is_enabled():
            return
        todos = self._get_todos(event)
        item, error = self._resolve_item(todos, args)
        if not item:
            yield event.plain_result(f"❌ {error}")
            return
        if not item.reminder_at:
            yield event.plain_result(f"ℹ️ 待办 #{item.id} 没有设置提醒。")
            return
        item.reminder_at = None
        item.reminder_origin = None
        item.reminder_text = None
        self._set_todos(event, todos)
        yield event.plain_result(f"✅ 已取消待办 #{item.id} 的提醒。")

    async def _send_active_reminder(self, origin: str, item: TodoItem) -> None:
        message = f"⏰ 待办提醒\n━━━━━━━━━━━━━━━━\n{PRIORITY_ICONS.get(item.priority, '⚪')} {item.content}\n永久 ID：#{item.id}"
        mode = str(self.cfg("reminder_send_mode", "text")).lower()
        if mode not in VALID_SEND_MODES:
            mode = "text"
        try:
            if mode == "text":
                await self.context.send_message(origin, MessageChain().message(message))
                return
            # 主动发送图片使用本地渲染结果，符合 AstrBot MessageChain/file_image 发送方式。
            html = f"""
            <div style=\"width:700px;padding:34px;font-family:Arial,'Microsoft YaHei';background:#f7f8fc;color:#202431;\">
              <div style=\"background:#202735;color:#fff;border-radius:22px;padding:28px;\">
                <div style=\"font-size:32px;font-weight:800;\">⏰ 待办提醒</div>
                <div style=\"margin-top:10px;font-size:18px;opacity:.82;\">到时间了，记得处理这个任务</div>
              </div>
              <div style=\"background:#fff;border-radius:20px;padding:24px;margin-top:16px;border:1px solid #eceef4;\">
                <div style=\"font-size:25px;font-weight:750;\">{escape(item.content)}</div>
                <div style=\"margin-top:12px;color:#7d8492;font-size:16px;\">优先级：{escape(item.priority)}　·　永久 ID：#{item.id}</div>
              </div>
            </div>
            """
            path = await self.html_render(html, {}, options={"type": "png", "full_page": True})
            if mode == "both":
                chain = MessageChain().message(message)
            else:
                chain = MessageChain()
            if isinstance(path, str):
                chain.file_image(path)
            else:
                chain.message(message)
            await self.context.send_message(origin, chain)
        except Exception as exc:
            logger.warning("[todo] 主动提醒发送失败：%s", exc)
            try:
                await self.context.send_message(origin, MessageChain().message(message))
            except Exception as fallback_exc:
                logger.error("[todo] 主动提醒文本兜底失败：%s", fallback_exc)

    async def _reminder_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(max(10, int(self.cfg("reminder_check_interval", 20))))
                if not self.is_enabled() or not bool(self.cfg("reminder_enabled", True)):
                    continue
                now = _now()
                changed = False
                sessions = self.data.setdefault("sessions", {})
                for session_key, raw_items in list(sessions.items()):
                    if not isinstance(raw_items, list):
                        continue
                    for raw in raw_items:
                        if not isinstance(raw, dict):
                            continue
                        item = TodoItem.from_dict(raw)
                        if not item.reminder_at or item.completed or not item.reminder_origin:
                            continue
                        try:
                            reminder_at = dt.datetime.fromisoformat(item.reminder_at)
                        except ValueError:
                            raw["reminder_at"] = None
                            changed = True
                            continue
                        if reminder_at <= now:
                            await self._send_active_reminder(item.reminder_origin, item)
                            raw["reminder_at"] = None
                            raw["reminder_origin"] = None
                            raw["reminder_text"] = None
                            changed = True
                if changed:
                    self._save_data()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("[todo] 提醒调度器异常：%s", exc)

    # ---------- 自助代办 ----------
    @filter.command("自助代办")
    async def auto_assign(self, event: AstrMessageEvent, args: str = ""):
        if not self.is_enabled() or not bool(self.cfg("auto_assign", True)):
            yield event.plain_result("ℹ️ 自助代办功能当前未开启。")
            return
        todos = self._get_todos(event)
        pending = [x for x in todos if not x.completed]
        if not pending:
            yield event.plain_result("ℹ️ 当前没有可代办的任务。")
            return
        # 基于发送者 ID 选取一个稳定的任务，避免依赖随机数库。
        seed = str(event.get_sender_id() or "")
        item = pending[hash(seed) % len(pending)]
        item.assigned_to = seed
        self._set_todos(event, todos)
        yield event.plain_result(f"🤝 已为你设置代办\n{self._format_item(item)}")

    # ---------- 清空确认 ----------
    @filter.command("清空待办")
    async def clear_todos(self, event: AstrMessageEvent):
        if not self.is_enabled():
            return
        denied = self._admin_denied_message(event) if bool(self.cfg("clear_admin_only", False)) else None
        if denied:
            yield event.plain_result(denied)
            return
        todos = self._get_todos(event)
        if not todos:
            yield event.plain_result("⚠️ 当前没有待办事项。")
            return
        if not bool(self.cfg("clear_require_confirm", True)):
            self._set_todos(event, [])
            yield event.plain_result(f"✅ 已清空 {len(todos)} 个待办。")
            return
        expires = _now().timestamp() + max(10, int(self.cfg("clear_confirm_seconds", 60)))
        self._clear_confirmations[self._session_key(event)] = expires
        yield event.plain_result(
            f"⚠️ 即将清空当前会话的 {len(todos)} 个待办。\n"
            f"请在 {self.cfg('clear_confirm_seconds', 60)} 秒内回复：确认清空\n"
            f"取消可回复：取消清空"
        )

    @filter.command("确认清空")
    async def confirm_clear(self, event: AstrMessageEvent, args: str = ""):
        key = self._session_key(event)
        expires = self._clear_confirmations.get(key, 0)
        if _now().timestamp() > expires:
            self._clear_confirmations.pop(key, None)
            yield event.plain_result("⌛ 清空确认已失效，请重新发送：清空待办")
            return
        todos = self._get_todos(event)
        self._clear_confirmations.pop(key, None)
        self._set_todos(event, [])
        yield event.plain_result(f"✅ 已清空 {len(todos)} 个待办。")

    @filter.command("取消清空")
    async def cancel_clear(self, event: AstrMessageEvent, args: str = ""):
        self._clear_confirmations.pop(self._session_key(event), None)
        yield event.plain_result("✅ 已取消清空操作。")

    async def terminate(self):
        if self._scheduler_task:
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                pass
            self._scheduler_task = None
