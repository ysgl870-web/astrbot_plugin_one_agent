"""AstrBot 待办事项、提醒、小游戏与 QQ 消息样式插件。

v3.0.0 重点：
- 修复 /提醒 1 14:21 因 AstrBot 自动参数拆分导致的格式错误。
- 时间从原始 event.message_str 提取，支持带空格的中文时间。
- 纯数字编号 = 当前列表序号；#数字 = 永久 ID。
- 待办/提醒/截止时间/搜索/统计等功能统一使用同一套数据模型。
- QQ 群/私聊/频道支持文本、图片、Markdown、原生 Ark/Keyboard（在配置并且
  当前 QQ 适配器/机器人权限支持时启用），失败自动降级到兼容模式。
- 增加签到、积分榜、猜数字、石头剪刀布、骰子、猜硬币、幸运抽签等小游戏。
- 配置项集中到 _conf_schema.json，历史更新放在 CHANGELOG.md。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import random
import re
import tempfile
from dataclasses import dataclass, field
from html import escape
from typing import Any, Iterable, Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register

try:
    from astrbot.core.config.astrbot_config import AstrBotConfig
except Exception:  # pragma: no cover - 旧版本兼容
    from astrbot.api import AstrBotConfig  # type: ignore


PLUGIN_ID = "astrbot_plugin_one_agent"
DATA_VERSION = 3
VALID_PRIORITIES = {"高", "中", "低"}
PRIORITY_ICONS = {"高": "🔴", "中": "🟡", "低": "🟢"}
PRIORITY_ORDER = {"高": 0, "中": 1, "低": 2}
VALID_MODES = {"auto", "text", "image", "markdown", "ark", "keyboard", "both"}
GAME_NAMES = {"猜数字", "石头剪刀布", "掷骰子", "猜硬币", "幸运抽签"}
RPS = {"石头": "剪刀", "剪刀": "布", "布": "石头"}
COIN_VALUES = {"正面", "反面"}


def _now() -> dt.datetime:
    return dt.datetime.now()


def _resolve_data_dir() -> str:
    try:
        from astrbot.api.star import StarTools

        path = str(StarTools.get_data_dir(PLUGIN_ID))
    except Exception as exc:  # pragma: no cover
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
    created_at: str = field(default_factory=lambda: _now().isoformat(timespec="seconds"))
    completed: bool = False
    completed_at: Optional[str] = None
    id: int = 0
    reminder_at: Optional[str] = None
    reminder_origin: Optional[str] = None
    reminder_text: Optional[str] = None
    due_at: Optional[str] = None

    def __post_init__(self) -> None:
        self.content = self.content.strip()
        if self.priority not in VALID_PRIORITIES:
            self.priority = "中"

    def to_dict(self) -> dict[str, Any]:
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
            "reminder_set": bool(self.reminder_at),
            "due_at": self.due_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TodoItem":
        return cls(
            content=str(data.get("content", "")),
            priority=str(data.get("priority", "中")),
            creator=str(data.get("creator", "")),
            assigned_to=str(data.get("assigned_to", "")),
            created_at=str(data.get("created_at") or _now().isoformat(timespec="seconds")),
            completed=bool(data.get("completed", False)),
            completed_at=data.get("completed_at"),
            id=int(data.get("id", 0) or 0),
            reminder_at=data.get("reminder_at"),
            reminder_origin=data.get("reminder_origin"),
            reminder_text=data.get("reminder_text"),
            due_at=data.get("due_at"),
        )


def _parse_time_only(value: str) -> Optional[tuple[int, int]]:
    value = value.strip().lower().replace("：", ":")
    value = re.sub(r"\s+", "", value)
    if not value:
        return None

    # 14:21 / 8 / 08:30
    m = re.fullmatch(r"(\d{1,2})(?::(\d{1,2}))?", value)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
        return None

    pm = False
    for prefix in ("晚上", "下午", "傍晚", "中午"):
        if value.startswith(prefix):
            pm = True
            value = value[len(prefix) :]
            break
    for prefix in ("早上", "早晨", "凌晨"):
        if value.startswith(prefix):
            value = value[len(prefix) :]
            break

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
    """解析提醒时间。

    支持：
      14:21 / 14点21分 / 晚上8点半
      今天 18:30 / 明天 08:00
      2026-10-06 18:30 / 10-06 18:30
      30分钟后 / 2小时后 / 1天后
    """
    text = raw.strip().replace("T", " ").replace("：", ":")
    if not text:
        return None
    now = now or _now()

    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(分钟|分|小时|时|天)后", text)
    if m:
        amount = float(m.group(1))
        if amount <= 0:
            return None
        unit = m.group(2)
        if unit in {"分钟", "分"}:
            return now + dt.timedelta(minutes=amount)
        if unit in {"小时", "时"}:
            return now + dt.timedelta(hours=amount)
        return now + dt.timedelta(days=amount)

    compact = re.sub(r"\s+", "", text)
    for prefix, delta_days in (("明天", 1), ("后天", 2)):
        if compact.startswith(prefix):
            parsed = _parse_time_only(compact[len(prefix) :])
            if not parsed:
                return None
            return (now + dt.timedelta(days=delta_days)).replace(
                hour=parsed[0], minute=parsed[1], second=0, microsecond=0
            )
    if compact.startswith("今天"):
        parsed = _parse_time_only(compact[2:])
        if not parsed:
            return None
        return now.replace(hour=parsed[0], minute=parsed[1], second=0, microsecond=0)

    m = re.fullmatch(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})\s+(.+)", text)
    if m:
        parsed = _parse_time_only(m.group(4))
        if not parsed:
            return None
        try:
            return dt.datetime(
                int(m.group(1)), int(m.group(2)), int(m.group(3)), parsed[0], parsed[1]
            )
        except ValueError:
            return None

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
    candidate = now.replace(hour=parsed[0], minute=parsed[1], second=0, microsecond=0)
    if candidate <= now:
        candidate += dt.timedelta(days=1)
    return candidate


def extract_command_tail(event: AstrMessageEvent, *commands: str) -> str:
    """从原始纯文本消息提取命令后的全部内容。

    AstrBot 会自动解析带参 command；对于“编号 + 带空格时间”这类复合参数，
    这里故意不依赖 handler 参数，而是直接读取 message_str。
    """
    raw = ""
    try:
        raw = str(event.get_message_str() or "").strip()
    except Exception:
        raw = str(getattr(event, "message_str", "") or "").strip()
    if not raw:
        raw = str(getattr(event, "message_str", "") or "").strip()

    # 兼容 /提醒、!提醒、／提醒 以及 AstrBot 已经去掉前缀的情况。
    normalized = re.sub(r"^\s*[/！!／]", "", raw, count=1).strip()
    for command in commands:
        pattern = rf"^{re.escape(command)}(?:\s+|$)"
        if re.match(pattern, normalized, flags=re.IGNORECASE):
            return re.sub(pattern, "", normalized, count=1, flags=re.IGNORECASE).strip()

    # 某些适配器/命令调度阶段传入的 message_str 已经去掉了命令名，
    # 此时 normalized 本身就是参数区，不能再退回 str(message_chain)。
    return normalized


def parse_index_and_tail(event: AstrMessageEvent, *commands: str) -> tuple[str, str]:
    tail = extract_command_tail(event, *commands)
    parts = tail.split(maxsplit=1)
    if len(parts) != 2:
        return (parts[0], "") if parts else ("", "")
    return parts[0], parts[1].strip()


@register(
    PLUGIN_ID,
    "one_agent",
    "待办事项、提醒、小游戏与 QQ 多消息样式插件",
    "3.0.0",
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
        self.data = self._load_data()
        self._clear_confirmations: dict[str, float] = {}
        self._scheduler_task: Optional[asyncio.Task[Any]] = None
        self._game_cooldowns: dict[str, float] = {}
        try:
            self._scheduler_task = asyncio.create_task(self._reminder_loop())
        except RuntimeError:
            logger.warning("[todo] 当前没有可用事件循环，提醒调度器将在插件重载后启动。")

    # ---------------- 配置 ----------------
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
        result: set[str] = set()
        for item in values or []:
            normalized = re.sub(r"[^0-9]", "", str(item))
            if normalized:
                result.add(normalized)
        return result

    def is_admin(self, event: AstrMessageEvent) -> bool:
        sender = re.sub(r"[^0-9]", "", str(event.get_sender_id() or ""))
        return sender in self.admin_ids()

    def _admin_denied(self) -> str:
        return "⛔ 你没有管理员权限。\n请在 AstrBot → 插件 → 本插件配置中添加你的 QQ 号。"

    def require_admin(self, event: AstrMessageEvent) -> bool:
        if not bool(self.cfg("admin_only_panel", True)):
            return True
        return self.is_admin(event)

    # ---------------- 数据 ----------------
    def _empty_data(self) -> dict[str, Any]:
        return {
            "version": DATA_VERSION,
            "sessions": {},
            "game_stats": {},
            "daily_checkins": {},
        }

    def _load_data(self) -> dict[str, Any]:
        if not os.path.exists(self.data_file):
            return self._empty_data()
        try:
            with open(self.data_file, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            if not isinstance(raw, dict):
                raise ValueError("根数据不是对象")
            if "sessions" not in raw:
                # 兼容最早版 {group_id:[todo,...]}
                raw = {
                    "version": DATA_VERSION,
                    "sessions": {"legacy:" + str(k): v for k, v in raw.items() if isinstance(v, list)},
                }
            raw.setdefault("version", DATA_VERSION)
            raw.setdefault("game_stats", {})
            raw.setdefault("daily_checkins", {})
            return raw
        except Exception as exc:
            logger.error("[todo] 数据读取失败: %s", exc)
            try:
                with open(self.data_file, "rb") as src, open(self.backup_file, "wb") as dst:
                    dst.write(src.read())
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

    def _get_todos(self, event: AstrMessageEvent) -> list[TodoItem]:
        sessions = self.data.setdefault("sessions", {})
        key = self._session_key(event)
        raw = sessions.get(key)
        if not isinstance(raw, list):
            legacy = self._legacy_key(event)
            raw = sessions.get(legacy, []) if legacy else []
        result: list[TodoItem] = []
        for item in raw:
            if isinstance(item, dict) and item.get("content") is not None:
                try:
                    result.append(TodoItem.from_dict(item))
                except Exception:
                    continue
        return result

    def _set_todos(self, event: AstrMessageEvent, todos: Iterable[TodoItem]) -> None:
        key = self._session_key(event)
        sessions = self.data.setdefault("sessions", {})
        sessions[key] = [item.to_dict() for item in todos]
        legacy = self._legacy_key(event)
        if legacy and legacy != key:
            sessions.pop(legacy, None)
        self._save_data()

    def _next_id(self, todos: list[TodoItem]) -> int:
        return max((item.id for item in todos), default=0) + 1

    def _sorted(self, todos: list[TodoItem]) -> list[TodoItem]:
        return sorted(
            todos,
            key=lambda x: (x.completed, PRIORITY_ORDER.get(x.priority, 1), x.due_at or "9999", x.created_at, x.id),
        )

    def _resolve_item(self, todos: list[TodoItem], token: str) -> tuple[Optional[TodoItem], str]:
        token = token.strip()
        if not token:
            return None, "请提供编号，例如：完成 1；精确 ID：完成 #12。"
        if token.startswith("#"):
            try:
                wanted = int(token[1:])
            except ValueError:
                return None, "编号格式错误，例如：#12。"
            for item in todos:
                if item.id == wanted:
                    return item, ""
            return None, f"没有找到永久 ID #{wanted}。"
        try:
            position = int(token)
        except ValueError:
            return None, "编号必须是数字，例如：完成 1。"
        display = self._sorted(todos)
        if position < 1 or position > len(display):
            return None, f"列表中没有第 {position} 项。"
        return display[position - 1], ""

    # ---------------- 格式/消息 ----------------
    def _format_dt(self, value: Optional[str]) -> str:
        if not value:
            return ""
        try:
            return dt.datetime.fromisoformat(value).strftime("%m-%d %H:%M")
        except Exception:
            return value

    def _format_item(self, item: TodoItem, position: Optional[int] = None) -> str:
        index = f"{position}. " if position is not None else ""
        status = "✅" if item.completed else "⬜"
        extra: list[str] = []
        if item.reminder_at:
            extra.append("⏰ " + self._format_dt(item.reminder_at))
        if item.due_at:
            extra.append("📌 " + self._format_dt(item.due_at))
        suffix = " · " + " · ".join(extra) if extra else ""
        return f"{index}{status} {PRIORITY_ICONS.get(item.priority,'⚪')} {item.content}  #{item.id}{suffix}"

    def _platform_name(self, event: AstrMessageEvent) -> str:
        try:
            return str(event.get_platform_name() or "").lower()
        except Exception:
            return ""

    def _effective_mode(self, event: AstrMessageEvent, requested: Optional[str] = None) -> str:
        mode = (requested or str(self.cfg("menu_send_mode", "auto"))).lower()
        if mode not in VALID_MODES:
            mode = "auto"
        if mode != "auto":
            return mode
        platform = self._platform_name(event)
        if platform == "qq_official":
            if int(self.cfg("qq_ark_template_id", 0) or 0) > 0 and bool(self.cfg("qq_native_ark_enabled", True)):
                return "ark"
            if str(self.cfg("qq_keyboard_template_id", "")).strip() and bool(self.cfg("qq_native_keyboard_enabled", True)):
                return "keyboard"
            return "markdown"
        return "image" if bool(self.cfg("auto_use_image_for_group", True)) else "text"

    async def _send_chain(self, event: AstrMessageEvent, text: str, *, mode: str = "text", image: Any = None):
        if mode == "markdown":
            # 直接 event.send(MessageChain) 才能保留 use_markdown(True) 元数据。
            await event.send(MessageChain().message(text).use_markdown(True))
            return
        if mode == "both":
            yield event.plain_result(text)
            if image:
                yield event.image_result(image)
            return
        if mode == "image" and image:
            yield event.image_result(image)
            return
        yield event.plain_result(text)

    async def _render_card(self, title: str, subtitle: str, body_html: str) -> Optional[Any]:
        html = f"""
        <div style=\"width:780px;box-sizing:border-box;padding:26px;background:#f5f7fb;font-family:Arial,'Microsoft YaHei',sans-serif;color:#1f2937;\">
          <div style=\"background:linear-gradient(135deg,#1e293b,#334155);color:#fff;border-radius:20px;padding:26px 28px;\">
            <div style=\"font-size:31px;font-weight:800;\">{escape(title)}</div>
            <div style=\"font-size:16px;margin-top:8px;opacity:.82;\">{escape(subtitle)}</div>
          </div>
          <div style=\"background:#fff;border-radius:20px;margin-top:16px;padding:22px;border:1px solid #e8ebf1;\">{body_html}</div>
        </div>
        """
        try:
            return await self.html_render(html, {}, options={"type": "png", "full_page": True})
        except Exception as exc:
            logger.warning("[todo] HTML 转图片失败: %s", exc)
            return None

    async def _menu_image(self, event: AstrMessageEvent) -> Optional[Any]:
        todos = self._sorted(self._get_todos(event))
        done = sum(item.completed for item in todos)
        cards = []
        for idx, item in enumerate(todos[:10], 1):
            reminders = []
            if item.reminder_at:
                reminders.append("⏰ " + self._format_dt(item.reminder_at))
            if item.due_at:
                reminders.append("📌 " + self._format_dt(item.due_at))
            cards.append(
                f"""
                <div style=\"display:flex;gap:14px;align-items:center;padding:13px 0;border-bottom:1px solid #eef0f4;\">
                  <div style=\"width:36px;height:36px;border-radius:11px;background:#f0f2f6;text-align:center;line-height:36px;font-weight:800;\">{idx}</div>
                  <div style=\"flex:1;\">
                    <div style=\"font-size:18px;font-weight:700;\">{escape(item.content)}</div>
                    <div style=\"font-size:13px;color:#8a93a3;margin-top:4px;\">#{item.id} · {escape(item.priority)}优先级 · {escape(' / '.join(reminders))}</div>
                  </div>
                  <div style=\"font-size:22px;\">{'✅' if item.completed else '⬜'}</div>
                </div>"""
            )
        if not cards:
            body = '<div style="padding:20px;text-align:center;color:#8a93a3;font-size:18px;">还没有待办事项</div>'
        else:
            body = f"<div style='font-size:16px;font-weight:750;margin-bottom:8px;'>常用指令</div><div style='font-size:14px;color:#667085;line-height:1.8;'>待办 内容　·　完成 编号　·　提醒 编号 时间　·　小游戏</div><div style='margin-top:15px;'>{''.join(cards)}</div>"
            if len(todos) > 10:
                body += f"<div style='padding-top:10px;color:#8a93a3;'>当前共有 {len(todos)} 项，仅显示前 10 项</div>"
        return await self._render_card(
            str(self.cfg("menu_title", "📋 待办管理中心")),
            f"进度 {done}/{len(todos)} · 编号支持 1=列表序号 / #12=永久 ID",
            body,
        )

    def _markdown_menu(self, event: AstrMessageEvent) -> str:
        todos = self._sorted(self._get_todos(event))
        done = sum(item.completed for item in todos)
        lines = [
            f"# {self.cfg('menu_title', '待办管理中心')}",
            f"> 进度：{done}/{len(todos)}",
            "",
            "### 常用操作",
            "- `待办 内容 [高/中/低]` 添加任务",
            "- `待办列表` 查看任务",
            "- `完成 编号` / `删除 编号`",
            "- `提醒 编号 时间`",
            "- `截止 编号 时间`",
            "- `搜索 关键词` / `待办统计`",
            "- `小游戏`",
        ]
        if todos and bool(self.cfg("menu_include_summary", True)):
            lines += ["", "### 当前待办"]
            lines.extend(f"{idx}. {'✅' if item.completed else '⬜'} {item.content} `#{item.id}`" for idx, item in enumerate(todos[:8], 1))
        return "\n".join(lines)

    # ---------------- 菜单 ----------------
    @filter.command("代办菜单", alias={"待办菜单", "菜单"})
    async def menu(self, event: AstrMessageEvent):
        if not self.is_enabled():
            return
        requested = str(self.cfg("menu_send_mode", "auto")).lower()
        mode = self._effective_mode(event, requested)
        text = self._markdown_menu(event) if mode == "markdown" else self._menu_text(event)
        if mode in {"ark", "keyboard"}:
            if await self._send_qq_native(event, text, mode):
                return
            mode = "markdown" if self._platform_name(event) == "qq_official" else "image"
            text = self._markdown_menu(event) if mode == "markdown" else self._menu_text(event)
        image = await self._menu_image(event) if mode in {"image", "both"} else None
        async for result in self._send_chain(event, text, mode=mode, image=image):
            yield result

    def _menu_text(self, event: AstrMessageEvent) -> str:
        todos = self._sorted(self._get_todos(event))
        done = sum(item.completed for item in todos)
        lines = [
            str(self.cfg("menu_title", "📋 待办管理中心")),
            "━━━━━━━━━━━━━━━━",
            f"📊 进度：{done}/{len(todos)}",
            "",
            "📝 待办",
            "待办 内容 [高/中/低]  添加任务",
            "待办列表              查看任务",
            "完成 编号              标记完成",
            "删除 编号              删除任务",
            "编辑 编号 新内容       修改任务",
            "改优先级 编号 高       调整优先级",
            "提醒 编号 时间         设置提醒",
            "取消提醒 编号         取消提醒",
            "截止 编号 时间         设置截止",
            "今日待办 / 逾期待办",
            "搜索 关键词 / 待办统计",
            "",
            "🎮 娱乐",
            "小游戏 / 签到 / 积分榜",
            "",
            "编号：1 = 当前列表序号，#12 = 永久 ID",
        ]
        if bool(self.cfg("menu_include_summary", True)) and todos:
            lines += ["", "📌 当前待办（前 8 项）"]
            lines += [self._format_item(item, idx) for idx, item in enumerate(todos[:8], 1)]
        return "\n".join(lines)

    # ---------------- QQ 原生 Ark/Keyboard ----------------
    async def _send_qq_native(self, event: AstrMessageEvent, text: str, mode: str) -> bool:
        if self._platform_name(event) != "qq_official" or not bool(self.cfg("qq_native_enabled", True)):
            return False
        bot = getattr(event, "bot", None)
        if bot is None:
            return False
        api = getattr(bot, "api", None)
        if api is None:
            return False

        try:
            raw_event = getattr(getattr(event, "message_obj", None), "raw_message", None)
            if mode == "ark":
                template_id = int(self.cfg("qq_ark_template_id", 0) or 0)
                if template_id <= 0:
                    return False
                kv_map: dict[str, Any] = {
                    "#TITLE#": str(self.cfg("menu_title", "待办管理中心")),
                    "#PROMPT#": "待办管理中心",
                    "#METATITLE#": "待办管理",
                    "#CONTENT#": text[:80],
                }
                extra = str(self.cfg("qq_ark_kv_json", "")).strip()
                if extra:
                    try:
                        parsed = json.loads(extra)
                        if isinstance(parsed, dict):
                            kv_map.update({str(k): str(v) for k, v in parsed.items()})
                    except json.JSONDecodeError:
                        logger.warning("[todo] qq_ark_kv_json 不是合法 JSON，忽略自定义 KV")
                ark = {
                    "template_id": template_id,
                    "kv": [{"key": k, "value": v} for k, v in kv_map.items()],
                }
                return bool(await self._qq_post_native(api, raw_event, event, ark=ark))

            if mode == "keyboard":
                template_id = str(self.cfg("qq_keyboard_template_id", "")).strip()
                if not template_id:
                    return False
                markdown = {"content": text}
                keyboard = {"id": template_id}
                return bool(await self._qq_post_native(api, raw_event, event, markdown=markdown, keyboard=keyboard))
        except Exception as exc:
            logger.warning("[todo] QQ 原生 %s 发送失败，回退普通模式：%s", mode, exc)
        return False

    async def _qq_post_native(self, api: Any, raw_event: Any, event: AstrMessageEvent, *, ark: Any = None, markdown: Any = None, keyboard: Any = None) -> Any:
        msg_id = str(getattr(event, "message_id", "") or "") or None
        # botpy 事件类型通过属性而非强制 import 判断，避免普通 AstrBot 环境缺 botpy 导致插件无法加载。
        if getattr(raw_event, "group_openid", None):
            group_openid = str(raw_event.group_openid)
            func = getattr(api, "post_group_message", None)
            if callable(func):
                return await func(
                    group_openid=group_openid,
                    ark=ark,
                    markdown=markdown,
                    keyboard=keyboard,
                    msg_id=msg_id,
                    msg_seq=random.randint(1, 10000),
                )
        if getattr(getattr(raw_event, "author", None), "user_openid", None):
            openid = str(raw_event.author.user_openid)
            func = getattr(api, "post_c2c_message", None)
            if callable(func):
                return await func(
                    openid=openid,
                    ark=ark,
                    markdown=markdown,
                    keyboard=keyboard,
                    msg_id=msg_id,
                    msg_seq=random.randint(1, 10000),
                )
        channel_id = getattr(raw_event, "channel_id", None)
        if channel_id:
            if keyboard is not None and callable(getattr(api, "post_keyboard_message", None)):
                return await api.post_keyboard_message(
                    channel_id=str(channel_id), markdown=markdown, keyboard=keyboard
                )
            func = getattr(api, "post_message", None)
            if callable(func):
                return await func(
                    channel_id=str(channel_id),
                    ark=ark,
                    markdown=markdown,
                    keyboard=keyboard,
                    msg_id=msg_id,
                )
        return None

    # ---------------- 管理 ----------------
    @filter.command("管理面板")
    async def admin_panel(self, event: AstrMessageEvent):
        if not self.is_enabled():
            return
        if not self.require_admin(event):
            yield event.plain_result(self._admin_denied())
            return
        todos = self._get_todos(event)
        lines = [
            "🛠️ 待办管理面板",
            "━━━━━━━━━━━━━━━━",
            f"插件：{'启用' if self.is_enabled() else '停用'}",
            f"当前会话：{len(todos)} 个待办",
            f"管理员 QQ：{', '.join(sorted(self.admin_ids())) or '未配置'}",
            "",
            f"菜单方式：{self.cfg('menu_send_mode', 'auto')}",
            f"QQ 原生：{'开启' if self.cfg('qq_native_enabled', True) else '关闭'}",
            f"提醒：{'开启' if self.cfg('reminder_enabled', True) else '关闭'}",
            f"小游戏：{'开启' if self.cfg('games_enabled', True) else '关闭'}",
            f"签到：{'开启' if self.cfg('checkin_enabled', True) else '关闭'}",
            "",
            "管理设置：查看详细配置",
            "清空待办 + 确认清空：危险操作",
        ]
        yield event.plain_result("\n".join(lines))

    @filter.command("管理设置")
    async def settings(self, event: AstrMessageEvent):
        if not self.is_enabled():
            return
        if not self.require_admin(event):
            yield event.plain_result(self._admin_denied())
            return
        keys = [
            ("default_priority", "默认优先级"),
            ("max_todos", "最大待办数"),
            ("max_content_length", "单条长度"),
            ("menu_send_mode", "菜单方式"),
            ("reminder_send_mode", "提醒方式"),
            ("reminder_check_interval", "提醒检查秒数"),
            ("clear_require_confirm", "清空需确认"),
            ("clear_admin_only", "清空仅管理员"),
            ("games_enabled", "小游戏"),
            ("game_cooldown_seconds", "游戏冷却秒数"),
            ("game_daily_limit", "每日游戏次数"),
            ("checkin_enabled", "每日签到"),
            ("checkin_base_points", "签到基础积分"),
            ("qq_native_enabled", "QQ 原生消息"),
            ("qq_native_ark_enabled", "QQ Ark"),
            ("qq_native_keyboard_enabled", "QQ Keyboard"),
            ("auto_use_image_for_group", "群聊自动图片"),
        ]
        lines = ["⚙️ 插件详细配置", "━━━━━━━━━━━━━━━━"]
        for key, label in keys:
            lines.append(f"{label}：{self.cfg(key)}")
        lines += ["", "管理员 QQ：" + (", ".join(sorted(self.admin_ids())) or "未配置")]
        yield event.plain_result("\n".join(lines))

    # ---------------- 待办 ----------------
    @filter.command("待办", alias={"代办"})
    async def add_todo(self, event: AstrMessageEvent):
        if not self.is_enabled():
            return
        content = extract_command_tail(event, "待办", "代办")
        if not content:
            yield event.plain_result("❌ 用法：待办 内容 [高/中/低]")
            return
        max_len = max(20, int(self.cfg("max_content_length", 200)))
        tokens = content.split()
        priority = str(self.cfg("default_priority", "中"))
        if tokens and tokens[-1] in VALID_PRIORITIES:
            priority = tokens.pop()
            content = " ".join(tokens).strip()
        elif tokens and tokens[0] in VALID_PRIORITIES:
            priority = tokens.pop(0)
            content = " ".join(tokens).strip()
        if not content:
            yield event.plain_result("❌ 待办内容不能为空。")
            return
        if len(content) > max_len:
            yield event.plain_result(f"❌ 待办内容过长，最多 {max_len} 个字符。")
            return
        todos = self._get_todos(event)
        if len(todos) >= int(self.cfg("max_todos", 200)):
            yield event.plain_result("❌ 当前会话待办已达到上限。")
            return
        item = TodoItem(
            content=content,
            priority=priority,
            creator=str(event.get_sender_id() or ""),
            id=self._next_id(todos),
        )
        todos.append(item)
        self._set_todos(event, todos)
        yield event.plain_result(f"✅ 已添加待办 #{item.id}\n{self._format_item(item)}")

    @filter.command("待办列表", alias={"代办列表", "列表"})
    async def list_todos(self, event: AstrMessageEvent):
        todos = self._sorted(self._get_todos(event))
        if not todos:
            yield event.plain_result("📝 当前没有待办事项。")
            return
        done = sum(x.completed for x in todos)
        lines = ["📝 待办事项", "━━━━━━━━━━━━━━━━"]
        lines.extend(self._format_item(item, idx) for idx, item in enumerate(todos, 1))
        lines += ["━━━━━━━━━━━━━━━━", f"📊 已完成 {done}/{len(todos)}", "数字=当前列表序号；#数字=永久 ID"]
        yield event.plain_result("\n".join(lines))

    @filter.command("完成")
    async def complete_todo(self, event: AstrMessageEvent):
        token = extract_command_tail(event, "完成")
        todos = self._get_todos(event)
        item, error = self._resolve_item(todos, token)
        if not item:
            yield event.plain_result("❌ " + error)
            return
        if item.completed:
            yield event.plain_result(f"⚠️ #{item.id} 已经完成。")
            return
        item.completed = True
        item.completed_at = _now().isoformat(timespec="seconds")
        self._set_todos(event, todos)
        yield event.plain_result(f"🎉 已完成\n{self._format_item(item)}")

    @filter.command("撤销完成", alias={"未完成"})
    async def undo_complete(self, event: AstrMessageEvent):
        token = extract_command_tail(event, "撤销完成", "未完成")
        todos = self._get_todos(event)
        item, error = self._resolve_item(todos, token)
        if not item:
            yield event.plain_result("❌ " + error)
            return
        item.completed = False
        item.completed_at = None
        self._set_todos(event, todos)
        yield event.plain_result(f"↩️ 已恢复为未完成：#{item.id}")

    @filter.command("删除")
    async def delete_todo(self, event: AstrMessageEvent):
        token = extract_command_tail(event, "删除")
        todos = self._get_todos(event)
        item, error = self._resolve_item(todos, token)
        if not item:
            yield event.plain_result("❌ " + error)
            return
        todos.remove(item)
        self._set_todos(event, todos)
        yield event.plain_result(f"🗑️ 已删除 #{item.id}：{item.content}")

    @filter.command("编辑")
    async def edit_todo(self, event: AstrMessageEvent):
        token, content = parse_index_and_tail(event, "编辑")
        todos = self._get_todos(event)
        item, error = self._resolve_item(todos, token)
        if not item:
            yield event.plain_result("❌ " + error)
            return
        if not content:
            yield event.plain_result("❌ 用法：编辑 编号 新内容")
            return
        if len(content) > int(self.cfg("max_content_length", 200)):
            yield event.plain_result("❌ 新内容过长。")
            return
        item.content = content
        self._set_todos(event, todos)
        yield event.plain_result(f"✏️ 已修改 #{item.id}\n{self._format_item(item)}")

    @filter.command("改优先级")
    async def set_priority(self, event: AstrMessageEvent):
        token, priority = parse_index_and_tail(event, "改优先级")
        priority = priority.strip().split()[0] if priority.strip() else ""
        todos = self._get_todos(event)
        item, error = self._resolve_item(todos, token)
        if not item:
            yield event.plain_result("❌ " + error)
            return
        if priority not in VALID_PRIORITIES:
            yield event.plain_result("❌ 优先级只能是：高 / 中 / 低")
            return
        item.priority = priority
        self._set_todos(event, todos)
        yield event.plain_result(f"✅ #{item.id} 优先级已改为 {priority}。")

    # ---------------- 日期提醒/截止 ----------------
    async def _configure_time(self, event: AstrMessageEvent, commands: tuple[str, ...], field_name: str) -> str:
        token, raw_time = parse_index_and_tail(event, *commands)
        if not token or not raw_time:
            return f"❌ 用法：{commands[0]} 编号 时间\n例如：{commands[0]} 1 14:21\n也支持：{commands[0]} #12 明天 08:00"
        todos = self._get_todos(event)
        item, error = self._resolve_item(todos, token)
        if not item:
            return "❌ " + error
        when = parse_reminder_datetime(raw_time)
        if when is None:
            return "❌ 时间无法识别。支持：14:21、晚上8点半、今天 18:30、明天 08:00、30分钟后。"
        if field_name == "reminder_at":
            if item.completed:
                return "⚠️ 这个待办已经完成，不需要设置提醒。"
            item.reminder_at = when.isoformat(timespec="seconds")
            item.reminder_origin = str(event.unified_msg_origin)
            item.reminder_text = str(self.cfg("reminder_prefix", "⏰ 待办提醒"))
            message = f"⏰ 已为待办 #{item.id} 设置提醒：{when.strftime('%Y-%m-%d %H:%M')}\n{item.content}"
        else:
            item.due_at = when.isoformat(timespec="seconds")
            message = f"📌 已为待办 #{item.id} 设置截止时间：{when.strftime('%Y-%m-%d %H:%M')}\n{item.content}"
        self._set_todos(event, todos)
        return message

    @filter.command("提醒", alias={"定时提醒"})
    async def reminder(self, event: AstrMessageEvent):
        if not self.is_enabled() or not bool(self.cfg("reminder_enabled", True)):
            yield event.plain_result("ℹ️ 提醒功能当前未开启。")
            return
        result = await self._configure_time(event, ("提醒", "定时提醒"), "reminder_at")
        yield event.plain_result(result)

    @filter.command("取消提醒")
    async def cancel_reminder(self, event: AstrMessageEvent):
        token = extract_command_tail(event, "取消提醒")
        todos = self._get_todos(event)
        item, error = self._resolve_item(todos, token)
        if not item:
            yield event.plain_result("❌ " + error)
            return
        item.reminder_at = None
        item.reminder_origin = None
        item.reminder_text = None
        self._set_todos(event, todos)
        yield event.plain_result(f"✅ 已取消 #{item.id} 的提醒。")

    @filter.command("提醒列表")
    async def reminder_list(self, event: AstrMessageEvent):
        todos = [x for x in self._sorted(self._get_todos(event)) if x.reminder_at and not x.completed]
        if not todos:
            yield event.plain_result("⏰ 当前没有有效提醒。")
            return
        lines = ["⏰ 当前提醒", "━━━━━━━━━━━━━━━━"]
        lines.extend(f"{idx}. {item.content} · {self._format_dt(item.reminder_at)} · #{item.id}" for idx, item in enumerate(todos, 1))
        yield event.plain_result("\n".join(lines))

    @filter.command("截止")
    async def due(self, event: AstrMessageEvent):
        yield event.plain_result(await self._configure_time(event, ("截止",), "due_at"))

    @filter.command("取消截止")
    async def cancel_due(self, event: AstrMessageEvent):
        token = extract_command_tail(event, "取消截止")
        todos = self._get_todos(event)
        item, error = self._resolve_item(todos, token)
        if not item:
            yield event.plain_result("❌ " + error)
            return
        item.due_at = None
        self._set_todos(event, todos)
        yield event.plain_result(f"✅ 已取消 #{item.id} 的截止时间。")

    @filter.command("今日待办")
    async def today_todos(self, event: AstrMessageEvent):
        today = _now().date().isoformat()
        todos = self._sorted(self._get_todos(event))
        items = [x for x in todos if (x.created_at or "").startswith(today) or (x.due_at or "").startswith(today) or (x.completed_at or "").startswith(today)]
        if not items:
            yield event.plain_result("📅 今天还没有待办。")
            return
        yield event.plain_result("📅 今日待办\n━━━━━━━━━━━━━━━━\n" + "\n".join(self._format_item(x, i) for i, x in enumerate(items, 1)))

    @filter.command("逾期待办")
    async def overdue(self, event: AstrMessageEvent):
        now = _now()
        items = []
        for item in self._sorted(self._get_todos(event)):
            if item.completed or not item.due_at:
                continue
            try:
                if dt.datetime.fromisoformat(item.due_at) < now:
                    items.append(item)
            except ValueError:
                pass
        if not items:
            yield event.plain_result("✅ 暂无逾期未完成任务。")
            return
        yield event.plain_result("⚠️ 逾期待办\n━━━━━━━━━━━━━━━━\n" + "\n".join(self._format_item(x, i) for i, x in enumerate(items, 1)))

    @filter.command("搜索")
    async def search(self, event: AstrMessageEvent):
        keyword = extract_command_tail(event, "搜索").strip()
        if not keyword:
            yield event.plain_result("❌ 用法：搜索 关键词")
            return
        items = [x for x in self._sorted(self._get_todos(event)) if keyword.lower() in x.content.lower()]
        if not items:
            yield event.plain_result("🔎 没有找到匹配的待办。")
            return
        yield event.plain_result("🔎 搜索结果\n━━━━━━━━━━━━━━━━\n" + "\n".join(self._format_item(x, i) for i, x in enumerate(items, 1)))

    @filter.command("我的待办")
    async def my_todos(self, event: AstrMessageEvent):
        uid = str(event.get_sender_id() or "")
        items = [x for x in self._sorted(self._get_todos(event)) if x.creator == uid or x.assigned_to == uid]
        if not items:
            yield event.plain_result("👤 你当前没有创建/认领的待办。")
            return
        yield event.plain_result("👤 我的待办\n━━━━━━━━━━━━━━━━\n" + "\n".join(self._format_item(x, i) for i, x in enumerate(items, 1)))

    @filter.command("待办统计", alias={"完成率"})
    async def stats(self, event: AstrMessageEvent):
        todos = self._get_todos(event)
        if not todos:
            yield event.plain_result("📊 当前没有待办，完成率 0%。")
            return
        done = sum(x.completed for x in todos)
        high = sum(x.priority == "高" and not x.completed for x in todos)
        reminders = sum(bool(x.reminder_at) and not x.completed for x in todos)
        overdue_count = 0
        now = _now()
        for x in todos:
            if not x.completed and x.due_at:
                try:
                    overdue_count += dt.datetime.fromisoformat(x.due_at) < now
                except ValueError:
                    pass
        rate = done / len(todos) * 100
        yield event.plain_result(
            f"📊 待办统计\n━━━━━━━━━━━━━━━━\n总任务：{len(todos)}\n已完成：{done}\n未完成：{len(todos)-done}\n完成率：{rate:.1f}%\n高优先级未完成：{high}\n有效提醒：{reminders}\n逾期任务：{overdue_count}"
        )

    # ---------------- 自助代办 ----------------
    @filter.command("自助代办")
    async def auto_assign(self, event: AstrMessageEvent):
        if not bool(self.cfg("auto_assign", True)):
            yield event.plain_result("ℹ️ 自助代办功能当前未开启。")
            return
        todos = self._get_todos(event)
        pending = [x for x in todos if not x.completed and not x.assigned_to]
        if not pending:
            yield event.plain_result("ℹ️ 当前没有可认领的未分配任务。")
            return
        item = random.choice(pending)
        item.assigned_to = str(event.get_sender_id() or "")
        self._set_todos(event, todos)
        yield event.plain_result(f"🤝 已为你认领：\n{self._format_item(item)}")

    # ---------------- 清空 ----------------
    @filter.command("清空待办")
    async def clear_todos(self, event: AstrMessageEvent):
        if bool(self.cfg("clear_admin_only", False)) and not self.is_admin(event):
            yield event.plain_result(self._admin_denied())
            return
        todos = self._get_todos(event)
        if not todos:
            yield event.plain_result("⚠️ 当前没有待办事项。")
            return
        if not bool(self.cfg("clear_require_confirm", True)):
            self._set_todos(event, [])
            yield event.plain_result(f"✅ 已清空 {len(todos)} 个待办。")
            return
        seconds = max(10, int(self.cfg("clear_confirm_seconds", 60)))
        self._clear_confirmations[self._session_key(event)] = _now().timestamp() + seconds
        yield event.plain_result(f"⚠️ 即将清空当前会话的 {len(todos)} 个待办。\n请在 {seconds} 秒内回复：确认清空\n取消：取消清空")

    @filter.command("确认清空")
    async def confirm_clear(self, event: AstrMessageEvent):
        key = self._session_key(event)
        expires = self._clear_confirmations.get(key, 0)
        if _now().timestamp() > expires:
            self._clear_confirmations.pop(key, None)
            yield event.plain_result("⌛ 清空确认已失效，请重新发送：清空待办")
            return
        if bool(self.cfg("clear_admin_only", False)) and not self.is_admin(event):
            yield event.plain_result(self._admin_denied())
            return
        todos = self._get_todos(event)
        self._clear_confirmations.pop(key, None)
        self._set_todos(event, [])
        yield event.plain_result(f"✅ 已清空 {len(todos)} 个待办。")

    @filter.command("取消清空")
    async def cancel_clear(self, event: AstrMessageEvent):
        self._clear_confirmations.pop(self._session_key(event), None)
        yield event.plain_result("✅ 已取消清空操作。")

    # ---------------- 提醒调度 ----------------
    async def _send_active_reminder(self, origin: str, item: TodoItem) -> None:
        text = f"{item.reminder_text or self.cfg('reminder_prefix','⏰ 待办提醒')}\n━━━━━━━━━━━━━━━━\n{PRIORITY_ICONS.get(item.priority,'⚪')} {item.content}\n永久 ID：#{item.id}"
        mode = str(self.cfg("reminder_send_mode", "auto")).lower()
        if mode == "auto":
            mode = "image" if bool(self.cfg("auto_use_image_for_group", True)) else "text"
        if mode not in VALID_MODES:
            mode = "text"
        try:
            if mode == "markdown":
                chain = MessageChain().message(text).use_markdown(True)
                await self.context.send_message(origin, chain)
                return
            if mode in {"image", "both"}:
                body = f"<div style='font-size:24px;font-weight:700;'>{escape(item.content)}</div><div style='margin-top:10px;color:#7c8596;font-size:16px;'>优先级：{escape(item.priority)}　·　ID：#{item.id}</div>"
                path = await self._render_card(str(item.reminder_text or "⏰ 待办提醒"), "该处理这项任务了", body)
                if mode == "image" and path:
                    await self.context.send_message(origin, MessageChain().file_image(path))
                    return
                if mode == "both" and path:
                    chain = MessageChain().message(text)
                    chain.file_image(path)
                    await self.context.send_message(origin, chain)
                    return
            await self.context.send_message(origin, MessageChain().message(text))
        except Exception as exc:
            logger.warning("[todo] 主动提醒发送失败，文本兜底：%s", exc)
            try:
                await self.context.send_message(origin, MessageChain().message(text))
            except Exception:
                pass

    async def _reminder_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(max(10, int(self.cfg("reminder_check_interval", 20))))
                if not self.is_enabled() or not bool(self.cfg("reminder_enabled", True)):
                    continue
                now = _now()
                changed = False
                for raw_items in list(self.data.setdefault("sessions", {}).values()):
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
                            raw["reminder_origin"] = None
                            raw["reminder_text"] = None
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

    # ---------------- 小游戏/积分 ----------------
    def _player_key(self, event: AstrMessageEvent) -> str:
        return f"{self._session_key(event)}|{str(event.get_sender_id() or 'unknown')}"

    def _stats_for(self, event: AstrMessageEvent) -> dict[str, Any]:
        stats = self.data.setdefault("game_stats", {})
        key = self._player_key(event)
        entry = stats.setdefault(key, {"points": 0, "wins": 0, "plays": 0, "checkins": 0})
        return entry

    def _game_allowed(self, event: AstrMessageEvent) -> tuple[bool, str]:
        if not bool(self.cfg("games_enabled", True)):
            return False, "ℹ️ 小游戏当前已关闭。"
        now = _now().timestamp()
        key = self._player_key(event)
        remain = self._game_cooldowns.get(key, 0) - now
        if remain > 0:
            return False, f"⏳ 请等待 {int(remain) + 1} 秒再玩。"
        today = _now().date().isoformat()
        entry = self._stats_for(event)
        if str(entry.get("game_day")) != today:
            entry["game_day"] = today
            entry["game_plays_today"] = 0
        if int(entry.get("game_plays_today", 0)) >= int(self.cfg("game_daily_limit", 50)):
            return False, "⛔ 今日小游戏次数已达到上限。"
        cooldown = max(0, int(self.cfg("game_cooldown_seconds", 3)))
        self._game_cooldowns[key] = now + cooldown
        entry["game_plays_today"] = int(entry.get("game_plays_today", 0)) + 1
        entry["plays"] = int(entry.get("plays", 0)) + 1
        return True, ""

    @filter.command("小游戏", alias={"游戏", "玩游戏"})
    async def games_menu(self, event: AstrMessageEvent):
        if not bool(self.cfg("games_enabled", True)):
            yield event.plain_result("ℹ️ 小游戏当前已关闭。")
            return
        yield event.plain_result("🎮 小游戏中心\n━━━━━━━━━━━━━━━━\n猜数字：猜 1~100\n石头剪刀布：石头剪刀布\n掷骰子：[掷骰子 20]\n猜硬币：猜硬币 正面/反面\n幸运抽签：幸运抽签\n\n签到：每日签到\n积分榜：查看积分排名")

    @filter.command("猜数字", alias={"猜"})
    async def guess_number(self, event: AstrMessageEvent):
        ok, msg = self._game_allowed(event)
        if not ok:
            yield event.plain_result(msg)
            return
        games = self.data.setdefault("game_stats", {})
        key = self._player_key(event)
        entry = games.setdefault(key, {"points": 0, "wins": 0, "plays": 0})
        raw = extract_command_tail(event, "猜数字", "猜").strip()
        state = entry.get("guess_number")
        if not isinstance(state, dict) or not state.get("active"):
            target = random.randint(1, 100)
            entry["guess_number"] = {"active": True, "target": target, "attempts": 0}
            entry["game_plays_today"] = max(0, int(entry.get("game_plays_today", 1)) - 1)
            self._save_data()
            yield event.plain_result("🎯 猜数字游戏开始！\n我已经想好 1~100 中的一个数字。\n发送：猜数字 50")
            return
        try:
            value = int(raw)
        except ValueError:
            yield event.plain_result("❌ 请输入 1~100 的整数，例如：猜数字 50")
            return
        if not 1 <= value <= 100:
            yield event.plain_result("❌ 数字范围是 1~100。")
            return
        state["attempts"] = int(state.get("attempts", 0)) + 1
        target = int(state["target"])
        if value == target:
            attempts = int(state["attempts"])
            reward = max(5, 25 - attempts * 2)
            entry["points"] = int(entry.get("points", 0)) + reward
            entry["wins"] = int(entry.get("wins", 0)) + 1
            entry["guess_number"] = {"active": False}
            self._save_data()
            yield event.plain_result(f"🎉 猜中了！答案就是 {target}。\n尝试次数：{attempts}\n获得积分：+{reward}")
            return
        hint = "大了" if value > target else "小了"
        self._save_data()
        yield event.plain_result(f"🔎 {hint}！再试一次。")

    @filter.command("石头剪刀布", alias={"猜拳"})
    async def rps_game(self, event: AstrMessageEvent):
        ok, msg = self._game_allowed(event)
        if not ok:
            yield event.plain_result(msg)
            return
        raw = extract_command_tail(event, "石头剪刀布", "猜拳").strip()
        if raw not in RPS:
            yield event.plain_result("✊ 用法：石头剪刀布 石头 / 剪刀 / 布")
            return
        bot = random.choice(list(RPS.keys()))
        entry = self._stats_for(event)
        if raw == bot:
            result, points = "平局", 2
        elif RPS[raw] == bot:
            result, points = "胜利", 8
            entry["wins"] = int(entry.get("wins", 0)) + 1
        else:
            result, points = "失败", 0
        entry["points"] = int(entry.get("points", 0)) + points
        self._save_data()
        yield event.plain_result(f"✊ 你出：{raw}\n🤖 我出：{bot}\n结果：{result}\n积分：+{points}")

    @filter.command("掷骰子", alias={"骰子"})
    async def dice_game(self, event: AstrMessageEvent):
        ok, msg = self._game_allowed(event)
        if not ok:
            yield event.plain_result(msg)
            return
        raw = extract_command_tail(event, "掷骰子", "骰子").strip()
        sides = 6
        if raw:
            try:
                sides = max(2, min(100, int(raw.split()[0])))
            except ValueError:
                yield event.plain_result("❌ 用法：掷骰子 [面数]，例如：掷骰子 20")
                return
        value = random.randint(1, sides)
        reward = 10 if value == sides else 2
        entry = self._stats_for(event)
        entry["points"] = int(entry.get("points", 0)) + reward
        self._save_data()
        yield event.plain_result(f"🎲 {sides} 面骰结果：{value}\n积分：+{reward}")

    @filter.command("猜硬币")
    async def coin_game(self, event: AstrMessageEvent):
        ok, msg = self._game_allowed(event)
        if not ok:
            yield event.plain_result(msg)
            return
        guess = extract_command_tail(event, "猜硬币").strip()
        if guess not in COIN_VALUES:
            yield event.plain_result("🪙 用法：猜硬币 正面 / 反面")
            return
        actual = random.choice(list(COIN_VALUES))
        entry = self._stats_for(event)
        points = 6 if guess == actual else 0
        if points:
            entry["wins"] = int(entry.get("wins", 0)) + 1
        entry["points"] = int(entry.get("points", 0)) + points
        self._save_data()
        yield event.plain_result(f"🪙 硬币结果：{actual}\n结果：{'猜对了！' if points else '猜错了。'}\n积分：+{points}")

    @filter.command("幸运抽签")
    async def lucky_draw(self, event: AstrMessageEvent):
        ok, msg = self._game_allowed(event)
        if not ok:
            yield event.plain_result(msg)
            return
        prizes = [
            ("SSR·超级幸运", 30),
            ("SR·今天很顺", 15),
            ("R·普通好运", 6),
            ("N·平平淡淡", 2),
        ]
        weights = [2, 8, 25, 65]
        prize = random.choices(prizes, weights=weights, k=1)[0]
        entry = self._stats_for(event)
        entry["points"] = int(entry.get("points", 0)) + prize[1]
        self._save_data()
        yield event.plain_result(f"🍀 今日签运：{prize[0]}\n获得积分：+{prize[1]}")

    @filter.command("签到")
    async def checkin(self, event: AstrMessageEvent):
        if not bool(self.cfg("checkin_enabled", True)):
            yield event.plain_result("ℹ️ 每日签到当前已关闭。")
            return
        key = self._player_key(event)
        today = _now().date().isoformat()
        checkins = self.data.setdefault("daily_checkins", {})
        if checkins.get(key) == today:
            yield event.plain_result("📅 今天已经签到过了，明天再来吧。")
            return
        checkins[key] = today
        entry = self._stats_for(event)
        reward = max(1, int(self.cfg("checkin_base_points", 10))) + random.randint(0, max(0, int(self.cfg("checkin_random_bonus", 10))))
        entry["points"] = int(entry.get("points", 0)) + reward
        entry["checkins"] = int(entry.get("checkins", 0)) + 1
        self._save_data()
        yield event.plain_result(f"✅ 签到成功！获得积分：+{reward}\n当前积分：{entry['points']}")

    @filter.command("积分榜")
    async def leaderboard(self, event: AstrMessageEvent):
        prefix = self._session_key(event) + "|"
        rows = []
        for key, entry in self.data.setdefault("game_stats", {}).items():
            if not key.startswith(prefix):
                continue
            uid = key.rsplit("|", 1)[-1]
            rows.append((int(entry.get("points", 0)), uid, entry))
        rows.sort(reverse=True, key=lambda x: x[0])
        if not rows:
            yield event.plain_result("🏆 当前还没有积分记录。")
            return
        lines = ["🏆 本会话积分榜", "━━━━━━━━━━━━━━━━"]
        for idx, (points, uid, entry) in enumerate(rows[:10], 1):
            lines.append(f"{idx}. {uid} · {points} 分 · 胜场 {int(entry.get('wins',0))}")
        yield event.plain_result("\n".join(lines))

    async def terminate(self):
        if self._scheduler_task:
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                pass
            self._scheduler_task = None
