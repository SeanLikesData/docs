#!/usr/bin/env python3
"""
Write the reference part of every API endpoint page from the OpenAPI spec.

Each page keeps its hand-written text. The script sets the title to the method and
path, then writes the parameter, body and response tables and the examples between
the GENERATED markers. Run it again after each API change.

Usage: python3 scripts/generate_api_pages.py [spec_path_or_url]
"""

import glob
import json
import os
import re
import sys
import urllib.request

DOCS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC_URL = "https://api.getbluejay.ai/openapi.json"
BASE_URL = "https://api.getbluejay.ai"
PAGE_GLOBS = ["api-reference/endpoint/*.mdx", "api-reference/webhook/*.mdx"]
IMPORT = 'import { FieldTable } from "/snippets/field-table.jsx";'
BAR_IMPORT = 'import { EndpointBar } from "/snippets/endpoint-bar.jsx";'
START = "{/* GENERATED: from the OpenAPI spec by scripts/generate_api_pages.py. Do not edit by hand. */}"
END = "{/* END GENERATED */}"
MAX_DEPTH = 6
METHODS = ("get", "post", "put", "patch", "delete")
ENDPOINT_DIR = "api-reference/endpoint"
# Routes that get no page. A line is "METHOD /path". Delete a line to give that route a page.
IGNORE_FILE = "scripts/api-ignore.txt"
OTHER_GROUP = "Other"
PRIVATE_SEGMENTS = {"internal", "admin", "health", ".well-known", "ping"}


def is_private(path, op):
    """Routes for staff, health checks and the platform. These never get a page."""
    parts = path.strip("/").split("/")
    if PRIVATE_SEGMENTS & set(parts) or any("bbb" in x for x in parts):
        return True
    return any("internal" in x.get("name", "").lower() for x in op.get("parameters", []))


def load_spec(source):
    if source.startswith("http"):
        with urllib.request.urlopen(source, timeout=60) as r:
            return json.load(r)
    with open(source) as f:
        return json.load(f)


def resolve(spec, node, seen=()):
    """Return (schema, ref name) with $ref and allOf merged in."""
    name = None
    while isinstance(node, dict) and "$ref" in node:
        ref = node["$ref"]
        if ref in seen:
            return {"type": "object", "title": ref.rsplit("/", 1)[-1]}, ref.rsplit("/", 1)[-1]
        seen = seen + (ref,)
        name = ref.rsplit("/", 1)[-1]
        target = spec
        for part in ref.lstrip("#/").split("/"):
            target = target[part]
        node = {**target, **{k: v for k, v in node.items() if k != "$ref"}}
    if isinstance(node, dict) and "allOf" in node:
        merged = {k: v for k, v in node.items() if k != "allOf"}
        props, required = {}, []
        for part in node["allOf"]:
            sub, sub_name = resolve(spec, part, seen)
            name = name or sub_name
            props.update(sub.get("properties", {}))
            required += sub.get("required", [])
            for k, v in sub.items():
                if k not in ("properties", "required"):
                    merged.setdefault(k, v)
        if props:
            merged["properties"] = {**props, **merged.get("properties", {})}
        if required:
            merged["required"] = required + merged.get("required", [])
        node = merged
    return node, name


def non_null(spec, schema, seen=()):
    """Drop the null branch of anyOf/oneOf. Returns (variants, nullable)."""
    schema, _ = resolve(spec, schema, seen)
    for key in ("anyOf", "oneOf"):
        if key in schema:
            variants = [v for v in schema[key] if v.get("type") != "null"]
            nullable = len(variants) < len(schema[key])
            out = []
            for v in variants:
                rv, nm = resolve(spec, v, seen)
                if nm and "title" not in rv:
                    rv = {**rv, "title": nm}
                out.append(rv)
            extra = {k: v for k, v in schema.items() if k not in (key,)}
            if len(out) == 1:
                return [{**out[0], **{k: v for k, v in extra.items() if k in ("description", "default", "example")}}], nullable
            return out or [{}], nullable
    return [schema], False


def type_label(spec, schema, seen=()):
    variants, nullable = non_null(spec, schema, seen)
    labels = []
    for v in variants:
        t = v.get("type")
        if t == "array":
            inner = type_label(spec, v.get("items", {}), seen)
            labels.append(f"{inner}[]" if " " not in inner else f"array of {inner}")
        elif t in ("object", None) and ("properties" in v or v.get("title")) and t != "string":
            labels.append("object")
        elif v.get("format") in ("uuid", "date-time", "date", "uri", "email", "binary"):
            labels.append(f"{t} ({v['format']})" if v["format"] != "binary" else "file")
        else:
            labels.append(t or "any")
    label = " or ".join(dict.fromkeys(labels))
    return label + (" or null" if nullable else "")


def object_children(spec, schema, depth, seen=()):
    """Rows for the properties of an object, or of the items of an array of objects."""
    if depth >= MAX_DEPTH:
        return []
    variants, _ = non_null(spec, schema, seen)
    rows = []
    for v in variants:
        target = v
        if v.get("type") == "array":
            target, _ = resolve(spec, v.get("items", {}), seen)
            inner, _ = non_null(spec, target, seen)
            target = inner[0] if len(inner) == 1 else target
        props = target.get("properties")
        if props:
            rows += field_rows(spec, props, target.get("required", []), depth + 1, seen)
        extra = target.get("additionalProperties")
        if isinstance(extra, dict) and extra:
            rows.append({"name": "{key}", "type": type_label(spec, extra, seen), "required": False,
                         "description": "Any key.", "children": object_children(spec, extra, depth + 1, seen)})
    return rows


# Plain-English rewrites of long spec descriptions. The spec text is also read by agents through
# the MCP server, so it stays as written there. Keys are the exact spec text.
OVERRIDES = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "descriptions.json")))


# Short sidebar and page titles, by page file name. Pages not listed use their description minus articles.
TITLES = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "titles.json")))


def clean(text):
    if not text:
        return ""
    text = re.sub(r"\s+", " ", str(text)).strip()
    text = OVERRIDES.get(text, text)
    return text.replace("—", ",")


def plain(name):
    """A short readable description for a field the spec leaves blank."""
    words = name.replace("-", "_").split("_")
    if name == "id":
        return "Unique ID."
    if len(words) > 1 and words[-1] == "id":
        return "ID of the " + " ".join(words[:-1]) + "."
    if len(words) > 1 and words[-1] == "at":
        return "When it was " + " ".join(words[:-1]) + "."
    if len(words) > 1 and words[-1] == "by":
        return "Who " + " ".join(words[:-1]) + " it."
    common = {
        "name": "Display name.", "description": "Free-text note about this item.", "tags": "Labels you can filter by.",
        "type": "The kind of item.", "status": "Current state.", "email": "Email address.", "url": "Web address.",
        "pinned": "Whether it is pinned to the top of lists.", "enabled": "Whether it is turned on.",
        "settings": "Configuration options.", "metadata": "Extra JSON you attach to the item.", "language": "Language code, such as `en`.",
        "timezone": "Timezone name, such as `America/Los_Angeles`.", "limit": "Maximum number of items to return.",
        "offset": "Number of items to skip.", "page": "Page number, starting at 1.", "page_size": "Items per page.",
        "message": "Human-readable message.", "error": "What went wrong.", "success": "Whether the request worked.",
        "transcript": "The conversation text, turn by turn.", "duration": "Length in seconds.", "count": "Number of items.",
        "total": "Total number of matching items.", "version": "Version number.", "label": "Short display label.",
        "phone_number": "Phone number in international format, such as `+14155550123`.", "start_time": "When it started.", "end_time": "When it ended.",
    }
    common.update({
        "agent_description": "What the agent does. Used to guide test generation.", "goals": "What the agent is meant to achieve.",
        "email_address": "Address the email agent receives mail at.", "email_reply_addresses": "Addresses the agent replies from.",
        "follow_up_sms_sender_numbers": "Numbers the follow-up text is sent from.", "websocket_url": "WebSocket URL Bluejay connects to.",
        "redact_pii": "Whether to hide personal data in transcripts.", "updated_by_api_key": "ID of the API key that last changed it.",
        "keyterms": "Words the speech recognizer should expect, such as product names.", "voice_provider": "Voice provider the agent uses.",
        "websocket_username": "Username for the WebSocket connection.", "websocket_password": "Password for the WebSocket connection.",
        "sip_uri": "SIP address Bluejay calls.", "custom_sip_headers": "Extra SIP headers sent with the call.",
        "sip_username": "Username for the SIP connection.", "sip_password": "Password for the SIP connection.",
        "simulation_notifications": "Where to send simulation results.", "livekit_agent_name": "Name of the LiveKit agent to dispatch.",
        "livekit_metadata": "Metadata sent to the LiveKit agent.", "livekit_customer_tool_topic": "LiveKit topic used for tool calls.",
        "livekit_token_webhook_url": "URL Bluejay calls to get a LiveKit token.", "livekit_token_webhook_auth": "How Bluejay signs in to that URL.",
        "pipecat_agent_name": "Name of the Pipecat agent.", "pipecat_agent_configuration": "Settings passed to the Pipecat agent.",
        "pipecat_room_properties": "Room settings for the Pipecat call.", "http_webhook": "Webhook settings for HTTP text agents.",
        "http_webhook_key": "Secret key sent with webhook requests.", "http_webhook_sync_reply": "Whether the agent answers in the webhook response.",
        "dynamic_sip_webhook_url": "URL Bluejay calls at dial time to get the SIP address.", "mock_tools": "Fake tool results used during simulations.",
        "outbound_trigger_webhook_url": "URL Bluejay calls to make the agent place an outbound call.",
        "outbound_trigger_payload_template": "Body Bluejay sends to that URL.", "dfcx_location": "Google Dialogflow CX region.",
        "amelia_base_url": "Base URL of your Amelia server.", "amelia_domain_code": "Amelia domain code.", "amelia_flow_name": "Amelia flow to test.",
        "amelia_agent_name": "Amelia agent to test.", "bland_call_settings": "Extra settings sent to Bland for each call.",
        "provider_request_data": "Variables for the agent, filled into `{{name}}` tokens.",
    })
    return common.get(name, "")


def field_rows(spec, props, required, depth=0, seen=()):
    rows = []
    for name, raw in props.items():
        schema, _ = resolve(spec, raw, seen)
        variants, _ = non_null(spec, schema, seen)
        first = variants[0] if len(variants) == 1 else schema
        row = {
            "name": name,
            "type": type_label(spec, schema, seen),
            "required": name in required,
            "description": clean(schema.get("description") or first.get("description")) or plain(name),
        }
        default = schema.get("default", first.get("default"))
        if default is not None and not isinstance(default, (dict, list)):
            row["default"] = default
        enum = first.get("enum") or (first.get("items", {}) or {}).get("enum")
        if enum:
            row["values"] = [str(e) for e in enum]
        children = object_children(spec, schema, depth, seen)
        if children:
            row["children"] = children
        rows.append(row)
    return rows


def param_rows(spec, params, where):
    rows = []
    for p in params:
        if p.get("in") != where:
            continue
        schema, _ = resolve(spec, p.get("schema", {}))
        variants, _ = non_null(spec, schema)
        first = variants[0] if len(variants) == 1 else schema
        row = {
            "name": p["name"],
            "type": type_label(spec, schema),
            "required": bool(p.get("required")),
            "description": clean(p.get("description") or schema.get("description") or first.get("description")) or plain(p["name"]),
        }
        default = schema.get("default", first.get("default"))
        if default is not None and not isinstance(default, (dict, list)):
            row["default"] = default
        enum = first.get("enum") or (first.get("items", {}) or {}).get("enum")
        if enum:
            row["values"] = [str(e) for e in enum]
        rows.append(row)
    return rows


SAMPLES = {
    "name": "Refund request test", "agent_name": "Support line", "description": "Checks that the agent handles a refund request.",
    "email": "alex@example.com", "phone_number": "+14155550123", "caller_number": "+14155550123", "url": "https://example.com/webhook",
    "intent": "Ask for a refund on an order from last week.", "persona": "Impatient customer", "status": "completed",
    "created_at": "2026-11-20T09:30:00Z", "updated_at": "2026-11-20T09:45:00Z", "start_time": "2026-11-20T09:30:00Z",
    "end_time": "2026-11-20T09:34:12Z", "language": "en", "timezone": "America/Los_Angeles", "tags": ["billing"],
    "system_prompt": "You are a friendly support agent for Acme.", "first_name": "Alex", "last_name": "Rivera",
}
SAMPLE_IDS = {"id": 4821, "agent_id": 1337, "simulation_id": 2045, "digital_human_id": 9912, "run_id": 7710, "folder_id": 58, "metric_id": 311}


def sample(name, t):
    if t == "string" and name in SAMPLES:
        return SAMPLES[name]
    if t == "array" and name in SAMPLES:
        return SAMPLES[name]
    if t == "integer" and (name in SAMPLE_IDS or name.endswith("_id")):
        return SAMPLE_IDS.get(name, 4821)
    return None


def example(spec, schema, depth=0, seen=(), name=""):
    schema, _ = resolve(spec, schema, seen)
    if "example" in schema:
        return schema["example"]
    if schema.get("examples") and isinstance(schema["examples"], list):
        return schema["examples"][0]
    variants, _ = non_null(spec, schema, seen)
    v = variants[0]
    if "example" in v:
        return v["example"]
    if v.get("enum"):
        return v["enum"][0]
    if "default" in schema and schema["default"] is not None:
        return schema["default"]
    t = v.get("type")
    if depth >= MAX_DEPTH:
        return {} if t == "object" else None
    if t == "array":
        got = sample(name, "array")
        return got if got is not None else [example(spec, v.get("items", {}), depth + 1, seen, name)]
    if t == "object" or "properties" in v:
        out = {k: example(spec, s, depth + 1, seen, k) for k, s in v.get("properties", {}).items()}
        extra = v.get("additionalProperties")
        if not out and isinstance(extra, dict) and extra:
            out = {"key": example(spec, extra, depth + 1, seen)}
        return out
    fmt = v.get("format")
    if t == "string":
        if fmt == "uuid":
            return "3c90c3cc-0d44-4b50-8888-8dd25736052a"
        if fmt == "date-time":
            return "2026-01-01T00:00:00Z"
        if fmt == "date":
            return "2026-01-01"
        if fmt in ("uri", "url"):
            return "https://example.com"
        if fmt == "email":
            return "name@example.com"
        got = sample(name, "string")
        return got if got is not None else "<string>"
    if t == "integer":
        got = sample(name, "integer")
        return got if got is not None else 123
    if t == "number":
        return 1.5
    if t == "boolean":
        return True
    return None


def body_schema(op):
    content = (op.get("requestBody") or {}).get("content", {})
    for ctype in ("application/json", "multipart/form-data", "application/x-www-form-urlencoded"):
        if ctype in content:
            return ctype, content[ctype].get("schema", {}), bool(op["requestBody"].get("required"))
    return None, None, False


def success(op):
    for code in sorted(op.get("responses", {})):
        if code.startswith("2"):
            return code, op["responses"][code]
    return None, None


KEYS_URL = "https://app.getbluejay.ai/settings/api-keys"
# The spec lists HTTPBearer on almost every route, so only these reject API keys by name.
USER_TOKEN_PATHS = {"/v1/uptime/monitors/{monitor_id}/series"}


def auth(path, op):
    """(curl header, table row, note) for the credential the route accepts."""
    schemes = {k for s in op.get("security") or [] for k in s}
    if "scimToken" in schemes:
        return ("Authorization: Bearer $BLUEJAY_SCIM_TOKEN", ("Authorization", "SCIM token as `Bearer <token>`. Not an API key."), "")
    if path in USER_TOKEN_PATHS:
        return ("Authorization: Bearer $BLUEJAY_USER_TOKEN", ("Authorization", "Signed-in user token as `Bearer <token>`. API keys are rejected."), "")
    return ("X-API-Key: $BLUEJAY_API_KEY", ("X-API-Key", "API key required to authenticate requests."), "")


def curl(spec, method, path, op):
    lines = [f"curl --request {method.upper()} \\", f"  --url {BASE_URL}{path} \\"]
    query = [p for p in op.get("parameters", []) if p.get("in") == "query" and p.get("required")]
    if query:
        qs = "&".join(f"{p['name']}=<{p['name']}>" for p in query)
        lines[1] = f"  --url '{BASE_URL}{path}?{qs}' \\"
    lines.append(f'  --header "{auth(path, op)[0]}"')
    ctype, schema, _ = body_schema(op)
    if ctype == "application/json":
        lines[-1] += " \\"
        lines.append("  --header 'Content-Type: application/json' \\")
        body = json.dumps(example(spec, schema), indent=2)
        lines.append("  --data '" + body.replace("'", "'\\''").replace("\n", "\n  ") + "'")
    elif ctype == "multipart/form-data":
        resolved, _ = resolve(spec, schema)
        fields = list(resolved.get("properties", {}).items())
        for i, (name, s) in enumerate(fields):
            lines[-1] += " \\"
            is_file = type_label(spec, s) == "file"
            lines.append(f"  --form {name}=@<file>" if is_file else f"  --form {name}=<{name}>")
    return "\n".join(lines)


def request_parts(spec, method, path, op):
    """Shared pieces for the Python and JavaScript snippets."""
    query = [p["name"] for p in op.get("parameters", []) if p.get("in") == "query" and p.get("required")]
    name, _, var = auth(path, op)[0].partition(": ")
    var = var.replace("Bearer ", "").lstrip("$")
    prefix = "Bearer " if name == "Authorization" else ""
    ctype, schema, _ = body_schema(op)
    return query, name, var, prefix, ctype, schema


def python_example(spec, method, path, op):
    query, name, var, prefix, ctype, schema = request_parts(spec, method, path, op)
    auth_val = f'"{prefix}" + os.environ["{var}"]' if prefix else f'os.environ["{var}"]'
    lines = ["import os", "import requests", "", f"response = requests.{method.lower()}(", f'    "{BASE_URL}{path}",']
    if query:
        lines.append("    params={" + ", ".join(f'"{q}": "<{q}>"' for q in query) + "},")
    lines.append(f'    headers={{"{name}": {auth_val}}},')
    if ctype == "application/json":
        body = json.dumps(example(spec, schema), indent=4)
        body = re.sub(r'(?m)(: |^\s*)(true|false|null)(,?)$', lambda m: m[1] + {"true": "True", "false": "False", "null": "None"}[m[2]] + m[3], body)
        lines.append("    json=" + body.replace("\n", "\n    ") + ",")
    elif ctype == "multipart/form-data":
        resolved, _ = resolve(spec, schema)
        files, data = [], []
        for n, sch in resolved.get("properties", {}).items():
            if type_label(spec, sch) == "file":
                files.append(f'"{n}": open("<file>", "rb")')
            else:
                data.append(f'"{n}": "<{n}>"')
        if files:
            lines.append("    files={" + ", ".join(files) + "},")
        if data:
            lines.append("    data={" + ", ".join(data) + "},")
    lines += [")", "print(response.json())"]
    return "\n".join(lines)


def javascript_example(spec, method, path, op):
    query, name, var, prefix, ctype, schema = request_parts(spec, method, path, op)
    auth_val = f"`{prefix}${{process.env.{var}}}`" if prefix else f"process.env.{var}"
    url = BASE_URL + path + ("?" + "&".join(f"{q}=<{q}>" for q in query) if query else "")
    lines = []
    headers = [f'"{name}": {auth_val}']
    extra = []
    if ctype == "application/json":
        headers.append('"Content-Type": "application/json"')
        body = json.dumps(example(spec, schema), indent=2).replace("\n", "\n  ")
        extra.append("  body: JSON.stringify(" + body + "),")
    elif ctype == "multipart/form-data":
        resolved, _ = resolve(spec, schema)
        lines.append("const form = new FormData();")
        for n, sch in resolved.get("properties", {}).items():
            if type_label(spec, sch) == "file":
                lines.append(f'form.append("{n}", new Blob(["<file contents>"]), "<file>");')
            else:
                lines.append(f'form.append("{n}", "<{n}>");')
        lines.append("")
        extra.append("  body: form,")
    lines += [f'const response = await fetch("{url}", {{', f'  method: "{method.upper()}",',
              "  headers: { " + ", ".join(headers) + " },"] + extra + ["});", "console.log(await response.json());"]
    return "\n".join(lines)


def table(rows):
    return "<FieldTable rows={" + json.dumps(rows, ensure_ascii=False) + "} />"


def generate(spec, method, path, op):
    parts = [START, ""]
    rail = ["<RequestExample>", "", "```bash cURL", curl(spec, method, path, op), "```", "",
            "```python Python", python_example(spec, method, path, op), "```", "",
            "```javascript JavaScript", javascript_example(spec, method, path, op), "```", "", "</RequestExample>", ""]
    params = op.get("parameters", [])
    for where, heading in (("path", "Path parameters"), ("query", "Query parameters"), ("header", "Headers")):
        rows = param_rows(spec, params, where)
        if where == "header":
            # An API key decides the workspace, so the API ignores X-Organization-Id for key callers.
            rows = [r for r in rows if r["name"] not in ("X-API-Key", "X-Organization-Id", "Authorization")]
            name, desc = auth(path, op)[1]
            rows.insert(0, {"name": name, "type": "string", "required": True, "description": desc})
        if rows:
            parts += [f"## {heading}", "", table(rows), ""]
    ctype, schema, body_required = body_schema(op)
    if schema is not None:
        resolved, _ = resolve(spec, schema)
        rows = field_rows(spec, resolved.get("properties", {}), resolved.get("required", []))
        if not rows:
            rows = [{"name": "body", "type": type_label(spec, schema), "required": body_required,
                     "children": object_children(spec, schema, 0)}]
        note = "" if ctype == "application/json" else f"Send the body as `{ctype}`.\n\n"
        parts += ["## Request body", "", note + table(rows), ""]
    code, resp = success(op)
    resp_schema = None
    if resp:
        resp_schema = (resp.get("content", {}).get("application/json") or {}).get("schema")
        parts += [f"## Response", ""]
        if resp_schema:
            resolved, _ = resolve(spec, resp_schema)
            rows = field_rows(spec, resolved.get("properties", {}), resolved.get("required", []))
            if not rows and resolved.get("type") == "array":
                rows = object_children(spec, resp_schema, -1)
                parts += ["The response is a list. Each item has these fields:" if rows
                          else f"The response is a list of `{type_label(spec, resolved.get('items', {}))}`.", ""]
            elif not rows:
                rows = object_children(spec, resp_schema, -1)
                if not rows:
                    parts += [f"The response is `{type_label(spec, resp_schema)}`.", ""]
            if rows:
                parts += [table(rows), ""]
        else:
            parts += [f"A `{code}` response has no body.", ""]
    statuses = [(c, clean(r.get("description"))) for c, r in sorted(op.get("responses", {}).items())]
    if statuses:
        parts += ["## Status codes", "", "| Status | Meaning |", "|---|---|"]
        parts += [f"| `{c}` | {d} |" for c, d in statuses]
        parts += [""]
    tabs = []
    if resp_schema:
        tabs.append((code, example(spec, resp_schema)))
    declared = op.get("responses", {})
    errors = {
        "400": {"detail": "The request is not valid for the current state."},
        "401": {"detail": "Invalid API key"},
        "404": {"detail": "Not found"},
        "409": {"detail": "The request conflicts with current data."},
        "422": {"detail": [{"loc": ["body", "name"], "msg": "Field required", "type": "missing"}]},
        "429": {"error": "Rate limit exceeded: 100 per 1 minute"},
    }
    for c, body in errors.items():
        # 401 and 429 apply to every route. 404 applies where the path names one item.
        if c in declared or c in ("401", "429") or (c == "404" and "{" in path):
            tabs.append((c, body))
    tabs.sort(key=lambda t: t[0])
    if tabs:
        rail += ["<ResponseExample>", ""]
        for c, body in tabs:
            rail += ["```json " + c, json.dumps(body, indent=2), "```", ""]
        rail += ["</ResponseExample>", ""]
    note = auth(path, op)[2]
    parts[2:2] = rail + ([f"<Note>{note}</Note>", ""] if note else [])
    parts.append(END)
    return "\n".join(parts)


def generate_webhook(spec, name, op):
    parts = [START, ""]
    _, schema, _ = body_schema(op)
    if schema is not None:
        resolved, _ = resolve(spec, schema)
        mapping = (resolved.get("discriminator") or {}).get("mapping", {})
        events = [(event, {"$ref": ref}) for event, ref in mapping.items()] or [("Payload", schema)]
        for event, event_schema in events:
            body, _ = resolve(spec, event_schema)
            rows = field_rows(spec, body.get("properties", {}), body.get("required", []))
            parts += [f"## `{event}`" if mapping else "## Payload", ""]
            if body.get("description"):
                parts += [clean(body["description"]), ""]
            parts += [table(rows), "", "```json Example", json.dumps(example(spec, event_schema), indent=2), "```", ""]
    parts.append(END)
    return "\n".join(parts)


FM_RE = re.compile(r"^---\n(.*?)\n---\n", re.S)
PROMPT_RE = [
    re.compile(r'<Accordion title="Prompt for AI agents".*?\n</Accordion>\n?', re.S),
    re.compile(r'<div className="ai-prompt-box">.*?````\n</div>\n?', re.S),
]


def set_key(fm, key, value):
    line = f'{key}: "{value}"'
    if re.search(rf"^{key}:", fm, re.M):
        return re.sub(rf"^{key}:.*$", line.replace("\\", "\\\\"), fm, flags=re.M)
    return fm + "\n" + line


def operation_of(fm):
    m = re.search(r'^openapi:\s*["\']?([A-Z]+) ([^"\'\n]+)', fm, re.M)
    if m:
        return m.group(1), m.group(2).strip()
    m = re.search(r'^api:\s*["\']?([A-Z]+) https?://[^/]+([^"\'\n]+)', fm, re.M)
    if m:
        return m.group(1), m.group(2).strip()
    return None


def process(spec, page):
    text = open(page).read()
    m = FM_RE.match(text)
    if not m:
        return "no frontmatter"
    fm, body = m.group(1), text[m.end():]
    for rx in PROMPT_RE:
        body = rx.sub("", body)
    body = re.sub(re.escape(START) + r".*?" + re.escape(END) + r"\n?", "", body, flags=re.S)
    body = body.replace(IMPORT + "\n", "").replace(BAR_IMPORT + "\n", "")
    body = re.sub(r"<EndpointBar [^>]*/>\n*", "", body).strip("\n")
    bar = ""

    webhook = re.search(r'^(?:openapi:\s*["\']?WEBHOOK |webhook:\s*["\']?)(\w+)["\']?$', fm, re.M)
    if webhook:
        op = next(iter(spec.get("webhooks", {}).get(webhook.group(1), {}).values()), None)
        if not op:
            return "webhook not in spec"
        fm = re.sub(r"^openapi:.*\n?", "", fm, flags=re.M).rstrip("\n")
        fm = set_key(fm, "webhook", webhook.group(1))
        generated = generate_webhook(spec, webhook.group(1), op)
    else:
        found = operation_of(fm)
        if not found:
            return "no operation"
        method, path = found
        op = spec["paths"].get(path, {}).get(method.lower())
        if not op:
            return f"{method} {path} not in spec"
        fm = re.sub(r"^openapi:.*\n?", "", fm, flags=re.M).rstrip("\n")
        d = re.search(r'^description:\s*"(.*)"\s*$', fm, re.M)
        title = TITLES.get(os.path.basename(page)[:-4]) or (d.group(1).rstrip(".") if d else "") or (op.get("summary") or f"{method} {path}")
        title = re.sub(r"\b(a|an|the|your) ", "", title)
        title = title[:1].upper() + title[1:]
        fm = set_key(fm, "title", title)
        fm = set_key(fm, "sidebarTitle", title)
        fm = set_key(fm, "api", f"{method} {BASE_URL}{path}")
        fm = set_key(fm, "playground", "none")
        fm = re.sub(r"^deprecated:.*\n?", "", fm, flags=re.M).rstrip("\n")
        if op.get("deprecated"):
            fm += "\ndeprecated: true"
        generated = generate(spec, method, path, op)
        bar = f'<EndpointBar method="{method.upper()}" path="{path}" />\n\n'

    out = f"---\n{fm}\n---\n\n{IMPORT}\n" + (BAR_IMPORT + "\n" if bar else "") + "\n" + bar
    if body:
        out += body + "\n\n"
    out += generated + "\n"
    if out != text:
        open(page, "w").write(out)
    return None


def nav_endpoints():
    """(group label, slug) for every endpoint page, in API sidebar order."""
    tabs = json.load(open(os.path.join(DOCS_DIR, "docs.json")))["navigation"]["tabs"]
    api = next(t for t in tabs if t["tab"] == "API")

    def walk(group, label):
        for p in group["pages"]:
            if isinstance(p, dict):
                yield from walk(p, p["group"])
            elif "/endpoint/" in p:
                yield label, p

    for top in api["groups"]:
        yield from walk(top, top["group"])


def write_operations(spec):
    ops = []
    for group, slug in nav_endpoints():
        fm = FM_RE.match(open(os.path.join(DOCS_DIR, slug + ".mdx")).read())
        found = operation_of(fm.group(1)) if fm else None
        if not found:
            continue
        method, path = found
        op = spec["paths"].get(path, {}).get(method.lower())
        if not op:
            continue
        params = [{"name": p["name"], "in": p["in"], "required": bool(p.get("required"))}
                  for p in op.get("parameters", []) if p["in"] in ("path", "query")]
        ctype, schema, _ = body_schema(op)
        body = json.dumps(example(spec, schema), indent=2) if ctype == "application/json" else None
        ops.append({"group": group, "slug": slug, "method": method, "path": path,
                    "summary": op.get("summary", ""), "params": params, "body": body,
                    "bodyType": ctype})
    out = os.path.join(DOCS_DIR, "snippets", "api-operations.jsx")
    content = ("// Generated by scripts/generate_api_pages.py from the OpenAPI spec. Do not edit by hand.\n"
               "export const apiOperations = " + json.dumps(ops, ensure_ascii=False) + ";\n")
    if not os.path.exists(out) or open(out).read() != content:
        open(out, "w").write(content)
    return len(ops)


def path_parts(path):
    """Path segments without the version, so /v2/x lands next to /v1/x."""
    parts = path.strip("/").split("/")
    return parts[1:] if parts and re.fullmatch(r"v\d+", parts[0]) else parts


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def nav_groups(tab):
    """(group, index, method, path) for every endpoint page in the tab."""
    def walk(group):
        for i, p in enumerate(group["pages"]):
            if isinstance(p, dict):
                yield from walk(p)
            elif "/endpoint/" in p:
                fm = FM_RE.match(open(os.path.join(DOCS_DIR, p + ".mdx")).read())
                found = operation_of(fm.group(1)) if fm else None
                if found:
                    yield group, i, found[0], found[1]

    for top in tab["groups"]:
        yield from walk(top)


def stub_page(spec, method, path, op):
    summary = clean(op.get("summary")) or f"{method} {path}"
    intro = clean((op.get("description") or "").split("\n\n")[0]) or summary + "."
    page = f'---\ndescription: "{summary.replace(chr(34), chr(39))}"\napi: "{method} {BASE_URL}{path}"\n---\n\n{intro}\n'
    slug = slugify(summary)
    version = path.strip("/").split("/")[0]
    candidates = [slug, f"{slug}-{version}", f"{slug}-{version}-{method.lower()}"]
    candidates += [f"{candidates[-1]}-{n}" for n in range(2, 100)]
    slug = next(c for c in candidates if not os.path.exists(os.path.join(DOCS_DIR, ENDPOINT_DIR, c + ".mdx")))
    open(os.path.join(DOCS_DIR, ENDPOINT_DIR, slug + ".mdx"), "w").write(page)
    return f"{ENDPOINT_DIR}/{slug}"


def add_new_pages(spec):
    """Stub a page and a nav entry for each spec route with no page that is not in IGNORE_FILE."""
    ignore_path = os.path.join(DOCS_DIR, IGNORE_FILE)
    ignored = {ln.strip() for ln in open(ignore_path) if ln.strip() and not ln.startswith("#")}
    docs = json.load(open(os.path.join(DOCS_DIR, "docs.json")))
    tab = next(t for t in docs["navigation"]["tabs"] if t["tab"] == "API")
    have = set()
    for page in glob.glob(os.path.join(DOCS_DIR, ENDPOINT_DIR, "*.mdx")):
        fm = FM_RE.match(open(page).read())
        found = operation_of(fm.group(1)) if fm else None
        if found:
            have.add(found)
    new = sorted((p, m.upper()) for p, item in spec["paths"].items() for m in item
                 if m in METHODS and (m.upper(), p) not in have and f"{m.upper()} {p}" not in ignored
                 and not is_private(p, item[m]))
    added = []
    for path, method in new:
        slug = stub_page(spec, method, path, spec["paths"][path][method.lower()])
        best, score = None, 0
        for group, i, m, p in nav_groups(tab):
            a, b = path_parts(path), path_parts(p)
            common = next((k for k, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
            if common > score or (common == score and best and group is best[0]):
                best, score = (group, i), common
        if best:
            best[0]["pages"].insert(best[1] + 1, slug)
        else:
            other = next((g for g in tab["groups"] if g["group"] == OTHER_GROUP), None)
            if not other:
                other = {"group": OTHER_GROUP, "pages": []}
                tab["groups"].append(other)
            other["pages"].append(slug)
        added.append(f"{method} {path} -> {slug}")
    if added:
        open(os.path.join(DOCS_DIR, "docs.json"), "w").write(json.dumps(docs, indent=2, ensure_ascii=False) + "\n")
    return added


def action_rank(method, title):
    """Order inside a sidebar section: create, list, get, update, delete, then other actions."""
    t = title.lower()
    if t.startswith("delete") or method == "DELETE":
        return 4
    if t.startswith(("create", "provision", "schedule", "start", "import")) or t == "add an agent":
        return 0
    if method == "GET":
        return 1 if t.startswith("list") else 2
    if method in ("PUT", "PATCH") or t.startswith(("update", "move")):
        return 3
    return 5


def sort_nav():
    """Sort each sidebar section so every section reads the same way. Deprecated pages go last."""
    path = os.path.join(DOCS_DIR, "docs.json")
    nav = json.load(open(path))
    meta = {}
    for page in glob.glob(os.path.join(DOCS_DIR, ENDPOINT_DIR, "*.mdx")):
        text = open(page).read()
        api = re.search(r'^api: "(\w+) ', text, re.M)
        title = re.search(r'^title: "(.*)"$', text, re.M)
        meta[f"{ENDPOINT_DIR}/{os.path.basename(page)[:-4]}"] = (
            api.group(1) if api else "", title.group(1) if title else "", "deprecated: true" in text)

    def walk(node):
        if isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            slots = [i for i, x in enumerate(node) if isinstance(x, str) and x in meta]
            if len(slots) > 1:
                ordered = sorted((node[i] for i in slots), key=lambda x: (meta[x][2], action_rank(meta[x][0], meta[x][1])))
                for i, x in zip(slots, ordered):
                    node[i] = x
            for x in node:
                walk(x)

    walk(nav)
    with open(path, "w") as f:
        f.write(json.dumps(nav, indent=2, ensure_ascii=False) + "\n")


def main():
    if "--sort-nav" in sys.argv:
        sort_nav()
        return
    spec = load_spec(sys.argv[1] if len(sys.argv) > 1 else SPEC_URL)
    added = add_new_pages(spec)
    print(f"new pages: {len(added)}")
    for a in added:
        print("  " + a)
    problems = []
    pages = sorted(p for g in PAGE_GLOBS for p in glob.glob(os.path.join(DOCS_DIR, g)))
    for page in pages:
        err = process(spec, page)
        if err:
            problems.append(f"{os.path.relpath(page, DOCS_DIR)}: {err}")
    # Two pages with one title (a /v1 and its /v2) get a version tag so the sidebar tells them apart.
    seen = {}
    for page in pages:
        text = open(page).read()
        t = re.search(r'^title: "(.*)"$', text, re.M)
        v = re.search(r'^api: "\w+ \S*/(v\d)/', text, re.M)
        if t and v:
            seen.setdefault(t.group(1), []).append((page, text, v.group(1)))
    for title, items in seen.items():
        if len(items) > 1:
            for page, text, ver in items:
                text = re.sub(r'^(title|sidebarTitle): ".*"$', lambda m: f'{m.group(1)}: "{title} ({ver})"', text, count=2, flags=re.M)
                open(page, "w").write(text)
    print(f"pages: {len(pages)}, skipped: {len(problems)}")
    for p in problems:
        print("  " + p)
    print(f"API Playground operations: {write_operations(spec)}")


if __name__ == "__main__":
    main()
