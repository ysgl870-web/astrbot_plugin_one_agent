"""AstrBot 待办事项与提醒插件

兼容 AstrBot 4.16+，并尽量只使用官方公开的 Star / filter / Context API。

核心修复：
1. _conf_schema.json 使用 AstrBot 支持的 "string" 类型，而不是 "str"。
2. 定时提醒不再把提醒配置写进待办列表，避免覆盖任务数据。
3. 使用 unified_msg_origin 作为数据作用域，群聊/私聊/不同平台互不串数据。
4. 清空待办使用会话确认，不再“提示确认但实际立即清空”。
5. JSON 原子保存 + 损坏文件备份 + 旧版本数据迁移。
6. 插件卸载时会停止后台提醒任务。

功能：
- 待办、待办列表、今日待办、逾期待办、我的待办
- 完成、撤销完成、删除、编辑、优先级
- 搜索、统计、完成率
- 截止时间、单项提醒、每日提醒
- 自助代办、放弃代办
- 菜单/帮助、管理设置
"""

from __future__ import annotations

import asyncio
import copy
import datetime as dt
import json
import os
import re
import tempfile
from typing import Any, Dict, List, Optional, Tuple

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star


PLUGIN_ID = "astrbot_plugin_one_agent"
DATA_VERSION = 2

PRIORITIES = ("高", "中", "低")
PRIORITY_ICONS = {"高": "🔴", "中": "🟡", "低": "🟢"}
PRIORITY_ORDER = {"高": 0, "中": 1, "低": 2}

_TIME_RE = re.compile(r"^(?:[01]?\d|2[0-3])(?::|：)([0-5]\d)$")
_FULL_DT_RE = re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?:\s+|T)(\d{1,2})(?::|：)(\d{2})$")
_DURATION_RE = re.compile(r"^(\d+)\s*(分钟|分|小时|时|天)$", re.I)


def _data_dir() -> str:
    """获取插件专属数据目录。失败时再回退到 AstrBot 常见目录。"""
    try:
        from astrbot.api.star import StarTools

        path = os.fspath(StarTools.get_data_dir(PLUGIN_ID))
        os.makedirs(path, exist_ok=True)
        return path
    except Exception as exc:
        logger.warning(f"[todo] StarTools 获取数据目录失败，使用回退路径: {exc}")
        path = os.path.join("AstrBot", "data", "plugin_data", PLUGIN_ID)
        os.makedirs(path, exist_ok=True)
        return path


def _now() -> dt.datetime:
    return dt.datetime.now()


def _iso(value: Optional[dt.datetime]) -> Optional[str]:
    return value.isoformat(timespec="seconds") if value else None


def _parse_iso(value: Any) -> Optional[dt.datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return dt.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _parse_clock(text: str) -> Optional[Tuple[int, int]]:
    """解析 HH:MM、晚上8点、下午2点等。"""
    s = text.strip().replace("时", "点")
    m = _TIME_RE.match(s)
    if m:
        hour = int(s.split(":")[0].split("：")[0])
        minute = int(m.group(1))
        return hour, minute

    patterns = [
        (re.compile(r"^(?:凌晨|早上)(\d{1,2})点(?:([0-5]\d)分?)?$"), 0),
        (re.compile(r"^(?:下午|午后)(\d{1,2})点(?:([0-5]\d)分?)?$"), 12),
        (re.compile(r"^晚上(\d{1,2})点(?:([0-5]\d)分?)?$"), 12),
        (re.compile(r"^(\d{1,2})点(?:([0-5]\d)分?)?$"), 0),
    ]
    for pattern, offset in patterns:
        match = pattern.match(s)
        if match:
            hour = int(match.group(1)) + offset
            if offset and int(match.group(1)) == 12:
                hour = 12
            minute = int(match.group(2) or 0)
            if 0 <= hour <= 23:
                return hour, minute
    return None


def _parse_remind_time(text: str, base: Optional[dt.datetime] = None) -> Optional[dt.datetime]:
    """支持：10分钟、2小时、1天、14:30、2026-10-05 14:30。"""
    s = text.strip()
    base = base or _now()

    duration = _DURATION_RE.match(s)
    if duration:
        amount = int(duration.group(1))
        unit = duration.group(2)
        if unit in ("分钟", "分"):
            return base + dt.timedelta(minutes=amount)
        if unit in ("小时", "时"):
            return base + dt.timedelta(hours=amount)
        return base + dt.timedelta(days=amount)

    full = _FULL_DT_RE.match(s)
    if full:
        y, m, d, h, minute = map(int, full.groups())
        try:
            return dt.datetime(y, m, d, h, minute)
        except ValueError:
            return None

    clock = _parse_clock(s)
    if clock:
        hour, minute = clock
        candidate = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= base:
            candidate += dt.timedelta(days=1)
        return candidate
    return None


class TodoItem:
    def __init__(
        self,
        content: str,
        priority: str = "中",
        creator: str = "",
        assigned_to: str = "",
        created_at: Optional[str] = None,
        completed: bool = False,
        completed_at: Optional[str] = None,
        due_at: Optional[str] = None,
        remind_at: Optional[str] = None,
    ) -> None:
        self.id = 0
        self.content = content.strip()
        self.priority = priority if priority in PRIORITIES else "中"
        self.creator = creator.strip()
        self.assigned_to = assigned_to.strip()
        self.created_at = created_at or _now().isoformat(timespec="seconds")
        self.completed = bool(completed)
        self.completed_at = completed_at
        self.due_at = due_at
        self.remind_at = remind_at
        self.reminder_sent_for = None

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "TodoItem":
        item = cls(
            content=str(raw.get("content", "")),
            priority=str(raw.get("priority", "中")),
            creator=str(raw.get("creator", "")),
            assigned_to=str(raw.get("assigned_to", "")),
            created_at=raw.get("created_at") or None,
            completed=bool(raw.get("completed", False)),
            completed_at=raw.get("completed_at") or None,
            due_at=raw.get("due_at") or None,
            remind_at=raw.get("remind_at") or None,
        )
        try:
            item.id = int(raw.get("id", 0))
        except (TypeError, ValueError):
            item.id = 0
        item.reminder_sent_for = raw.get("reminder_sent_for")
        return item

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
            "due_at": self.due_at,
            "remind_at": self.remind_at,
            "reminder_sent_for": self.reminder_sent_for,
        }

    def overdue(self, now: Optional[dt.datetime] = None) -> bool:
        if self.completed:
            return False
        due = _parse_iso(self.due_at)
        return bool(due and due < (now or _now()))


class TodoManager(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config
        self.data_file = os.path.join(_data_dir(), "todo_data.json")
        self.data: Dict[str, Any] = self._load_data()
        self._lock = asyncio.Lock()
        self._reminder_task: Optional[asyncio.Task] = None
        try:
            self._reminder_task = asyncio.create_task(self._reminder_loop())
        except RuntimeError:
            logger.warning("[todo] 当前没有可用事件循环，后台提醒将不会启动")
        logger.info(f"[todo] 待办插件已加载，数据文件: {self.data_file}")

    # ---------------- 配置 ----------------
    def _cfg(self, key: str, default: Any) -> Any:
        try:
            if hasattr(self.config, "get"):
                value = self.config.get(key, default)
            else:
                value = getattr(self.config, key, default)
            return default if value is None else value
        except Exception:
            try:
                value = getattr(self.config, key)
                return default if value is None else value
            except Exception:
                return default

    def _enabled(self) -> bool:
        return bool(self._cfg("enabled", True))

    # ---------------- 数据存储 ----------------
    def _default_data(self) -> Dict[str, Any]:
        return {"version": DATA_VERSION, "scopes": {}}

    def _load_data(self) -> Dict[str, Any]:
        if not os.path.exists(self.data_file):
            return self._default_data()
        try:
            with open(self.data_file, "r", encoding="utf-8") as f:
                raw = json.load(f)
            migrated = self._migrate_data(raw)
            if migrated != raw:
                self.data = migrated
                self._save_data_sync()
            return migrated
        except Exception as exc:
            backup = f"{self.data_file}.broken.{_now().strftime('%Y%m%d%H%M%S')}"
            try:
                os.replace(self.data_file, backup)
            except Exception:
                backup = "未能创建损坏文件备份"
            logger.error(f"[todo] 读取数据失败，已尝试备份: {backup}; {exc}")
            return self._default_data()

    def _migrate_data(self, raw: Any) -> Dict[str, Any]:
        """将旧版 {scope: [todo...]} 结构迁移为 v2。"""
        if isinstance(raw, dict) and raw.get("version") == DATA_VERSION and isinstance(raw.get("scopes"), dict):
            return raw

        result = self._default_data()
        if not isinstance(raw, dict):
            return result

        old_scopes = raw.get("scopes") if isinstance(raw.get("scopes"), dict) else raw
        for scope, value in old_scopes.items():
            scope_key = str(scope)
            if not isinstance(value, list):
                continue
            todos: List[Dict[str, Any]] = []
            daily_time: Optional[str] = None
            for entry in value:
                if not isinstance(entry, dict):
                    continue
                # 原插件的“定时提醒”错误地把配置写成 id=999/time/channel。
                # 有效待办必须至少存在 content；旧提醒配置则只尝试迁移时间。
                content = str(entry.get("content", "")).strip()
                if content:
                    item = copy.deepcopy(entry)
                    item.pop("time", None)
                    item.pop("channel", None)
                    item.setdefault("due_at", None)
                    item.setdefault("remind_at", None)
                    todos.append(item)
                else:
                    time_value = entry.get("time")
                    if isinstance(time_value, str) and _parse_clock(time_value):
                        daily_time = time_value
            scope_payload = {
                "todos": todos,
                "daily_reminder": {
                    "enabled": bool(daily_time),
                    "time": daily_time or str(self._cfg("default_daily_reminder_time", "18:00")),
                    "last_sent": None,
                },
            }
            result["scopes"][scope_key] = scope_payload
        return result

    def _save_data_sync(self) -> None:
        """原子写文件，避免进程异常时把 JSON 写成半截。"""
        os.makedirs(os.path.dirname(self.data_file), exist_ok=True)
        directory = os.path.dirname(self.data_file) or "."
        fd, tmp = tempfile.mkstemp(prefix=".todo_", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.data_file)
        finally:
            try:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            except OSError:
                pass

    async def _save_data(self) -> None:
        async with self._lock:
            self._save_data_sync()

    def _scope_key(self, event: AstrMessageEvent) -> str:
        try:
            value = getattr(event, "unified_msg_origin", None)
            if value:
                return str(value)
        except Exception:
            pass
        try:
            return f"fallback:{event.get_group_id()}"
        except Exception:
            return "fallback:private"

    def _user_id(self, event: AstrMessageEvent) -> str:
        try:
            return str(event.get_sender_id())
        except Exception:
            return "unknown"

    def _user_name(self, event: AstrMessageEvent) -> str:
        try:
            name = event.get_sender_name()
            return str(name).strip() if name else "用户"
        except Exception:
            return "用户"

    def _scope(self, key: str) -> Dict[str, Any]:
        scopes = self.data.setdefault("scopes", {})
        payload = scopes.setdefault(
            key,
            {
                "todos": [],
                "daily_reminder": {
                    "enabled": False,
                    "time": str(self._cfg("default_daily_reminder_time", "18:00")),
                    "last_sent": None,
                },
            },
        )
        payload.setdefault("todos", [])
        payload.setdefault("daily_reminder", {"enabled": False, "time": "18:00", "last_sent": None})
        return payload

    def _todos(self, key: str) -> List[TodoItem]:
        payload = self._scope(key)
        result: List[TodoItem] = []
        for raw in payload.get("todos", []):
            if not isinstance(raw, dict):
                continue
            try:
                item = TodoItem.from_dict(raw)
                if not item.content:
                    continue
                result.append(item)
            except Exception:
                continue
        return result

    def _write_todos(self, key: str, todos: List[TodoItem]) -> None:
        payload = self._scope(key)
        payload["todos"] = [item.to_dict() for item in todos]

    def _next_id(self, todos: List[TodoItem]) -> int:
        return max((item.id for item in todos), default=0) + 1

    def _find(self, todos: List[TodoItem], todo_id: int) -> Optional[TodoItem]:
        return next((x for x in todos if x.id == todo_id), None)

    def _sort(self, todos: List[TodoItem]) -> List[TodoItem]:
        now = _now()

        def key(item: TodoItem):
            due = _parse_iso(item.due_at)
            return (
                item.completed,
                0 if item.overdue(now) else 1,
                PRIORITY_ORDER.get(item.priority, 1),
                due or dt.datetime.max,
                item.id,
            )

        return sorted(todos, key=key)

    def _format_item(self, item: TodoItem, include_id: bool = False) -> str:
        status = "✅" if item.completed else ("⚠️" if item.overdue() else "⬜")
        owner = f" · 👤{item.creator}" if item.creator else ""
        assigned = f" · 🤝{item.assigned_to}" if item.assigned_to else ""
        due = ""
        if item.due_at:
            due_dt = _parse_iso(item.due_at)
            due = f" · 截止 {due_dt.strftime('%m-%d %H:%M')}" if due_dt else ""
        remind = ""
        if item.remind_at and not item.completed:
            remind_dt = _parse_iso(item.remind_at)
            if remind_dt:
                remind = f" · ⏰{remind_dt.strftime('%m-%d %H:%M')}"
        prefix = f"#{item.id} " if include_id else ""
        return f"{status} {prefix}{PRIORITY_ICONS.get(item.priority, '⚪')} {item.content}{owner}{assigned}{due}{remind}"

    def _parse_priority_and_content(self, args: str) -> Tuple[str, str]:
        text = args.strip()
        priority = str(self._cfg("default_priority", "中"))
        if priority not in PRIORITIES:
            priority = "中"
        m = re.match(r"^(高|中|低)(?:\s+|$)(.*)$", text, re.S)
        if m:
            priority = m.group(1)
            text = m.group(2).strip()
        return priority, text

    def _help_text(self) -> str:
        return (
            "📋 待办事项助手\n"
            "────────────\n"
            "待办 <内容> [高/中/低]   添加待办\n"
            "待办列表 [全部/未完成/已完成]   查看\n"
            "完成 <编号>              标记完成\n"
            "撤销完成 <编号>          恢复未完成\n"
            "编辑 <编号> <内容>       修改内容\n"
            "优先级 <编号> 高/中/低    修改优先级\n"
            "截止 <编号> 2026-10-06 18:00   设置截止时间\n"
            "提醒 <编号> 30分钟/18:30  设置单项提醒\n"
            "取消提醒 <编号>          取消单项提醒\n"
            "今日待办 / 逾期待办 / 我的待办\n"
            "搜索待办 <关键词>        搜索任务\n"
            "完成率 / 待办统计        查看统计\n"
            "删除 <编号>              删除待办\n"
            "清空待办                 需要再次发送“确认清空”\n"
            "自助代办 / 放弃代办 <编号>  认领/放弃任务\n"
            "每日提醒 18:00           设置每天提醒\n"
            "取消每日提醒             关闭每天提醒\n"
            "代办菜单                 打开本帮助\n"
            "────────────\n"
            "提示：所有数据按当前会话隔离，群聊和私聊不会串库。"
        )

    async def _guard(self, event: AstrMessageEvent) -> Optional[str]:
        if not self._enabled():
            return "⚠️ 待办插件当前已在配置中停用。"
        return None

    # ---------------- 指令 ----------------
    @filter.command("代办菜单", alias={"待办帮助", "todo帮助"})
    async def menu(self, event: AstrMessageEvent):
        err = await self._guard(event)
        if err:
            yield event.plain_result(err)
            return
        yield event.plain_result(self._help_text())

    @filter.command("待办")
    async def add_todo(self, event: AstrMessageEvent, args: str = ""):
        err = await self._guard(event)
        if err:
            yield event.plain_result(err)
            return
        priority, content = self._parse_priority_and_content(args)
        if not content:
            yield event.plain_result("❌ 请输入内容，例如：待办 高 完成报告")
            return

        scope = self._scope_key(event)
        todos = self._todos(scope)
        max_todos = max(1, int(self._cfg("max_todos", 100)))
        if len(todos) >= max_todos:
            yield event.plain_result(f"❌ 当前会话已有 {len(todos)} 个待办，已达到配置上限 {max_todos} 个。")
            return

        item = TodoItem(content, priority, self._user_name(event))
        item.id = self._next_id(todos)
        todos.append(item)
        self._write_todos(scope, todos)
        await self._save_data()
        yield event.plain_result(f"✅ 已添加待办\n{self._format_item(item, True)}")

    @filter.command("待办列表", alias={"todo列表", "待办清单"})
    async def list_todos(self, event: AstrMessageEvent, args: str = ""):
        err = await self._guard(event)
        if err:
            yield event.plain_result(err)
            return
        scope = self._scope_key(event)
        todos = self._todos(scope)
        mode = args.strip() or "全部"
        if mode not in {"全部", "未完成", "已完成"}:
            yield event.plain_result("❌ 用法：待办列表 / 待办列表 未完成 / 待办列表 已完成")
            return
        if mode == "未完成":
            todos = [x for x in todos if not x.completed]
        elif mode == "已完成":
            todos = [x for x in todos if x.completed]
        if not todos:
            yield event.plain_result(f"📝 没有符合条件的待办（筛选：{mode}）。")
            return

        all_todos = self._todos(scope)
        completed = sum(1 for x in all_todos if x.completed)
        lines = [f"📝 待办列表 · {mode}", "────────────"]
        lines.extend(self._format_item(x, True) for x in self._sort(todos))
        lines.extend(["────────────", f"📊 进度：{completed}/{len(all_todos)}"])
        yield event.plain_result("\n".join(lines))

    @filter.command("完成")
    async def complete_todo(self, event: AstrMessageEvent, args: str = ""):
        err = await self._guard(event)
        if err:
            yield event.plain_result(err)
            return
        try:
            todo_id = int(args.strip())
        except ValueError:
            yield event.plain_result("❌ 用法：完成 <编号>")
            return
        scope = self._scope_key(event)
        todos = self._todos(scope)
        item = self._find(todos, todo_id)
        if not item:
            yield event.plain_result(f"❌ 未找到待办 #{todo_id}")
            return
        if item.completed:
            yield event.plain_result(f"⚠️ 待办 #{todo_id} 已完成。")
            return
        item.completed = True
        item.completed_at = _iso(_now())
        item.remind_at = None
        self._write_todos(scope, todos)
        await self._save_data()
        yield event.plain_result(f"🎉 已完成！\n{self._format_item(item, True)}")

    @filter.command("撤销完成", alias={"取消完成", "恢复待办"})
    async def uncomplete_todo(self, event: AstrMessageEvent, args: str = ""):
        err = await self._guard(event)
        if err:
            yield event.plain_result(err)
            return
        try:
            todo_id = int(args.strip())
        except ValueError:
            yield event.plain_result("❌ 用法：撤销完成 <编号>")
            return
        scope = self._scope_key(event)
        todos = self._todos(scope)
        item = self._find(todos, todo_id)
        if not item:
            yield event.plain_result(f"❌ 未找到待办 #{todo_id}")
            return
        item.completed = False
        item.completed_at = None
        self._write_todos(scope, todos)
        await self._save_data()
        yield event.plain_result(f"↩️ 已恢复未完成\n{self._format_item(item, True)}")

    @filter.command("删除")
    async def delete_todo(self, event: AstrMessageEvent, args: str = ""):
        err = await self._guard(event)
        if err:
            yield event.plain_result(err)
            return
        try:
            todo_id = int(args.strip())
        except ValueError:
            yield event.plain_result("❌ 用法：删除 <编号>")
            return
        scope = self._scope_key(event)
        todos = self._todos(scope)
        item = self._find(todos, todo_id)
        if not item:
            yield event.plain_result(f"❌ 未找到待办 #{todo_id}")
            return
        todos.remove(item)
        self._write_todos(scope, todos)
        await self._save_data()
        yield event.plain_result(f"🗑️ 已删除 #{todo_id}：{item.content}")

    def _clear_pending(self) -> Dict[str, float]:
        return getattr(self, "_clear_pending_map", None) or {}

    @filter.command("清空待办")
    async def clear_todos(self, event: AstrMessageEvent):
        err = await self._guard(event)
        if err:
            yield event.plain_result(err)
            return
        scope = self._scope_key(event)
        todos = self._todos(scope)
        if not todos:
            yield event.plain_result("⚠️ 当前没有待办事项。")
            return
        self._clear_pending_map = getattr(self, "_clear_pending_map", {})
        self._clear_pending_map[scope] = _now().timestamp()
        yield event.plain_result(
            f"⚠️ 你准备清空当前会话的 {len(todos)} 个待办。\n"
            "如确认，请在 60 秒内发送：确认清空\n"
            "发送其他消息不会执行清空。"
        )

    @filter.command("确认清空")
    async def confirm_clear(self, event: AstrMessageEvent):
        err = await self._guard(event)
        if err:
            yield event.plain_result(err)
            return
        scope = self._scope_key(event)
        pending = getattr(self, "_clear_pending_map", {}).get(scope)
        if not pending or (_now().timestamp() - pending > 60):
            getattr(self, "_clear_pending_map", {}).pop(scope, None)
            yield event.plain_result("ℹ️ 没有待确认的清空操作，或确认已超时。")
            return
        self._clear_pending_map.pop(scope, None)
        self._write_todos(scope, [])
        payload = self._scope(scope)
        payload["daily_reminder"]["last_sent"] = None
        await self._save_data()
        yield event.plain_result("✅ 已清空当前会话的全部待办事项。")

    @filter.command("编辑", alias={"修改待办"})
    async def edit_todo(self, event: AstrMessageEvent, args: str = ""):
        err = await self._guard(event)
        if err:
            yield event.plain_result(err)
            return
        m = re.match(r"^(\d+)\s+(.+)$", args.strip(), re.S)
        if not m:
            yield event.plain_result("❌ 用法：编辑 <编号> <新内容>")
            return
        todo_id, content = int(m.group(1)), m.group(2).strip()
        scope = self._scope_key(event)
        todos = self._todos(scope)
        item = self._find(todos, todo_id)
        if not item:
            yield event.plain_result(f"❌ 未找到待办 #{todo_id}")
            return
        if not content:
            yield event.plain_result("❌ 新内容不能为空。")
            return
        item.content = content
        self._write_todos(scope, todos)
        await self._save_data()
        yield event.plain_result(f"✏️ 已修改\n{self._format_item(item, True)}")

    @filter.command("优先级")
    async def set_priority(self, event: AstrMessageEvent, args: str = ""):
        err = await self._guard(event)
        if err:
            yield event.plain_result(err)
            return
        m = re.match(r"^(\d+)\s+(高|中|低)$", args.strip())
        if not m:
            yield event.plain_result("❌ 用法：优先级 <编号> 高/中/低")
            return
        todo_id, priority = int(m.group(1)), m.group(2)
        scope = self._scope_key(event)
        todos = self._todos(scope)
        item = self._find(todos, todo_id)
        if not item:
            yield event.plain_result(f"❌ 未找到待办 #{todo_id}")
            return
        item.priority = priority
        self._write_todos(scope, todos)
        await self._save_data()
        yield event.plain_result(f"🎚️ 已调整优先级\n{self._format_item(item, True)}")

    @filter.command("截止")
    async def set_due(self, event: AstrMessageEvent, args: str = ""):
        err = await self._guard(event)
        if err:
            yield event.plain_result(err)
            return
        m = re.match(r"^(\d+)\s+(.+)$", args.strip(), re.S)
        if not m:
            yield event.plain_result("❌ 用法：截止 <编号> 2026-10-06 18:00\n取消：截止 <编号> 取消")
            return
        todo_id, when_text = int(m.group(1)), m.group(2).strip()
        scope = self._scope_key(event)
        todos = self._todos(scope)
        item = self._find(todos, todo_id)
        if not item:
            yield event.plain_result(f"❌ 未找到待办 #{todo_id}")
            return
        if when_text in {"取消", "关闭", "清除"}:
            item.due_at = None
            self._write_todos(scope, todos)
            await self._save_data()
            yield event.plain_result(f"✅ 已取消 #{todo_id} 的截止时间。")
            return
        when = _parse_remind_time(when_text)
        if not when:
            yield event.plain_result("❌ 时间格式不支持。示例：截止 1 2026-10-06 18:00")
            return
        item.due_at = _iso(when)
        self._write_todos(scope, todos)
        await self._save_data()
        yield event.plain_result(f"📌 已设置截止时间\n{self._format_item(item, True)}")

    @filter.command("提醒")
    async def set_reminder(self, event: AstrMessageEvent, args: str = ""):
        err = await self._guard(event)
        if err:
            yield event.plain_result(err)
            return
        m = re.match(r"^(\d+)\s+(.+)$", args.strip(), re.S)
        if not m:
            yield event.plain_result("❌ 用法：提醒 <编号> 30分钟 / 18:30 / 2026-10-06 18:30\n取消：取消提醒 <编号>")
            return
        todo_id, when_text = int(m.group(1)), m.group(2).strip()
        scope = self._scope_key(event)
        todos = self._todos(scope)
        item = self._find(todos, todo_id)
        if not item:
            yield event.plain_result(f"❌ 未找到待办 #{todo_id}")
            return
        when = _parse_remind_time(when_text)
        if not when or when <= _now():
            yield event.plain_result("❌ 提醒时间无效。请设置为未来时间。")
            return
        item.remind_at = _iso(when)
        item.reminder_sent_for = None
        self._write_todos(scope, todos)
        await self._save_data()
        yield event.plain_result(f"⏰ 已设置提醒\n{self._format_item(item, True)}")

    @filter.command("取消提醒")
    async def cancel_reminder(self, event: AstrMessageEvent, args: str = ""):
        err = await self._guard(event)
        if err:
            yield event.plain_result(err)
            return
        try:
            todo_id = int(args.strip())
        except ValueError:
            yield event.plain_result("❌ 用法：取消提醒 <编号>")
            return
        scope = self._scope_key(event)
        todos = self._todos(scope)
        item = self._find(todos, todo_id)
        if not item:
            yield event.plain_result(f"❌ 未找到待办 #{todo_id}")
            return
        item.remind_at = None
        item.reminder_sent_for = None
        self._write_todos(scope, todos)
        await self._save_data()
        yield event.plain_result(f"✅ 已取消 #{todo_id} 的提醒。")

    @filter.command("每日提醒")
    async def daily_reminder(self, event: AstrMessageEvent, args: str = ""):
        err = await self._guard(event)
        if err:
            yield event.plain_result(err)
            return
        clock = _parse_clock(args.strip())
        if not clock:
            yield event.plain_result("❌ 用法：每日提醒 18:00 / 每日提醒 晚上8点")
            return
        hour, minute = clock
        scope = self._scope_key(event)
        payload = self._scope(scope)
        payload["daily_reminder"] = {
            "enabled": True,
            "time": f"{hour:02d}:{minute:02d}",
            "last_sent": None,
        }
        await self._save_data()
        yield event.plain_result(f"🔔 已开启每日待办提醒，每天 {hour:02d}:{minute:02d} 推送当前未完成待办。")

    @filter.command("取消每日提醒")
    async def cancel_daily_reminder(self, event: AstrMessageEvent):
        err = await self._guard(event)
        if err:
            yield event.plain_result(err)
            return
        scope = self._scope_key(event)
        payload = self._scope(scope)
        payload["daily_reminder"]["enabled"] = False
        payload["daily_reminder"]["last_sent"] = None
        await self._save_data()
        yield event.plain_result("✅ 已关闭每日待办提醒。")

    @filter.command("今日待办")
    async def today_todos(self, event: AstrMessageEvent):
        err = await self._guard(event)
        if err:
            yield event.plain_result(err)
            return
        today = _now().date()
        todos = self._todos(self._scope_key(event))
        items = []
        for item in todos:
            created = _parse_iso(item.created_at)
            completed = _parse_iso(item.completed_at)
            due = _parse_iso(item.due_at)
            if any(x and x.date() == today for x in (created, completed, due)):
                items.append(item)
        if not items:
            yield event.plain_result(f"📅 今天（{today.isoformat()}）没有相关待办。")
            return
        done = sum(1 for x in items if x.completed)
        lines = [f"📅 今日待办 · {today.isoformat()}", "────────────"]
        lines.extend(self._format_item(x, True) for x in self._sort(items))
        lines.append(f"────────────\n✅ 已完成：{done}/{len(items)}")
        yield event.plain_result("\n".join(lines))

    @filter.command("逾期待办")
    async def overdue_todos(self, event: AstrMessageEvent):
        err = await self._guard(event)
        if err:
            yield event.plain_result(err)
            return
        todos = [x for x in self._todos(self._scope_key(event)) if x.overdue()]
        if not todos:
            yield event.plain_result("🎉 当前没有逾期未完成待办。")
            return
        lines = [f"⚠️ 逾期待办 · {len(todos)} 项", "────────────"]
        lines.extend(self._format_item(x, True) for x in self._sort(todos))
        yield event.plain_result("\n".join(lines))

    @filter.command("我的待办")
    async def my_todos(self, event: AstrMessageEvent):
        err = await self._guard(event)
        if err:
            yield event.plain_result(err)
            return
        uid = self._user_id(event)
        name = self._user_name(event)
        todos = self._todos(self._scope_key(event))
        items = [x for x in todos if x.creator == name or x.assigned_to in {uid, name}]
        if not items:
            yield event.plain_result("👤 当前没有属于你的待办。")
            return
        lines = [f"👤 我的待办 · {name}", "────────────"]
        lines.extend(self._format_item(x, True) for x in self._sort(items))
        yield event.plain_result("\n".join(lines))

    @filter.command("搜索待办", alias={"搜索"})
    async def search_todos(self, event: AstrMessageEvent, args: str = ""):
        err = await self._guard(event)
        if err:
            yield event.plain_result(err)
            return
        keyword = args.strip()
        if not keyword:
            yield event.plain_result("❌ 用法：搜索待办 <关键词>")
            return
        todos = [x for x in self._todos(self._scope_key(event)) if keyword.lower() in x.content.lower()]
        if not todos:
            yield event.plain_result(f"🔎 没有找到包含“{keyword}”的待办。")
            return
        lines = [f"🔎 搜索结果 · {keyword}", "────────────"]
        lines.extend(self._format_item(x, True) for x in self._sort(todos))
        yield event.plain_result("\n".join(lines))

    @filter.command("完成率")
    async def completion_rate(self, event: AstrMessageEvent):
        err = await self._guard(event)
        if err:
            yield event.plain_result(err)
            return
        todos = self._todos(self._scope_key(event))
        if not todos:
            yield event.plain_result("📊 还没有待办事项，完成率为 0%。")
            return
        total = len(todos)
        completed = sum(1 for x in todos if x.completed)
        rate = completed / total * 100
        filled = round(20 * rate / 100)
        bar = "█" * filled + "░" * (20 - filled)
        yield event.plain_result(
            f"📊 完成率\n────────────\n总任务：{total}\n已完成：{completed}\n未完成：{total - completed}\n"
            f"完成率：{rate:.1f}%\n进度：|{bar}|"
        )

    @filter.command("待办统计")
    async def stats(self, event: AstrMessageEvent):
        err = await self._guard(event)
        if err:
            yield event.plain_result(err)
            return
        todos = self._todos(self._scope_key(event))
        total = len(todos)
        completed = sum(1 for x in todos if x.completed)
        overdue = sum(1 for x in todos if x.overdue())
        assigned = sum(1 for x in todos if x.assigned_to)
        priority_counts = {p: sum(1 for x in todos if x.priority == p and not x.completed) for p in PRIORITIES}
        yield event.plain_result(
            "📈 待办统计\n"
            "────────────\n"
            f"总数：{total}\n"
            f"已完成：{completed}\n"
            f"未完成：{total - completed}\n"
            f"逾期：{overdue}\n"
            f"已认领：{assigned}\n"
            f"未完成优先级：高 {priority_counts['高']} / 中 {priority_counts['中']} / 低 {priority_counts['低']}"
        )

    @filter.command("自助代办")
    async def auto_assign(self, event: AstrMessageEvent):
        err = await self._guard(event)
        if err:
            yield event.plain_result(err)
            return
        if not bool(self._cfg("auto_assign", True)):
            yield event.plain_result("⚠️ 自助代办功能已在插件配置中关闭。")
            return
        scope = self._scope_key(event)
        todos = self._sort([x for x in self._todos(scope) if not x.completed and not x.assigned_to])
        if not todos:
            yield event.plain_result("ℹ️ 没有可供认领的未完成待办。")
            return
        item = todos[0]
        item.assigned_to = self._user_name(event)
        all_todos = self._todos(scope)
        target = self._find(all_todos, item.id)
        if target:
            target.assigned_to = item.assigned_to
        self._write_todos(scope, all_todos)
        await self._save_data()
        yield event.plain_result(f"🤝 已为你认领待办\n{self._format_item(item, True)}")

    @filter.command("放弃代办")
    async def unassign(self, event: AstrMessageEvent, args: str = ""):
        err = await self._guard(event)
        if err:
            yield event.plain_result(err)
            return
        try:
            todo_id = int(args.strip())
        except ValueError:
            yield event.plain_result("❌ 用法：放弃代办 <编号>")
            return
        scope = self._scope_key(event)
        todos = self._todos(scope)
        item = self._find(todos, todo_id)
        if not item:
            yield event.plain_result(f"❌ 未找到待办 #{todo_id}")
            return
        if item.assigned_to and item.assigned_to not in {self._user_name(event), self._user_id(event)}:
            yield event.plain_result("❌ 这项待办不是你认领的，不能直接放弃。")
            return
        item.assigned_to = ""
        self._write_todos(scope, todos)
        await self._save_data()
        yield event.plain_result(f"↩️ 已放弃认领 #{todo_id}。")

    @filter.command("管理设置")
    async def settings(self, event: AstrMessageEvent):
        err = await self._guard(event)
        if err:
            yield event.plain_result(err)
            return
        scope = self._scope_key(event)
        payload = self._scope(scope)
        todos = self._todos(scope)
        daily = payload.get("daily_reminder", {})
        completed = sum(1 for x in todos if x.completed)
        rate = completed / len(todos) * 100 if todos else 0
        yield event.plain_result(
            "⚙️ 待办插件当前设置\n"
            "────────────\n"
            f"插件启用：{'是' if self._enabled() else '否'}\n"
            f"默认优先级：{self._cfg('default_priority', '中')}\n"
            f"自助代办：{'开启' if self._cfg('auto_assign', True) else '关闭'}\n"
            f"后台提醒：{'开启' if self._cfg('reminder_enabled', True) else '关闭'}\n"
            f"每日提醒：{'开启' if daily.get('enabled') else '关闭'} ({daily.get('time', '18:00')})\n"
            f"当前待办：{len(todos)}\n"
            f"完成率：{rate:.1f}%"
        )

    # ---------------- 后台提醒 ----------------
    async def _reminder_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(20)
                if not self._enabled() or not bool(self._cfg("reminder_enabled", True)):
                    continue
                changed = False
                now = _now()
                for scope, payload in list(self.data.get("scopes", {}).items()):
                    todos = self._todos(str(scope))
                    for item in todos:
                        when = _parse_iso(item.remind_at)
                        if item.completed or not when or when > now:
                            continue
                        stamp = item.remind_at
                        if item.reminder_sent_for == stamp:
                            continue
                        message = (
                            "⏰ 待办提醒\n"
                            "────────────\n"
                            f"{self._format_item(item, True)}\n"
                            "请完成后使用：完成 <编号>"
                        )
                        try:
                            await self.context.send_message(str(scope), MessageChain().message(message))
                            item.reminder_sent_for = stamp
                            changed = True
                        except Exception as exc:
                            logger.warning(f"[todo] 发送单项提醒失败 scope={scope}: {exc}")

                    daily = payload.get("daily_reminder", {}) or {}
                    if bool(daily.get("enabled")):
                        clock = _parse_clock(str(daily.get("time", "18:00")))
                        last = daily.get("last_sent")
                        if clock and (now.hour, now.minute) == clock and last != now.date().isoformat():
                            pending = [x for x in self._sort(todos) if not x.completed]
                            if pending:
                                lines = [f"🔔 每日待办提醒 · {now.date().isoformat()}", "────────────"]
                                lines.extend(self._format_item(x, True) for x in pending[:30])
                                if len(pending) > 30:
                                    lines.append(f"……还有 {len(pending) - 30} 项")
                                try:
                                    await self.context.send_message(str(scope), MessageChain().message("\n".join(lines)))
                                    daily["last_sent"] = now.date().isoformat()
                                    changed = True
                                except Exception as exc:
                                    logger.warning(f"[todo] 发送每日提醒失败 scope={scope}: {exc}")
                if changed:
                    self._save_data_sync()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f"[todo] 后台提醒任务异常退出: {exc}")

    async def terminate(self):
        if self._reminder_task and not self._reminder_task.done():
            self._reminder_task.cancel()
            try:
                await self._reminder_task
            except asyncio.CancelledError:
                pass
        try:
            self._save_data_sync()
        except Exception as exc:
            logger.error(f"[todo] 插件卸载保存数据失败: {exc}")
        logger.info("[todo] 待办事项插件已卸载")
