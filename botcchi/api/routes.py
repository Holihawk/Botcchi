"""
管理台 REST API：Assistant / Provider / 会话 / 记忆
M3 无鉴权，面向本机与内网使用；对外暴露前需加访问控制
"""

import re
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select

from .. import __version__
from ..agent.assistant import (
    Assistant,
    GreetingPolicy,
    OutputPolicy,
    delete_assistant,
    save_assistant,
)
from ..config import ModelDetail, ProviderSettings
from ..db.models import ConversationRow, MemoryRow, MessageRow

if TYPE_CHECKING:
    from ..app import AppContext


NAME_PATTERN = re.compile(r"^[a-z0-9_-]{1,32}$")


class AssistantUpdate(BaseModel):
    display_name: str | None = None
    prompt: str | None = None
    model: str | None = None
    fallback_models: list[str] | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    tools: list[str] | None = None
    max_tool_rounds: int | None = None
    policy: OutputPolicy | None = None
    greeting: GreetingPolicy | None = None


class AssistantCreate(BaseModel):
    name: str
    display_name: str = ""
    model: str
    prompt: str = "你乐于助人。"


class AssistantView(BaseModel):
    name: str
    display_name: str
    prompt: str
    model: str
    fallback_models: list[str]
    temperature: float | None
    max_tokens: int | None
    tools: list[str]
    max_tool_rounds: int
    policy: OutputPolicy
    greeting: GreetingPolicy
    is_default: bool = False

    @classmethod
    def from_assistant(cls, a: Assistant, default: str) -> "AssistantView":
        return cls(**a.model_dump(), is_default=a.name == default)


class ProviderCreate(BaseModel):
    name: str
    api_base: str
    api_type: str = "openai"
    api_keys: list[str] = Field(default_factory=list)
    models: list[ModelDetail] = Field(default_factory=list)


class ProviderUpdate(BaseModel):
    api_base: str | None = None
    models: list[ModelDetail] | None = None
    add_keys: list[str] | None = None
    remove_key_indexes: list[int] | None = None


class ReorderRequest(BaseModel):
    names: list[str]


class LevelPut(BaseModel):
    user_id: str
    group_id: str | None = None
    level: int = Field(ge=0, le=998)


class GroupPut(BaseModel):
    active: bool | None = None
    su_blocked: list[str] | None = None


def _mask_key(key: str) -> str:
    return f"{key[:6]}…{key[-4:]}" if len(key) > 12 else "···"


def _provider_view(p: ProviderSettings) -> dict[str, Any]:
    return {
        "name": p.name,
        "api_type": p.api_type,
        "api_base": p.api_base,
        "key_count": len(p.api_keys),
        "key_masks": [_mask_key(k) for k in p.api_keys],
        "models": [m.model_dump(exclude_none=True) for m in p.models],
    }


def build_management_router(ctx: "AppContext") -> APIRouter:
    router = APIRouter(prefix="/api")
    settings = ctx.settings

    # ---- 状态 -----------------------------------------------------------

    @router.get("/status")
    async def status() -> dict[str, Any]:
        return {
            "version": __version__,
            "default_assistant": settings.default_assistant,
            "storage": "database" if ctx.db else "memory",
            "memory_enabled": ctx.service.memory is not None,
            "assistants": list(ctx.service.assistants),
            "providers": [p.name for p in settings.providers],
            "mcp": [
                {
                    "name": s.name,
                    "connected": s.connected,
                    "tools": s.tools,
                    "error": s.error,
                }
                for s in (ctx.mcp.states.values() if ctx.mcp else [])
            ],
        }

    # ---- Assistant ------------------------------------------------------

    @router.get("/assistants")
    async def list_assistants() -> list[AssistantView]:
        return [
            AssistantView.from_assistant(a, settings.default_assistant)
            for a in ctx.service.assistants.values()
        ]

    @router.get("/tools")
    async def list_tools() -> list[dict[str, str]]:
        loop_tools = ctx.service.loop.tools
        return [
            {"name": d["function"]["name"], "description": d["function"]["description"]}
            for d in loop_tools.definitions()
        ]

    def _validate_model_refs(assistant: Assistant) -> None:
        for ref in [assistant.model, *assistant.fallback_models]:
            try:
                ctx.providers.resolve(ref)
            except Exception as e:
                raise HTTPException(422, f"模型引用无效: {e}") from e

    @router.put("/assistants/{name}")
    async def update_assistant(name: str, patch: AssistantUpdate) -> AssistantView:
        current = ctx.service.assistants.get(name)
        if current is None:
            raise HTTPException(404, f"Assistant 不存在: {name}")
        updated = current.model_copy(
            update=patch.model_dump(exclude_unset=True, exclude_none=True)
        )
        _validate_model_refs(updated)
        save_assistant(settings.assistants_dir, updated)
        ctx.service.assistants[name] = updated  # 热更新，无需重启
        return AssistantView.from_assistant(updated, settings.default_assistant)

    @router.post("/assistants", status_code=201)
    async def create_assistant(req: AssistantCreate) -> AssistantView:
        if not NAME_PATTERN.match(req.name):
            raise HTTPException(422, "名称仅允许小写字母/数字/-/_，1-32 位")
        if req.name in ctx.service.assistants:
            raise HTTPException(409, f"Assistant 已存在: {req.name}")
        assistant = Assistant(
            name=req.name,
            display_name=req.display_name,
            model=req.model,
            prompt=req.prompt,
            prompt_file=f"{req.name}.prompt.md",
        )
        _validate_model_refs(assistant)
        save_assistant(settings.assistants_dir, assistant)
        ctx.service.assistants[req.name] = assistant
        return AssistantView.from_assistant(assistant, settings.default_assistant)

    @router.delete("/assistants/{name}")
    async def remove_assistant(name: str) -> dict[str, str]:
        assistant = ctx.service.assistants.get(name)
        if assistant is None:
            raise HTTPException(404, f"Assistant 不存在: {name}")
        if name == settings.default_assistant:
            raise HTTPException(409, "默认人格不能删除（可先在配置中更换默认人格）")
        delete_assistant(settings.assistants_dir, assistant)
        del ctx.service.assistants[name]
        return {"status": "deleted"}

    # ---- Provider（可编辑；密钥只出掩码） ----------------------------------

    @router.get("/providers")
    async def list_providers() -> list[dict[str, Any]]:
        return [_provider_view(p) for p in settings.providers]

    def _find_provider(name: str) -> ProviderSettings:
        for p in settings.providers:
            if p.name == name:
                return p
        raise HTTPException(404, f"Provider 不存在: {name}")

    @router.post("/providers", status_code=201)
    async def create_provider(req: ProviderCreate) -> dict[str, Any]:
        if not NAME_PATTERN.match(req.name):
            raise HTTPException(422, "名称仅允许小写字母/数字/-/_，1-32 位")
        if any(p.name == req.name for p in settings.providers):
            raise HTTPException(409, f"Provider 已存在: {req.name}")
        if not req.api_keys:
            raise HTTPException(422, "至少需要一个 api_key")
        provider = ProviderSettings(
            name=req.name,
            api_type="openai",
            api_base=req.api_base,
            api_keys=req.api_keys,
            models=req.models,
        )
        ctx.reload_providers([*settings.providers, provider])
        return _provider_view(provider)

    @router.put("/providers/{name}")
    async def update_provider(name: str, patch: ProviderUpdate) -> dict[str, Any]:
        current = _find_provider(name)
        keys = list(current.api_keys)
        if patch.remove_key_indexes:
            keys = [
                k
                for i, k in enumerate(keys)
                if i not in set(patch.remove_key_indexes)
            ]
        if patch.add_keys:
            keys.extend(k.strip() for k in patch.add_keys if k.strip())
        if not keys:
            raise HTTPException(422, "不能删光所有 api_key")
        updated = current.model_copy(
            update={
                "api_base": patch.api_base or current.api_base,
                "api_keys": keys,
                "models": patch.models if patch.models is not None else current.models,
            }
        )
        ctx.reload_providers(
            [updated if p.name == name else p for p in settings.providers]
        )
        return _provider_view(updated)

    @router.delete("/providers/{name}")
    async def remove_provider(name: str) -> dict[str, str]:
        _find_provider(name)
        used_by = [
            a.name
            for a in ctx.service.assistants.values()
            for ref in [a.model, *a.fallback_models]
            if ref.startswith(f"{name}/")
        ]
        if used_by:
            raise HTTPException(
                409, f"Provider 正被人格使用: {', '.join(sorted(set(used_by)))}"
            )
        remaining = [p for p in settings.providers if p.name != name]
        if not remaining:
            raise HTTPException(409, "至少要保留一个 Provider")
        ctx.reload_providers(remaining)
        return {"status": "deleted"}

    @router.post("/providers/reorder")
    async def reorder_providers(req: ReorderRequest) -> list[dict[str, Any]]:
        by_name = {p.name: p for p in settings.providers}
        if set(req.names) != set(by_name):
            raise HTTPException(422, "名称列表与现有 Provider 不一致")
        ctx.reload_providers([by_name[n] for n in req.names])
        return [_provider_view(p) for p in settings.providers]

    # ---- 会话 -------------------------------------------------------------

    def _require_db():
        if ctx.db is None:
            raise HTTPException(409, "未配置 database.url，会话数据不可浏览")
        return ctx.db

    @router.get("/conversations")
    async def list_conversations() -> list[dict[str, Any]]:
        db = _require_db()
        async with db.session() as s:
            rows = (
                await s.execute(
                    select(
                        ConversationRow.session_key,
                        func.count(MessageRow.id).label("message_count"),
                        func.max(MessageRow.created_at).label("last_at"),
                    )
                    .join(MessageRow, MessageRow.conversation_id == ConversationRow.id)
                    .group_by(ConversationRow.session_key)
                    .order_by(func.max(MessageRow.id).desc())
                )
            ).all()
        return [
            {
                "session_key": r.session_key,
                "message_count": r.message_count,
                "last_at": str(r.last_at),
            }
            for r in rows
        ]

    @router.get("/messages")
    async def list_messages(
        session: str = Query(..., description="session_key"),
        limit: int = Query(100, le=500),
    ) -> list[dict[str, Any]]:
        db = _require_db()
        async with db.session() as s:
            conv = await s.scalar(
                select(ConversationRow).where(ConversationRow.session_key == session)
            )
            if conv is None:
                return []
            rows = await s.scalars(
                select(MessageRow)
                .where(MessageRow.conversation_id == conv.id)
                .order_by(MessageRow.id.desc())
                .limit(limit)
            )
            messages = list(rows.all())
        messages.reverse()
        return [
            {
                "id": m.id,
                "role": m.role,
                "content": m.content,
                "reasoning": m.reasoning,
                "created_at": str(m.created_at),
            }
            for m in messages
        ]

    @router.delete("/conversations")
    async def delete_conversation(session: str = Query(...)) -> dict[str, str]:
        db = _require_db()
        async with db.session() as s:
            conv = await s.scalar(
                select(ConversationRow).where(ConversationRow.session_key == session)
            )
            if conv is not None:
                await s.execute(
                    delete(MessageRow).where(MessageRow.conversation_id == conv.id)
                )
                await s.execute(
                    delete(ConversationRow).where(ConversationRow.id == conv.id)
                )
                await s.commit()
        return {"status": "deleted"}

    # ---- 记忆 -------------------------------------------------------------

    @router.get("/memories/scopes")
    async def list_memory_scopes() -> list[dict[str, Any]]:
        db = _require_db()
        async with db.session() as s:
            rows = (
                await s.execute(
                    select(MemoryRow.scope_key, func.count(MemoryRow.id))
                    .group_by(MemoryRow.scope_key)
                    .order_by(MemoryRow.scope_key)
                )
            ).all()
        return [{"scope_key": r[0], "count": r[1]} for r in rows]

    @router.get("/memories")
    async def list_memories(scope: str = Query(...)) -> list[dict[str, Any]]:
        db = _require_db()
        async with db.session() as s:
            rows = await s.scalars(
                select(MemoryRow)
                .where(MemoryRow.scope_key == scope)
                .order_by(MemoryRow.id.desc())
            )
            return [
                {"id": m.id, "content": m.content, "created_at": str(m.created_at)}
                for m in rows.all()
            ]

    @router.delete("/memories/{memory_id}")
    async def delete_memory(memory_id: int) -> dict[str, str]:
        db = _require_db()
        async with db.session() as s:
            await s.execute(delete(MemoryRow).where(MemoryRow.id == memory_id))
            await s.commit()
        return {"status": "deleted"}

    # ---- 调用统计 ---------------------------------------------------------

    @router.get("/stats")
    async def usage_stats(days: int = Query(14, ge=1, le=90)) -> dict[str, Any]:
        _require_db()
        if ctx.service.stats is None:
            raise HTTPException(409, "调用统计不可用")
        return await ctx.service.stats.overview(days)

    # ---- 定时任务 ---------------------------------------------------------

    @router.get("/jobs")
    async def list_jobs() -> list[dict[str, Any]]:
        if ctx.jobs is None:
            raise HTTPException(409, "需要配置 database.url")
        return [
            {
                "id": job.id,
                "kind": job.kind,
                "session_key": str(job.session),
                "next_run_at": job.next_run_at.isoformat(),
                "recurrence": job.recurrence,
                "payload": job.payload,
                "created_by": job.created_by,
            }
            for job in await ctx.jobs.list_jobs()
        ]

    @router.delete("/jobs/{job_id}")
    async def delete_job(job_id: int) -> dict[str, str]:
        if ctx.jobs is None:
            raise HTTPException(409, "需要配置 database.url")
        if not await ctx.jobs.remove(job_id):
            raise HTTPException(404, f"任务 #{job_id} 不存在")
        return {"status": "deleted"}

    # ---- 权限：封禁 / 等级授予 / 群设置 -----------------------------------

    def _require_perms():
        if ctx.perms_store is None:
            raise HTTPException(409, "需要配置 database.url")
        return ctx.perms_store

    @router.get("/perms/features")
    async def list_features() -> list[dict[str, str]]:
        from ..perms import FEATURES

        return [
            {"key": f.key, "description": f.description}
            for f in FEATURES.values()
        ]

    @router.get("/perms/bans")
    async def list_bans() -> list[dict[str, Any]]:
        store = _require_perms()
        return [
            {
                "id": b.id,
                "user_id": b.user_id,
                "group_id": b.group_id,
                "until": b.until.isoformat() if b.until else None,
                "reason": b.reason,
                "operator": b.operator,
                "by_superuser": b.by_superuser,
            }
            for b in await store.list_bans()
        ]

    @router.delete("/perms/bans/{ban_id}")
    async def delete_ban(ban_id: int) -> dict[str, str]:
        store = _require_perms()
        if not await store.remove_ban_by_id(ban_id):
            raise HTTPException(404, f"封禁 #{ban_id} 不存在")
        return {"status": "deleted"}

    @router.get("/perms/levels")
    async def list_levels() -> list[dict[str, Any]]:
        store = _require_perms()
        return [
            {
                "id": g.id,
                "user_id": g.user_id,
                "group_id": g.group_id,
                "level": g.level,
                "granted_by": g.granted_by,
            }
            for g in await store.list_levels()
        ]

    @router.put("/perms/levels")
    async def put_level(req: LevelPut) -> dict[str, str]:
        store = _require_perms()
        await store.set_level(
            req.user_id, req.group_id or None, req.level, granted_by="webui"
        )
        return {"status": "ok"}

    @router.delete("/perms/levels/{grant_id}")
    async def delete_level(grant_id: int) -> dict[str, str]:
        store = _require_perms()
        if not await store.remove_level_by_id(grant_id):
            raise HTTPException(404, f"授权 #{grant_id} 不存在")
        return {"status": "deleted"}

    @router.get("/perms/groups")
    async def list_groups() -> list[dict[str, Any]]:
        store = _require_perms()
        return [
            {
                "group_id": gid,
                "active": state.active,
                "blocked": sorted(state.blocked),
                "su_blocked": sorted(state.su_blocked),
            }
            for gid, state in await store.list_groups()
        ]

    @router.put("/perms/groups/{group_id}")
    async def put_group(group_id: str, req: GroupPut) -> dict[str, str]:
        """WebUI 是超管台：功能开关写超管禁用列表（群管解不开）"""
        store = _require_perms()
        await store.update_group(
            group_id,
            active=req.active,
            su_blocked=set(req.su_blocked) if req.su_blocked is not None else None,
        )
        return {"status": "ok"}

    return router
