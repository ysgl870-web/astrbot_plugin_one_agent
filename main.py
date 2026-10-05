"""AstrBot 待办事项与提醒插件。

兼容 AstrBot 4.16+。
核心设计：
- 使用 unified_msg_origin 作为会话作用域，避免不同群/私聊串库。
- 所有编号操作统一解析：裸数字=当前列表序号，#数字=稳定 ID。
- 兼容历史版本的待办 JSON 数据。
- 原子写入 JSON，避免异常退出造成半截文件。
- 主动提醒使用 AstrBot 官方 MessageChain + unified_msg_origin API。
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
DATA_VERSION = 3

PRIORITIES = ("高", "中", "低")
PRIORITY_ICONS = {"高": "🔴", "中": "🟡", "低": "🟢"}
PRIORITY_ORDER = {"高": 0, "中": 1, "低": 2}

_CLOCK_RE = re.compile(r"^(?:[01]?\d|2[0-3])[:：][0-5]\d$")
_FULL_DT_RE = re.compile(
    r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\s+(\d{1,2})[:：](\d{2})$"
)
_DURATION_RE = re.compile(r"^(\d+)\s*(分钟|分|小时|时|天)$", re.I)
_REF_RE = re.compile(r"^#?(?:(?:编号|序号)\s*)?(\d+)$")


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
    """解析 18:30、晚上8点、下午2点、早上8点。"""
    s = str(text or "").strip().replace("时", "点")
    if _CLOCK_RE.fullmatch(s):
        parts = re.split(r"[:：]", s)
        return int(parts[0]), int(parts[1])

    patterns = (
        (r"^(?:凌晨|早上)(\d{1,2})点(?:([0-5]\d)分?)?$", 0),
        (r"^(?:下午|午后)(\d{1,2})点(?:([0-5]\d)分?)?$", 12),
        (r"^晚上(\d{1,2})点(?:([0-5]\d)分?)?$", 12),
        (r"^(\d{1,2})点(?:([0-5]\d)分?)?$", 0),
    )
    for pattern, offset in patterns:
        match = re.fullmatch(pattern, s)
        if not match:
            continue
        hour = int(match.group(1))
        if offset and hour != 12:
            hour += offset
        minute = int(match.group(2) or 0)
        if 0 <= hour <= 23:
            return hour, minute
    return None


def _parse_time(text: str, base: Optional[dt.datetime] = None) -> Optional[dt.datetime]:
    """解析 30分钟、2小时、18:30、2026-10-06 18:30。"""
    s = str(text or "").strip()
    base = base or _now()

    m = _DURATION_RE.fullmatch(s)
    if m:
        amount = int(m.group(1))
        unit = m.group(2)
        if unit in {"分钟", "分"}:
            return base + dt.timedelta(minutes=amount)
        if unit in {"小时", "时"}:
            return base + dt.timedelta(hours=amount)
        return base + dt.timedelta(days=amount)

    m = _FULL_DT_RE.fullmatch(s)
    if m:
        y, month, day, hour, minute = map(int, m.groups())
        try:
            return dt.datetime(y, month, day, hour, minute)
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
        creator_id: str = "",
        assigned_to: str = "",
        assigned_to_id: str = "",
        created_at: Optional[str] = None,
        completed: bool = False,
        completed_at: Optional[str] = None,
        due_at: Optional[str] = None,
        remind_at: Optional[str] = None,
        reminder_sent_for: Optional[str] = None,
    ) -> None:
        self.id = 0
        self.content = str(content or "").strip()
        self.priority = priority if priority in PRIORITIES else "中"
        self.creator = str(creator or "").strip()
        self.creator_id = str(creator_id or "").strip()
        self.assigned_to = str(assigned_to or "").strip()
        self.assigned_to_id = str(assigned_to_id or "").strip()
        self.created_at = created_at or _iso(_now())
        self.completed = bool(completed)
        self.completed_at = completed_at
        self.due_at = due_at
        self.remind_at = remind_at
        self.reminder_sent_for = reminder_sent_for

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "TodoItem":
        item = cls(
            content=raw.get("content", raw.get("title", "")),
            priority=str(raw.get("priority", "中")),
            creator=str(raw.get("creator", "")),
            creator_id=str(raw.get("creator_id", "")),
            assigned_to=str(raw.get("assigned_to", "")),
            assigned_to_id=str(raw.get("assigned_to_id", "")),
            created_at=raw.get("created_at") or None,
            completed=raw.get("completed", False),
            completed_at=raw.get("completed_at") or None,
            due_at=raw.get("due_at") or None,
            remind_at=raw.get("remind_at") or None,
            reminder_sent_for=raw.get("reminder_sent_for") or None,
        )
        try:
            item.id = int(raw.get("id", 0))
        except (TypeError, ValueError):
            item.id = 0
        return item

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "content": self.content,
            "priority": self.priority,
            "creator": self.creator,
            "creator_id": self.creator_id,
            "assigned_to": self.assigned_to,
            "assigned_to_id": self.assigned_to_id,
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
        self.data_file = os.path.join(self._data_dir(), "todo_data.json")
        self.data = self._load_data()
        self._lock = asyncio.Lock()
        self._clear_pending_map: Dict[str, float] = {}
        self._reminder_task: Optional[asyncio.Task] = None
        try:
            self._reminder_task = asyncio.create_task(self._reminder_loop())
        except RuntimeError:
            logger.warning("[todo] 当前无可用事件循环，后台提醒未启动")

    @staticmethod
    def _data_dir() -> str:
        try:
            from astrbot.api.star import StarTools

            path = os.fspath(StarTools.get_data_dir(PLUGIN_ID))
            os.makedirs(path, exist_ok=True)
            return path
        except Exception as exc:
            logger.warning(f"[todo] 获取插件数据目录失败，使用回退目录: {exc}")
            path = os.path.join("AstrBot", "data", "plugin_data", PLUGIN_ID)
            os.makedirs(path, exist_ok=True)
            return path

    # ---------------- 配置 ----------------
    def _cfg(self, key: str, default: Any) -> Any:
        try:
            getter = getattr(self.config, "get", None)
            value = getter(key, default) if callable(getter) else getattr(self.config, key, default)
            return default if value is None else value
        except Exception:
            return default

    def _enabled(self) -> bool:
        return bool(self._cfg("enabled", True))

    # ---------------- 数据 ----------------
    @staticmethod
    def _default_data() -> Dict[str, Any]:
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
                backup = "备份失败"
            logger.error(f"[todo] 数据文件损坏，已创建备份({backup})：{exc}")
            return self._default_data()

    def _migrate_data(self, raw: Any) -> Dict[str, Any]:
        result = self._default_data()
        if not isinstance(raw, dict):
            return result

        # v3 已经是当前结构。
        if raw.get("version") == DATA_VERSION and isinstance(raw.get("scopes"), dict):
            for key, payload in raw["scopes"].items():
                if not isinstance(payload, dict):
                    continue
                result["scopes"][str(key)] = self._normalize_scope(payload)
            return result

        scopes = raw.get("scopes") if isinstance(raw.get("scopes"), dict) else raw
        if not isinstance(scopes, dict):
            return result

        for key, value in scopes.items():
            if isinstance(value, dict) and "todos" in value:
                # v2 数据结构：{todos: [...], daily_reminder: {...}}
                result["scopes"][str(key)] = self._normalize_scope(value)
                continue

            # v1 数据结构：{scope: [todo, todo, ...]}。
            if not isinstance(value, list):
                continue
            todos: List[Dict[str, Any]] = []
            legacy_daily_time: Optional[str] = None
            for entry in value:
                if not isinstance(entry, dict):
                    continue
                content = str(entry.get("content", "")).strip()
                if content:
                    item = copy.deepcopy(entry)
                    item.pop("time", None)
                    item.pop("channel", None)
                    item.setdefault("due_at", None)
                    item.setdefault("remind_at", None)
                    todos.append(item)
                else:
                    clock = entry.get("time")
                    if isinstance(clock, str) and _parse_clock(clock):
                        legacy_daily_time = clock
            result["scopes"][str(key)] = {
                "todos": todos,
                "daily_reminder": {
                    "enabled": bool(legacy_daily_time),
                    "time": legacy_daily_time or str(self._cfg("default_daily_reminder_time", "18:00")),
                    "last_sent": None,
                },
            }
        return result

    def _normalize_scope(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        daily = payload.get("daily_reminder")
        if not isinstance(daily, dict):
            daily = {}
        return {
            "todos": payload.get("todos") if isinstance(payload.get("todos"), list) else [],
            "daily_reminder": {
                "enabled": bool(daily.get("enabled", False)),
                "time": str(daily.get("time") or self._cfg("default_daily_reminder_time", "18:00")),
                "last_sent": daily.get("last_sent"),
            },
        }

    def _save_data_sync(self) -> None:
        directory = os.path.dirname(self.data_file) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".todo_", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.data_file)
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    async def _save_data(self) -> None:
        async with self._lock:
            self._save_data_sync()

    # ---------------- 会话/用户 ----------------
    def _scope_key(self, event: AstrMessageEvent) -> str:
        try:
            umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
            if umo:
                return f"umo:{umo}"
        except Exception:
            pass
        try:
            group_id = str(event.get_group_id() or "").strip()
        except Exception:
            group_id = ""
        if group_id:
            return f"group:{group_id}"
        try:
            user_id = str(event.get_sender_id() or "").strip()
        except Exception:
            user_id = "unknown"
        return f"private:{user_id}"

    def _scope_candidates(self, event: AstrMessageEvent) -> List[str]:
        primary = self._scope_key(event)
        candidates = [primary]
        try:
            group_id = str(event.get_group_id() or "").strip()
        except Exception:
            group_id = ""
        if group_id:
            candidates.extend([f"group:{group_id}", f"fallback:{group_id}", group_id])
        else:
            candidates.extend(["private", "fallback:private"])
            try:
                user_id = str(event.get_sender_id() or "").strip()
            except Exception:
                user_id = ""
            if user_id:
                candidates.append(f"private:{user_id}")
        return list(dict.fromkeys(candidates))

    def _resolve_scope(self, event: AstrMessageEvent) -> str:
        scopes = self.data.setdefault("scopes", {})
        candidates = self._scope_candidates(event)
        for key in candidates:
            payload = scopes.get(key)
            if not isinstance(payload, dict):
                continue
            if isinstance(payload.get("todos"), list) and payload["todos"]:
                return key
            daily = payload.get("daily_reminder")
            if isinstance(daily, dict) and daily.get("enabled"):
                return key
        return candidates[0]

    def _user_id(self, event: AstrMessageEvent) -> str:
        try:
            return str(event.get_sender_id() or "unknown")
        except Exception:
            return "unknown"

    def _user_name(self, event: AstrMessageEvent) -> str:
        try:
            value = event.get_sender_name()
            return str(value).strip() if value else "用户"
        except Exception:
            return "用户"

    def _scope_payload(self, key: str) -> Dict[str, Any]:
        scopes = self.data.setdefault("scopes", {})
        payload = scopes.setdefault(key, {
            "todos": [],
            "daily_reminder": {
                "enabled": False,
                "time": str(self._cfg("default_daily_reminder_time", "18:00")),
                "last_sent": None,
            },
        })
        normalized = self._normalize_scope(payload)
        scopes[key] = normalized
        return normalized

    def _repair_ids(self, todos: List[TodoItem]) -> bool:
        """修复旧数据中的 0、负数或重复 ID。"""
        used = set()
        changed = False
        next_id = max((x.id for x in todos if x.id > 0), default=0) + 1
        for item in todos:
            if item.id <= 0 or item.id in used:
                while next_id in used:
                    next_id += 1
                item.id = next_id
                next_id += 1
                changed = True
            used.add(item.id)
        return changed

    def _todos(self, key: str) -> List[TodoItem]:
        payload = self._scope_payload(key)
        result: List[TodoItem] = []
        for raw in payload.get("todos", []):
            if not isinstance(raw, dict):
                continue
            try:
                item = TodoItem.from_dict(raw)
                if item.content:
                    result.append(item)
            except Exception as exc:
                logger.warning(f"[todo] 忽略异常待办数据：{exc}")
        if self._repair_ids(result):
            payload["todos"] = [x.to_dict() for x in result]
            self._save_data_sync()
        return result

    def _write_todos(self, key: str, todos: List[TodoItem]) -> None:
        payload = self._scope_payload(key)
        payload["todos"] = [x.to_dict() for x in todos]

    def _next_id(self, todos: List[TodoItem]) -> int:
        return max((x.id for x in todos), default=0) + 1

    # ---------------- 编号解析 ----------------
    @staticmethod
    def _parse_ref(text: str) -> Optional[Tuple[bool, int]]:
        value = str(text or "").strip().replace("＃", "#")
        m = _REF_RE.fullmatch(value)
        if not m:
            return None
        return value.startswith("#"), int(m.group(1))

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

    def _resolve_item(self, todos: List[TodoItem], ref: str) -> Optional[TodoItem]:
        parsed = self._parse_ref(ref)
        if not parsed:
            return None
        exact_id, number = parsed
        if number <= 0:
            return None
        if exact_id:
            return next((x for x in todos if x.id == number), None)

        # 裸数字优先代表当前列表序号。
        visible = self._sort(todos)
        if 1 <= number <= len(visible):
            return visible[number - 1]
        # 若超出列表序号，再兼容按稳定 ID 查找。
        return next((x for x in todos if x.id == number), None)

    def _format_item(self, item: TodoItem, include_id: bool = True) -> str:
        if item.completed:
            status = "✅"
        elif item.overdue():
            status = "⚠️"
        else:
            status = "⬜"
        owner = f" · 👤{item.creator}" if item.creator else ""
        assigned = f" · 🤝{item.assigned_to}" if item.assigned_to else ""
        due = ""
        due_dt = _parse_iso(item.due_at)
        if due_dt:
            due = f" · 截止 {due_dt.strftime('%m-%d %H:%M')}"
        remind = ""
        remind_dt = _parse_iso(item.remind_at)
        if remind_dt and not item.completed:
            remind = f" · ⏰{remind_dt.strftime('%m-%d %H:%M')}"
        ident = f"#{item.id} " if include_id else ""
        return f"{status} {ident}{PRIORITY_ICONS.get(item.priority, '⚪')} {item.content}{owner}{assigned}{due}{remind}"

    def _format_list(self, todos: List[TodoItem]) -> str:
        visible = self._sort(todos)
        total = len(todos)
        completed = sum(1 for x in todos if x.completed)
        lines = ["📝 待办列表", "────────────"]
        lines.extend(f"{i}. {self._format_item(item)}" for i, item in enumerate(visible, 1))
        lines.extend([
            "────────────",
            f"📊 进度：{completed}/{total}",
            "💡 操作：完成 1 = 列表第1项；完成 #7 = 精确操作ID为7的任务。",
        ])
        return "\n".join(lines)

    def _parse_add(self, args: str) -> Tuple[str, str]:
        text = re.sub(r"\s+", " ", str(args or "").strip())
        priority = str(self._cfg("default_priority", "中"))
        if priority not in PRIORITIES:
            priority = "中"
        if not text:
            return priority, ""
        front = re.fullmatch(r"(高|中|低)(?:\s+|$)(.*)", text, re.S)
        if front:
            return front.group(1), front.group(2).strip()
        back = re.fullmatch(r"(.+?)\s+(高|中|低)", text, re.S)
        if back:
            return back.group(2), back.group(1).strip()
        return priority, text

    def _help_text(self) -> str:
        return (
            "📋 待办事项助手\n"
            "────────────\n"
            "待办/代办 <内容> [高/中/低]  添加待办\n"
            "待办列表/代办列表 [全部/未完成/已完成]  查看\n"
            "完成 <序号> 或 完成 #ID  标记完成\n"
            "撤销完成 <序号> 或 撤销完成 #ID  恢复未完成\n"
            "编辑 <序号> <内容>  修改内容\n"
            "优先级 <序号> 高/中/低  修改优先级\n"
            "截止 <序号> 2026-10-06 18:00  设置截止\n"
            "提醒 <序号> 30分钟/18:30  设置提醒\n"
            "取消提醒 <序号>  取消提醒\n"
            "今日待办 / 逾期待办 / 我的待办\n"
            "搜索待办 <关键词> / 完成率 / 待办统计\n"
            "自助代办 / 放弃代办 <序号>\n"
            "删除 <序号> / 清空待办 → 确认清空\n"
            "每日提醒 18:00 / 取消每日提醒\n"
            "代办菜单  打开帮助\n"
            "────────────\n"
            "提示：列表中的 1/2/3 是当前显示顺序；#ID 是精确任务 ID。"
        )

    async def _guard(self, event: AstrMessageEvent) -> Optional[str]:
        if not self._enabled():
            return "⚠️ 待办插件当前已关闭，请在插件配置中重新开启。"
        return None

    # ---------------- 基础指令 ----------------
    @filter.command("代办菜单", alias={"待办帮助", "todo帮助", "待办菜单"})
    async def menu(self, event: AstrMessageEvent):
        err = await self._guard(event)
        if err:
            yield event.plain_result(err)
            return
        yield event.plain_result(self._help_text())

    @filter.command("待办", alias={"代办", "添加待办", "todo"})
    async def add_todo(self, event: AstrMessageEvent, args: str = ""):
        err = await self._guard(event)
        if err:
            yield event.plain_result(err)
            return
        priority, content = self._parse_add(args)
        if not content:
            yield event.plain_result("❌ 内容不能为空。例：代办 测试 高")
            return

        scope = self._resolve_scope(event)
        todos = self._todos(scope)
        limit = max(1, int(self._cfg("max_todos", 100)))
        if len(todos) >= limit:
            yield event.plain_result(f"❌ 当前会话已有 {len(todos)} 项待办，已达到上限 {limit}。")
            return

        item = TodoItem(
            content=content,
            priority=priority,
            creator=self._user_name(event),
            creator_id=self._user_id(event),
        )
        item.id = self._next_id(todos)
        todos.append(item)
        self._write_todos(scope, todos)
        await self._save_data()
        yield event.plain_result(f"✅ 已添加待办\n{self._format_item(item)}\n💡 之后可直接发送：完成 1")

    @filter.command("待办列表", alias={"代办列表", "todo列表", "待办清单", "代办清单"})
    async def list_todos(self, event: AstrMessageEvent, args: str = ""):
        err = await self._guard(event)
        if err:
            yield event.plain_result(err)
            return
        scope = self._resolve_scope(event)
        todos = self._todos(scope)
        mode = str(args or "").strip() or "全部"
        if mode not in {"全部", "未完成", "已完成"}:
            yield event.plain_result("❌ 用法：待办列表 / 待办列表 未完成 / 待办列表 已完成")
            return
        if mode == "未完成":
            todos = [x for x in todos if not x.completed]
        elif mode == "已完成":
            todos = [x for x in todos if x.completed]
        if not todos:
            yield event.plain_result(f"📝 没有符合条件的待办（{mode}）。")
            return
        yield event.plain_result(self._format_list(todos))

    def _get_ref_and_item(self, event: AstrMessageEvent, args: str) -> Tuple[Optional[str], Optional[str], Optional[TodoItem], List[TodoItem]]:
        raw = str(args or "").strip()
        if not raw:
            return None, None, None, []
        ref = raw.split(None, 1)[0]
        if not self._parse_ref(ref):
            return ref, None, None, []
        scope = self._resolve_scope(event)
        todos = self._todos(scope)
        return ref, scope, self._resolve_item(todos, ref), todos

    @filter.command("完成", alias={"完成待办", "todo完成"})
    async def complete_todo(self, event: AstrMessageEvent, args: str = ""):
        err = await self._guard(event)
        if err:
            yield event.plain_result(err)
            return
        ref, scope, item, todos = self._get_ref_and_item(event, args)
        if not ref or not self._parse_ref(ref):
            yield event.plain_result("❌ 用法：完成 1 或 完成 #1；建议先发送：待办列表")
            return
        if not item or scope is None:
            yield event.plain_result(f"❌ 当前列表中没有找到“{ref}”。请先发送：待办列表")
            return
        if item.completed:
            yield event.plain_result(f"⚠️ #{item.id} 已经完成，无需重复操作。")
            return
        item.completed = True
        item.completed_at = _iso(_now())
        item.remind_at = None
        item.reminder_sent_for = None
        self._write_todos(scope, todos)
        await self._save_data()
        yield event.plain_result(f"🎉 已完成 #" + str(item.id) + f"\n{self._format_item(item)}")

    @filter.command("撤销完成", alias={"取消完成", "恢复待办"})
    async def uncomplete_todo(self, event: AstrMessageEvent, args: str = ""):
        err = await self._guard(event)
        if err:
            yield event.plain_result(err)
            return
        ref, scope, item, todos = self._get_ref_and_item(event, args)
        if not ref or not self._parse_ref(ref):
            yield event.plain_result("❌ 用法：撤销完成 1 或 撤销完成 #1")
            return
        if not item or scope is None:
            yield event.plain_result(f"❌ 当前列表中没有找到“{ref}”。请先发送：待办列表")
            return
        item.completed = False
        item.completed_at = None
        self._write_todos(scope, todos)
        await self._save_data()
        yield event.plain_result(f"↩️ 已恢复未完成 #{item.id}\n{self._format_item(item)}")

    @filter.command("删除", alias={"删除待办", "todo删除"})
    async def delete_todo(self, event: AstrMessageEvent, args: str = ""):
        err = await self._guard(event)
        if err:
            yield event.plain_result(err)
            return
        ref, scope, item, todos = self._get_ref_and_item(event, args)
        if not ref or not self._parse_ref(ref):
            yield event.plain_result("❌ 用法：删除 1 或 删除 #1")
            return
        if not item or scope is None:
            yield event.plain_result(f"❌ 当前列表中没有找到“{ref}”。请先发送：待办列表")
            return
        todos.remove(item)
        self._write_todos(scope, todos)
        await self._save_data()
        yield event.plain_result(f"🗑️ 已删除 #{item.id}：{item.content}")

    # ---------------- 编辑/属性 ----------------
    @filter.command("编辑", alias={"修改待办"})
    async def edit_todo(self, event: AstrMessageEvent, args: str = ""):
        err = await self._guard(event)
        if err:
            yield event.plain_result(err)
            return
        m = re.fullmatch(r"(#?\d+|(?:编号|序号)\s*\d+)\s+(.+)", str(args or "").strip(), re.S)
        if not m:
            yield event.plain_result("❌ 用法：编辑 1 新内容 或 编辑 #1 新内容")
            return
        ref, content = m.group(1), m.group(2).strip()
        scope = self._resolve_scope(event)
        todos = self._todos(scope)
        item = self._resolve_item(todos, ref)
        if not item:
            yield event.plain_result(f"❌ 当前列表中没有找到“{ref}”。请先发送：待办列表")
            return
        item.content = content
        self._write_todos(scope, todos)
        await self._save_data()
        yield event.plain_result(f"✏️ 已修改 #{item.id}\n{self._format_item(item)}")

    @filter.command("优先级")
    async def set_priority(self, event: AstrMessageEvent, args: str = ""):
        err = await self._guard(event)
        if err:
            yield event.plain_result(err)
            return
        m = re.fullmatch(r"(#?\d+|(?:编号|序号)\s*\d+)\s+(高|中|低)", str(args or "").strip())
        if not m:
            yield event.plain_result("❌ 用法：优先级 1 高 或 优先级 #1 高")
            return
        ref, priority = m.group(1), m.group(2)
        scope = self._resolve_scope(event)
        todos = self._todos(scope)
        item = self._resolve_item(todos, ref)
        if not item:
            yield event.plain_result("❌ 当前列表中没有找到对应任务。请先发送：待办列表")
            return
        item.priority = priority
        self._write_todos(scope, todos)
        await self._save_data()
        yield event.plain_result(f"🎚️ 已调整 #{item.id} 的优先级为【{priority}】")

    @filter.command("截止")
    async def set_due(self, event: AstrMessageEvent, args: str = ""):
        err = await self._guard(event)
        if err:
            yield event.plain_result(err)
            return
        m = re.fullmatch(r"(#?\d+|(?:编号|序号)\s*\d+)\s+(.+)", str(args or "").strip(), re.S)
        if not m:
            yield event.plain_result("❌ 用法：截止 1 2026-10-06 18:00；取消：截止 1 取消")
            return
        ref, when_text = m.group(1), m.group(2).strip()
        scope = self._resolve_scope(event)
        todos = self._todos(scope)
        item = self._resolve_item(todos, ref)
        if not item:
            yield event.plain_result("❌ 当前列表中没有找到对应任务。请先发送：待办列表")
            return
        if when_text in {"取消", "关闭", "清除"}:
            item.due_at = None
            self._write_todos(scope, todos)
            await self._save_data()
            yield event.plain_result(f"✅ 已取消 #{item.id} 的截止时间。")
            return
        when = _parse_time(when_text)
        if not when:
            yield event.plain_result("❌ 时间格式不支持。例：截止 1 2026-10-06 18:00")
            return
        item.due_at = _iso(when)
        self._write_todos(scope, todos)
        await self._save_data()
        yield event.plain_result(f"📌 已设置截止时间\n{self._format_item(item)}")

    @filter.command("提醒")
    async def set_reminder(self, event: AstrMessageEvent, args: str = ""):
        err = await self._guard(event)
        if err:
            yield event.plain_result(err)
            return
        m = re.fullmatch(r"(#?\d+|(?:编号|序号)\s*\d+)\s+(.+)", str(args or "").strip(), re.S)
        if not m:
            yield event.plain_result("❌ 用法：提醒 1 30分钟 / 18:30 / 2026-10-06 18:30")
            return
        ref, when_text = m.group(1), m.group(2).strip()
        scope = self._resolve_scope(event)
        todos = self._todos(scope)
        item = self._resolve_item(todos, ref)
        if not item:
            yield event.plain_result("❌ 当前列表中没有找到对应任务。请先发送：待办列表")
            return
        when = _parse_time(when_text)
        if not when or when <= _now():
            yield event.plain_result("❌ 提醒时间必须是未来时间。")
            return
        item.remind_at = _iso(when)
        item.reminder_sent_for = None
        self._write_todos(scope, todos)
        await self._save_data()
        yield event.plain_result(f"⏰ 已设置提醒\n{self._format_item(item)}")

    @filter.command("取消提醒")
    async def cancel_reminder(self, event: AstrMessageEvent, args: str = ""):
        err = await self._guard(event)
        if err:
            yield event.plain_result(err)
            return
        ref = str(args or "").strip()
        if not self._parse_ref(ref):
            yield event.plain_result("❌ 用法：取消提醒 1 或 取消提醒 #1")
            return
        scope = self._resolve_scope(event)
        todos = self._todos(scope)
        item = self._resolve_item(todos, ref)
        if not item:
            yield event.plain_result("❌ 当前列表中没有找到对应任务。请先发送：待办列表")
            return
        item.remind_at = None
        item.reminder_sent_for = None
        self._write_todos(scope, todos)
        await self._save_data()
        yield event.plain_result(f"✅ 已取消 #{item.id} 的提醒。")

    # ---------------- 查询 ----------------
    @filter.command("今日待办")
    async def today_todos(self, event: AstrMessageEvent):
        err = await self._guard(event)
        if err:
            yield event.plain_result(err)
            return
        today = _now().date()
        todos = self._todos(self._resolve_scope(event))
        items = []
        for item in todos:
            dates = (_parse_iso(item.created_at), _parse_iso(item.completed_at), _parse_iso(item.due_at))
            if any(value and value.date() == today for value in dates):
                items.append(item)
        if not items:
            yield event.plain_result(f"📅 今天（{today.isoformat()}）没有相关待办。")
            return
        done = sum(1 for x in items if x.completed)
        lines = [f"📅 今日待办 · {today.isoformat()}", "────────────"]
        lines.extend(f"{i}. {self._format_item(item)}" for i, item in enumerate(self._sort(items), 1))
        lines.append(f"────────────\n✅ 已完成：{done}/{len(items)}")
        yield event.plain_result("\n".join(lines))

    @filter.command("逾期待办")
    async def overdue_todos(self, event: AstrMessageEvent):
        err = await self._guard(event)
        if err:
            yield event.plain_result(err)
            return
        items = [x for x in self._todos(self._resolve_scope(event)) if x.overdue()]
        if not items:
            yield event.plain_result("🎉 当前没有逾期未完成待办。")
            return
        lines = [f"⚠️ 逾期待办 · {len(items)} 项", "────────────"]
        lines.extend(f"{i}. {self._format_item(item)}" for i, item in enumerate(self._sort(items), 1))
        yield event.plain_result("\n".join(lines))

    @filter.command("我的待办")
    async def my_todos(self, event: AstrMessageEvent):
        err = await self._guard(event)
        if err:
            yield event.plain_result(err)
            return
        uid = self._user_id(event)
        name = self._user_name(event)
        todos = self._todos(self._resolve_scope(event))
        items = [
            x for x in todos
            if x.creator_id == uid or x.creator == name or x.assigned_to_id == uid or x.assigned_to == name
        ]
        if not items:
            yield event.plain_result("👤 当前没有属于你的待办。")
            return
        lines = [f"👤 我的待办 · {name}", "────────────"]
        lines.extend(f"{i}. {self._format_item(item)}" for i, item in enumerate(self._sort(items), 1))
        yield event.plain_result("\n".join(lines))

    @filter.command("搜索待办", alias={"搜索"})
    async def search_todos(self, event: AstrMessageEvent, args: str = ""):
        err = await self._guard(event)
        if err:
            yield event.plain_result(err)
            return
        keyword = str(args or "").strip()
        if not keyword:
            yield event.plain_result("❌ 用法：搜索待办 <关键词>")
            return
        items = [x for x in self._todos(self._resolve_scope(event)) if keyword.casefold() in x.content.casefold()]
        if not items:
            yield event.plain_result(f"🔎 没有找到包含“{keyword}”的待办。")
            return
        lines = [f"🔎 搜索结果 · {keyword}", "────────────"]
        lines.extend(f"{i}. {self._format_item(item)}" for i, item in enumerate(self._sort(items), 1))
        yield event.plain_result("\n".join(lines))

    @filter.command("完成率")
    async def completion_rate(self, event: AstrMessageEvent):
        err = await self._guard(event)
        if err:
            yield event.plain_result(err)
            return
        todos = self._todos(self._resolve_scope(event))
        if not todos:
            yield event.plain_result("📊 当前没有待办，完成率为 0%。")
            return
        total = len(todos)
        completed = sum(1 for x in todos if x.completed)
        rate = completed / total * 100
        filled = round(rate / 100 * 20)
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
        todos = self._todos(self._resolve_scope(event))
        total = len(todos)
        completed = sum(1 for x in todos if x.completed)
        overdue = sum(1 for x in todos if x.overdue())
        assigned = sum(1 for x in todos if x.assigned_to or x.assigned_to_id)
        high = sum(1 for x in todos if x.priority == "高" and not x.completed)
        middle = sum(1 for x in todos if x.priority == "中" and not x.completed)
        low = sum(1 for x in todos if x.priority == "低" and not x.completed)
        yield event.plain_result(
            "📈 待办统计\n────────────\n"
            f"总数：{total}\n已完成：{completed}\n未完成：{total - completed}\n"
            f"逾期：{overdue}\n已认领：{assigned}\n"
            f"未完成优先级：高 {high} / 中 {middle} / 低 {low}"
        )

    # ---------------- 代办 ----------------
    @filter.command("自助代办")
    async def auto_assign(self, event: AstrMessageEvent):
        err = await self._guard(event)
        if err:
            yield event.plain_result(err)
            return
        if not bool(self._cfg("auto_assign", True)):
            yield event.plain_result("⚠️ 自助代办功能已关闭。")
            return
        scope = self._resolve_scope(event)
        todos = self._todos(scope)
        candidates = self._sort([x for x in todos if not x.completed and not x.assigned_to and not x.assigned_to_id])
        if not candidates:
            yield event.plain_result("ℹ️ 没有可供认领的未完成待办。")
            return
        item = candidates[0]
        item.assigned_to = self._user_name(event)
        item.assigned_to_id = self._user_id(event)
        self._write_todos(scope, todos)
        await self._save_data()
        yield event.plain_result(f"🤝 已认领 #{item.id}\n{self._format_item(item)}")

    @filter.command("放弃代办")
    async def unassign(self, event: AstrMessageEvent, args: str = ""):
        err = await self._guard(event)
        if err:
            yield event.plain_result(err)
            return
        ref = str(args or "").strip()
        if not self._parse_ref(ref):
            yield event.plain_result("❌ 用法：放弃代办 1 或 放弃代办 #1")
            return
        scope = self._resolve_scope(event)
        todos = self._todos(scope)
        item = self._resolve_item(todos, ref)
        if not item:
            yield event.plain_result("❌ 当前列表中没有找到对应任务。请先发送：待办列表")
            return
        uid = self._user_id(event)
        name = self._user_name(event)
        if item.assigned_to_id and item.assigned_to_id != uid:
            yield event.plain_result("❌ 这项待办不是你认领的，不能放弃。")
            return
        if not item.assigned_to_id and item.assigned_to and item.assigned_to != name:
            yield event.plain_result("❌ 这项待办不是你认领的，不能放弃。")
            return
        item.assigned_to = ""
        item.assigned_to_id = ""
        self._write_todos(scope, todos)
        await self._save_data()
        yield event.plain_result(f"↩️ 已放弃认领 #{item.id}。")

    # ---------------- 清空 ----------------
    @filter.command("清空待办")
    async def clear_todos(self, event: AstrMessageEvent):
        err = await self._guard(event)
        if err:
            yield event.plain_result(err)
            return
        scope = self._resolve_scope(event)
        todos = self._todos(scope)
        if not todos:
            yield event.plain_result("ℹ️ 当前没有待办，无需清空。")
            return
        self._clear_pending_map[scope] = _now().timestamp()
        yield event.plain_result(
            f"⚠️ 当前共有 {len(todos)} 项待办。\n"
            "确认清空请在 60 秒内发送：确认清空\n"
            "其他消息不会执行清空。"
        )

    @filter.command("确认清空")
    async def confirm_clear(self, event: AstrMessageEvent):
        err = await self._guard(event)
        if err:
            yield event.plain_result(err)
            return
        scope = self._resolve_scope(event)
        pending = self._clear_pending_map.get(scope)
        if pending is None:
            yield event.plain_result("ℹ️ 当前没有待确认的清空操作。")
            return
        if _now().timestamp() - pending > 60:
            self._clear_pending_map.pop(scope, None)
            yield event.plain_result("⏱️ 清空确认已超时，请重新发送：清空待办")
            return
        self._clear_pending_map.pop(scope, None)
        self._write_todos(scope, [])
        self._scope_payload(scope)["daily_reminder"]["last_sent"] = None
        await self._save_data()
        yield event.plain_result("✅ 已清空当前会话的全部待办事项。")

    # ---------------- 每日提醒 ----------------
    @filter.command("每日提醒")
    async def daily_reminder(self, event: AstrMessageEvent, args: str = ""):
        err = await self._guard(event)
        if err:
            yield event.plain_result(err)
            return
        clock = _parse_clock(str(args or "").strip())
        if not clock:
            yield event.plain_result("❌ 用法：每日提醒 18:00 或 每日提醒 晚上8点")
            return
        hour, minute = clock
        scope = self._resolve_scope(event)
        self._scope_payload(scope)["daily_reminder"] = {
            "enabled": True,
            "time": f"{hour:02d}:{minute:02d}",
            "last_sent": None,
        }
        await self._save_data()
        yield event.plain_result(f"🔔 已开启每日提醒，每天 {hour:02d}:{minute:02d} 推送未完成待办。")

    @filter.command("取消每日提醒")
    async def cancel_daily_reminder(self, event: AstrMessageEvent):
        err = await self._guard(event)
        if err:
            yield event.plain_result(err)
            return
        scope = self._resolve_scope(event)
        daily = self._scope_payload(scope)["daily_reminder"]
        daily["enabled"] = False
        daily["last_sent"] = None
        await self._save_data()
        yield event.plain_result("✅ 已关闭每日待办提醒。")

    @filter.command("管理设置")
    async def settings(self, event: AstrMessageEvent):
        err = await self._guard(event)
        if err:
            yield event.plain_result(err)
            return
        scope = self._resolve_scope(event)
        todos = self._todos(scope)
        daily = self._scope_payload(scope)["daily_reminder"]
        completed = sum(1 for x in todos if x.completed)
        rate = completed / len(todos) * 100 if todos else 0
        yield event.plain_result(
            "⚙️ 待办插件设置\n────────────\n"
            f"插件启用：{'是' if self._enabled() else '否'}\n"
            f"默认优先级：{self._cfg('default_priority', '中')}\n"
            f"自助代办：{'开启' if self._cfg('auto_assign', True) else '关闭'}\n"
            f"后台提醒：{'开启' if self._cfg('reminder_enabled', True) else '关闭'}\n"
            f"每日提醒：{'开启' if daily.get('enabled') else '关闭'} ({daily.get('time', '18:00')})\n"
            f"当前待办：{len(todos)}\n完成率：{rate:.1f}%"
        )

    # ---------------- 后台提醒 ----------------
    async def _reminder_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(15)
                if not self._enabled() or not bool(self._cfg("reminder_enabled", True)):
                    continue

                changed = False
                now = _now()
                scopes = self.data.get("scopes", {})
                for scope_key, payload in list(scopes.items()):
                    if not isinstance(payload, dict):
                        continue
                    todos = self._todos(str(scope_key))

                    # 单项提醒：达到时间即发送，不依赖某一分钟恰好命中。
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
                            f"{self._format_item(item)}\n"
                            "完成后发送：完成 <序号> 或 完成 #ID"
                        )
                        try:
                            await self.context.send_message(
                                str(scope_key), MessageChain().message(message)
                            )
                            item.reminder_sent_for = stamp
                            changed = True
                        except Exception as exc:
                            logger.warning(f"[todo] 单项提醒发送失败 scope={scope_key}: {exc}")

                    # 每日提醒：达到设定时间后只发送一次，插件重启后也不会永久错过。
                    daily = payload.get("daily_reminder") if isinstance(payload.get("daily_reminder"), dict) else {}
                    if bool(daily.get("enabled")):
                        clock = _parse_clock(str(daily.get("time", "18:00")))
                        today = now.date().isoformat()
                        last_sent = daily.get("last_sent")
                        if clock and last_sent != today:
                            target = now.replace(hour=clock[0], minute=clock[1], second=0, microsecond=0)
                            pending = [x for x in self._sort(todos) if not x.completed]
                            if pending and now >= target:
                                lines = [f"🔔 每日待办提醒 · {today}", "────────────"]
                                lines.extend(self._format_item(x) for x in pending[:30])
                                if len(pending) > 30:
                                    lines.append(f"……还有 {len(pending) - 30} 项")
                                try:
                                    await self.context.send_message(
                                        str(scope_key), MessageChain().message("\n".join(lines))
                                    )
                                    daily["last_sent"] = today
                                    changed = True
                                except Exception as exc:
                                    logger.warning(f"[todo] 每日提醒发送失败 scope={scope_key}: {exc}")

                if changed:
                    self._save_data_sync()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f"[todo] 后台提醒任务异常退出：{exc}")

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
            logger.error(f"[todo] 卸载保存数据失败：{exc}")
