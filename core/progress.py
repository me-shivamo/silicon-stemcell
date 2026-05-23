import json
import time


PROGRESS_SCHEMA_VERSION = 1


def now_ms():
    return int(time.time() * 1000)


def compact(text, limit=240):
    text = " ".join(str(text or "").split())
    if len(text) > limit:
        return text[:limit - 1] + "..."
    return text


def progress_event(provider, kind, **fields):
    event = {
        "schema": "silicon.progress",
        "version": PROGRESS_SCHEMA_VERSION,
        "provider": provider,
        "kind": kind,
        "ts_ms": now_ms(),
    }
    for key, value in fields.items():
        if value is not None and value != "":
            event[key] = value
    return event


def write_progress_line(path, event):
    if not path or not event:
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")


def stringify_command(command):
    if isinstance(command, list):
        return " ".join(str(part) for part in command)
    return str(command or "")


def claude_progress_events(event):
    etype = event.get("type", "")
    events = []

    if etype == "system" and event.get("subtype") == "init":
        events.append(progress_event(
            "claude",
            "session",
            session_id=event.get("session_id"),
            model=event.get("model"),
            cwd=event.get("cwd"),
        ))

    elif etype == "rate_limit_event":
        info = event.get("rate_limit_info", {})
        events.append(progress_event(
            "claude",
            "rate_limit",
            status=info.get("status"),
            resets_at=info.get("resetsAt"),
            rate_limit_type=info.get("rateLimitType"),
        ))

    elif etype == "assistant":
        usage = event.get("message", {}).get("usage") or {}
        for block in event.get("message", {}).get("content", []):
            btype = block.get("type", "")
            if btype == "thinking":
                events.append(progress_event("claude", "thinking"))
            elif btype == "tool_use":
                events.append(progress_event(
                    "claude",
                    "tool_start",
                    item_id=block.get("id"),
                    tool_name=block.get("name"),
                    command=stringify_command((block.get("input") or {}).get("command")),
                    description=(block.get("input") or {}).get("description"),
                ))
            elif btype == "text":
                text = block.get("text", "")
                events.append(progress_event("claude", "text", text=text, preview=compact(text)))
        if usage:
            events.append(progress_event(
                "claude",
                "tokens",
                input_tokens=usage.get("input_tokens"),
                output_tokens=usage.get("output_tokens"),
                cache_creation_input_tokens=usage.get("cache_creation_input_tokens"),
                cache_read_input_tokens=usage.get("cache_read_input_tokens"),
            ))

    elif etype == "user":
        for block in event.get("message", {}).get("content", []):
            if block.get("type") == "tool_result":
                content = block.get("content", "")
                events.append(progress_event(
                    "claude",
                    "tool_result",
                    item_id=block.get("tool_use_id"),
                    is_error=block.get("is_error", False),
                    output=content,
                    preview=compact(content),
                ))

    elif etype == "result":
        result = event.get("result", "")
        events.append(progress_event(
            "claude",
            "done",
            status=event.get("subtype") or ("error" if event.get("is_error") else "success"),
            is_error=event.get("is_error", False),
            duration_ms=event.get("duration_ms"),
            cost_usd=event.get("total_cost_usd") or event.get("cost_usd"),
            text=result,
            preview=compact(result),
        ))

    return events


def codex_item_label(item):
    item_type = item.get("type", "item")
    if item_type == "commandExecution":
        return stringify_command(item.get("command") or item.get("cmd") or item.get("argv"))
    if item_type == "fileChange":
        changes = item.get("changes") or []
        paths = [str(change.get("path")) for change in changes if change.get("path")]
        return ", ".join(paths) if paths else item.get("path") or item.get("filePath") or "file change"
    if item_type == "mcpToolCall":
        return f"{item.get('server') or item.get('serverName') or '?'}.{item.get('tool') or item.get('name') or '?'}"
    if item_type == "dynamicToolCall":
        return str(item.get("tool") or "dynamic tool")
    if item_type == "webSearch":
        return str(item.get("query") or item.get("action") or "web search")
    if item_type == "imageView":
        return str(item.get("path") or "image")
    if item_type == "agentMessage":
        phase = item.get("phase")
        return f"assistant {phase}" if phase else "assistant"
    return item_type


def codex_progress_event(msg, state=None):
    state = state if state is not None else {}
    method = msg.get("method", "")
    params = msg.get("params") or {}

    if method == "thread/started":
        thread = params.get("thread") or {}
        return progress_event(
            "codex",
            "session",
            session_id=thread.get("id"),
            model_provider=thread.get("modelProvider"),
            cwd=thread.get("cwd"),
        )

    if method == "turn/started":
        turn = params.get("turn") or {}
        return progress_event("codex", "turn_start", turn_id=turn.get("id"))

    if method == "turn/completed":
        turn = params.get("turn") or {}
        return progress_event(
            "codex",
            "done",
            turn_id=turn.get("id"),
            status=turn.get("status"),
            duration_ms=turn.get("durationMs"),
            error=(turn.get("error") or {}).get("message") if isinstance(turn.get("error"), dict) else None,
        )

    if method == "item/started":
        item = params.get("item") or {}
        item_id = item.get("id") or params.get("itemId")
        item_type = item.get("type", "item")
        label = codex_item_label(item)
        if item_id:
            state.setdefault("item_types", {})[item_id] = item_type
            state.setdefault("item_labels", {})[item_id] = label
        if item_type == "userMessage":
            return progress_event("codex", "input", item_id=item_id)
        if item_type == "agentMessage":
            return progress_event("codex", "text_start", item_id=item_id, label=label)
        if item_type == "commandExecution":
            return progress_event("codex", "tool_start", item_id=item_id, tool_type="command", command=label, cwd=item.get("cwd"))
        if item_type == "fileChange":
            return progress_event("codex", "file_start", item_id=item_id, path=label)
        if item_type in {"mcpToolCall", "dynamicToolCall", "collabToolCall"}:
            return progress_event("codex", "tool_start", item_id=item_id, tool_type=item_type, tool_name=label)
        if item_type == "webSearch":
            return progress_event("codex", "web_start", item_id=item_id, query=label)
        if item_type in {"reasoning", "plan"}:
            return progress_event("codex", f"{item_type}_start", item_id=item_id)
        return progress_event("codex", "item_start", item_id=item_id, item_type=item_type, label=label)

    if method == "item/completed":
        item = params.get("item") or {}
        item_id = item.get("id") or params.get("itemId")
        item_type = item.get("type") or state.get("item_types", {}).get(item_id, "item")
        label = codex_item_label(item) if item else state.get("item_labels", {}).get(item_id, "item")
        status = item.get("status")
        if item_type == "userMessage":
            return progress_event("codex", "input_done", item_id=item_id)
        if item_type == "agentMessage":
            text = item.get("text", "")
            return progress_event("codex", "text", item_id=item_id, text=text, preview=compact(text))
        if item_type == "commandExecution":
            output = item.get("aggregatedOutput", "")
            return progress_event(
                "codex",
                "tool_result",
                item_id=item_id,
                tool_type="command",
                command=label,
                status=status,
                exit_code=item.get("exitCode"),
                output=output,
                preview=compact(output),
            )
        if item_type == "fileChange":
            return progress_event("codex", "file_done", item_id=item_id, path=label, status=status)
        if item_type in {"mcpToolCall", "dynamicToolCall", "collabToolCall"}:
            return progress_event("codex", "tool_result", item_id=item_id, tool_type=item_type, tool_name=label, status=status)
        if item_type == "webSearch":
            return progress_event("codex", "web_done", item_id=item_id, query=label)
        if item_type in {"reasoning", "plan"}:
            return progress_event("codex", f"{item_type}_done", item_id=item_id)
        return progress_event("codex", "item_done", item_id=item_id, item_type=item_type, label=label, status=status)

    if method in {"item/agentMessage/delta", "item/commandExecution/outputDelta", "item/fileChange/outputDelta"}:
        delta = params.get("delta", "")
        kind = "text_delta" if method == "item/agentMessage/delta" else "tool_delta"
        return progress_event("codex", kind, item_id=params.get("itemId"), delta=delta, preview=compact(delta))

    if method == "item/reasoning/summaryTextDelta":
        delta = params.get("delta", "")
        return progress_event("codex", "thinking", item_id=params.get("itemId"), summary_delta=delta, preview=compact(delta))

    if method == "item/plan/delta":
        delta = params.get("delta", "")
        return progress_event("codex", "plan_delta", item_id=params.get("itemId"), delta=delta, preview=compact(delta))

    if method == "item/fileChange/patchUpdated":
        return progress_event("codex", "file_patch", item_id=params.get("itemId"), path=params.get("path") or params.get("filePath"))

    if method == "thread/tokenUsage/updated":
        usage = (params.get("tokenUsage") or {}).get("total") or {}
        return progress_event(
            "codex",
            "tokens",
            total_tokens=usage.get("totalTokens"),
            input_tokens=usage.get("inputTokens"),
            cached_input_tokens=usage.get("cachedInputTokens"),
            output_tokens=usage.get("outputTokens"),
            reasoning_output_tokens=usage.get("reasoningOutputTokens"),
        )

    if method == "error":
        err = params.get("error") or params
        return progress_event("codex", "error", message=err.get("message") if isinstance(err, dict) else str(err))

    return None


def progress_display_line(event):
    if not event:
        return ""

    provider = event.get("provider", "")
    kind = event.get("kind", "")

    if kind == "session":
        sid = str(event.get("session_id", ""))[:8]
        model = event.get("model") or event.get("model_provider") or ""
        return f"{provider} session {sid}" + (f" | {model}" if model else "")
    if kind == "rate_limit":
        return f"rate limit {event.get('status')}"
    if kind == "turn_start":
        return f"turn start {str(event.get('turn_id', ''))[:8]}"
    if kind in {"thinking", "reasoning_start"}:
        preview = event.get("preview")
        return f"thinking: {preview}" if preview else "thinking"
    if kind == "plan_delta":
        return f"plan: {event.get('preview', '')}"
    if kind == "tool_start":
        label = event.get("command") or event.get("tool_name") or event.get("tool_type") or "tool"
        return f"tool start {compact(label, 160)}"
    if kind == "tool_delta":
        return f"tool output {event.get('preview', '')}"
    if kind == "tool_result":
        label = event.get("command") or event.get("tool_name") or event.get("tool_type") or "tool"
        bits = [f"tool end {compact(label, 120)}"]
        if event.get("status"):
            bits.append(f"status={event.get('status')}")
        if event.get("exit_code") is not None:
            bits.append(f"exit={event.get('exit_code')}")
        if event.get("preview"):
            bits.append(f"output={event.get('preview')}")
        return " ".join(bits)
    if kind in {"file_start", "file_done", "file_patch"}:
        return f"{kind.replace('_', ' ')} {event.get('path', '')}".strip()
    if kind in {"web_start", "web_done"}:
        return f"{kind.replace('_', ' ')} {event.get('query', '')}".strip()
    if kind in {"text_start"}:
        return "text start"
    if kind in {"text", "text_delta"}:
        return f"text {event.get('preview', '')}".strip()
    if kind == "tokens":
        total = event.get("total_tokens")
        if total is None:
            return f"tokens input={event.get('input_tokens')} output={event.get('output_tokens')}"
        return f"tokens total={total} input={event.get('input_tokens')} output={event.get('output_tokens')}"
    if kind == "done":
        parts = [f"done {event.get('status', '')}".strip()]
        if event.get("duration_ms") is not None:
            parts.append(f"{event.get('duration_ms') / 1000:.1f}s")
        if event.get("cost_usd") is not None:
            parts.append(f"${event.get('cost_usd'):.4f}")
        return " ".join(parts)
    if kind == "error":
        return f"error {event.get('message', '')}"

    return kind
