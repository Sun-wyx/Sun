import json
import mimetypes
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from wsgiref.simple_server import make_server

BASE_DIR = Path(__file__).resolve().parent
NOMINATIM_URL = os.getenv("NOMINATIM_URL", "https://nominatim.openstreetmap.org/search")
OSRM_URL = os.getenv("OSRM_URL", "https://router.project-osrm.org/route/v1/driving")
USER_AGENT = os.getenv("APP_USER_AGENT", "rent-decision-app/2.0 contact@example.com")

ENVIRONMENT_LABELS = {
    "satisfied": "比较满意",
    "acceptable": "可以接受",
    "verify": "还需实地核实",
}
AMENITY_LABELS = {
    "convenient": "比较便利",
    "average": "基本够用",
    "inconvenient": "不太便利",
    "verify": "还需实地核实",
}
PRIORITY_LABELS = {
    "balanced": "预算与通勤均衡",
    "commute": "通勤优先",
    "cost": "租金优先",
    "living": "居住体验优先",
}


class ExternalServiceError(RuntimeError):
    """第三方接口调用失败。"""


def http_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
    timeout: int = 30,
    service_name: str = "外部接口",
) -> Any:
    request_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        **(headers or {}),
    }
    body = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")

    req = Request(url, data=body, headers=request_headers, method=method)
    try:
        with urlopen(req, timeout=timeout) as response:
            raw = response.read()
            status = getattr(response, "status", 200)
            content_type = response.headers.get("Content-Type", "")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:240].strip()
        suffix = f"：{detail}" if detail else ""
        raise ExternalServiceError(f"{service_name}返回 HTTP {exc.code}{suffix}") from exc
    except (URLError, TimeoutError) as exc:
        raise ExternalServiceError(f"{service_name}连接失败：{exc}") from exc

    if not raw.strip():
        raise ExternalServiceError(f"{service_name}返回了空内容，请稍后重试")

    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        preview = " ".join(text[:160].split())
        raise ExternalServiceError(
            f"{service_name}返回的不是有效 JSON（HTTP {status}，{content_type or '未知类型'}）：{preview or '空内容'}"
        ) from exc


def geocode(address: str) -> dict[str, Any]:
    address = address.strip()
    if not address:
        raise ValueError("地址不能为空")
    query = urlencode(
        {
            "q": address,
            "format": "jsonv2",
            "limit": 1,
            "accept-language": "zh-CN,zh,en",
        }
    )
    results = http_json(
        f"{NOMINATIM_URL}?{query}",
        timeout=25,
        service_name="地址解析服务",
    )
    if not isinstance(results, list) or not results:
        raise ValueError(f"未找到地址：{address}。请补充城市、区县或道路名称")
    item = results[0]
    return {
        "lat": float(item["lat"]),
        "lon": float(item["lon"]),
        "display_name": item.get("display_name", address),
    }


def route(origin: dict[str, Any], destination: dict[str, Any]) -> dict[str, float]:
    coordinates = f'{origin["lon"]},{origin["lat"]};{destination["lon"]},{destination["lat"]}'
    query = urlencode({"overview": "false", "steps": "false"})
    payload = http_json(
        f"{OSRM_URL.rstrip('/')}/{coordinates}?{query}",
        timeout=25,
        service_name="通勤路线服务",
    )
    if not isinstance(payload, dict) or payload.get("code") != "Ok" or not payload.get("routes"):
        message = payload.get("message") if isinstance(payload, dict) else None
        raise ExternalServiceError(f"路线服务暂时不可用{f'：{message}' if message else ''}")
    best = payload["routes"][0]
    return {
        "distance_km": round(float(best["distance"]) / 1000, 1),
        "duration_min": round(float(best["duration"]) / 60, 1),
    }


def require_positive_number(value: Any, field_name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name}必须填写数字") from exc
    if number <= 0:
        raise ValueError(f"{field_name}必须大于 0")
    return number


def option_status(option: dict[str, Any], budget: float, max_commute: float) -> dict[str, Any]:
    rent = float(option["rent"])
    duration = float(option["duration_min"])
    rent_ratio = rent / budget
    commute_ratio = duration / max_commute

    if rent <= budget:
        budget_text = f"预算内，余量约 ¥{round(budget - rent):,}"
        budget_level = 0
    elif rent <= budget * 1.10:
        budget_text = f"略超预算 {round((rent_ratio - 1) * 100)}%"
        budget_level = 1
    else:
        budget_text = f"超预算 {round((rent_ratio - 1) * 100)}%"
        budget_level = 2

    if duration <= max_commute:
        commute_text = f"在可接受通勤内，余量约 {round(max_commute - duration)} 分钟"
        commute_level = 0
    elif duration <= max_commute * 1.15:
        commute_text = f"略超通勤上限 {round(duration - max_commute)} 分钟"
        commute_level = 1
    else:
        commute_text = f"超通勤上限 {round(duration - max_commute)} 分钟"
        commute_level = 2

    env = option["environment"]
    amenity = option["amenities"]
    verify_count = int(env == "verify") + int(amenity == "verify")
    weak_count = int(amenity == "inconvenient")

    severe = rent_ratio > 1.20 or commute_ratio > 1.30
    hard_violations = int(rent > budget) + int(duration > max_commute)

    if severe or hard_violations == 2:
        decision = "不建议优先"
        tier = 3
    elif hard_violations == 1:
        decision = "可作为备选"
        tier = 2
    elif verify_count > 0 or weak_count > 0:
        decision = "先核实再决定"
        tier = 1
    else:
        decision = "优先看房"
        tier = 0

    reasons = [budget_text, commute_text]
    if env == "verify":
        reasons.append("居住环境尚未确认")
    elif env == "satisfied":
        reasons.append("居住环境印象较好")
    if amenity == "verify":
        reasons.append("生活配套尚未确认")
    elif amenity == "inconvenient":
        reasons.append("生活配套可能不足")
    elif amenity == "convenient":
        reasons.append("生活配套较便利")

    return {
        **option,
        "budget_text": budget_text,
        "commute_text": commute_text,
        "decision": decision,
        "decision_tier": tier,
        "reasons": reasons,
        "_budget_level": budget_level,
        "_commute_level": commute_level,
        "_rent_ratio": rent_ratio,
        "_commute_ratio": commute_ratio,
        "_environment_rank": {"satisfied": 0, "acceptable": 1, "verify": 2}[env],
        "_amenity_rank": {"convenient": 0, "average": 1, "verify": 2, "inconvenient": 3}[amenity],
    }


def ranking_key(option: dict[str, Any], priority: str) -> tuple[Any, ...]:
    common = (option["decision_tier"],)
    if priority == "commute":
        return common + (
            option["_commute_level"],
            option["_commute_ratio"],
            option["_budget_level"],
            option["_rent_ratio"],
            option["_environment_rank"],
            option["_amenity_rank"],
        )
    if priority == "cost":
        return common + (
            option["_budget_level"],
            option["_rent_ratio"],
            option["_commute_level"],
            option["_commute_ratio"],
            option["_environment_rank"],
            option["_amenity_rank"],
        )
    if priority == "living":
        return common + (
            option["_environment_rank"],
            option["_amenity_rank"],
            option["_commute_level"],
            option["_budget_level"],
            option["_commute_ratio"],
            option["_rent_ratio"],
        )
    # 均衡：先看哪个硬条件偏离更大，再看居住体验；不计算公开总分。
    return common + (
        max(option["_rent_ratio"], option["_commute_ratio"]),
        option["_budget_level"] + option["_commute_level"],
        option["_environment_rank"],
        option["_amenity_rank"],
        option["_rent_ratio"] + option["_commute_ratio"],
    )


def clean_for_response(option: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in option.items() if not key.startswith("_")}


def build_ai_summary(ranked: list[dict[str, Any]], priority: str) -> tuple[str | None, str | None]:
    api_key = os.getenv("LLM_API_KEY")
    base_url = os.getenv("LLM_BASE_URL")
    model = os.getenv("LLM_MODEL")
    if not all([api_key, base_url, model]):
        return None, None

    prompt = (
        "你是租房位置决策助手。下面的候选房源没有使用百分制评分，而是依据预算上限、"
        "通勤上限、用户选择的定性居住体验和决策侧重进行分层排序。请用中文给出不超过220字的建议，"
        "说明首选、备选、需要现场核实的事项。不得杜撰治安、学区、房屋质量或公共交通信息，"
        "不得把排序写成客观结论。\n"
        f"决策侧重：{PRIORITY_LABELS[priority]}\n"
        + json.dumps(ranked, ensure_ascii=False)
    )
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "基于给定数据做审慎比较，不使用总分，不虚构缺失信息。",
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.25,
    }

    try:
        result = http_json(
            f"{base_url.rstrip('/')}/chat/completions",
            method="POST",
            headers={"Authorization": f"Bearer {api_key}"},
            payload=payload,
            timeout=45,
            service_name="AI 分析接口",
        )
        content = result["choices"][0]["message"]["content"]
        if not isinstance(content, str) or not content.strip():
            raise ExternalServiceError("AI 分析接口没有返回有效文字")
        return content.strip(), None
    except (ExternalServiceError, KeyError, IndexError, TypeError) as exc:
        # AI 是可选增强项，失败不能影响地图和排序结果。
        print(f"AI summary unavailable: {exc}", file=sys.stderr)
        return None, f"AI 建议暂不可用：{exc}"


def build_fallback_summary(ranked: list[dict[str, Any]], priority: str) -> str:
    top = ranked[0]
    text = (
        f"按“{PRIORITY_LABELS[priority]}”的侧重，建议先看“{top['name']}”。"
        f"它的判断为“{top['decision']}”：{top['budget_text']}，{top['commute_text']}。"
    )
    pending = [reason for reason in top["reasons"] if "尚未确认" in reason or "可能不足" in reason]
    if pending:
        text += "现场还要重点核实：" + "、".join(pending) + "。"
    text += "排序不代表房屋质量或安全结论，最终仍需实地看房。"
    return text


def analyze(payload: dict[str, Any]) -> dict[str, Any]:
    workplace = geocode(str(payload.get("workplace", "")))
    budget = require_positive_number(payload.get("budget"), "月租预算")
    max_commute = require_positive_number(payload.get("max_commute"), "可接受通勤时间")
    priority = str(payload.get("priority", "balanced"))
    if priority not in PRIORITY_LABELS:
        raise ValueError("决策侧重无效")

    options = payload.get("options")
    if not isinstance(options, list) or not options:
        raise ValueError("至少需要一个候选房源")
    if len(options) > 8:
        raise ValueError("演示版一次最多分析 8 个候选房源")

    evaluated: list[dict[str, Any]] = []
    for index, item in enumerate(options, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"第 {index} 个候选房源格式错误")
        address = str(item.get("address", "")).strip()
        if not address:
            raise ValueError(f"第 {index} 个候选房源缺少地址")
        rent = require_positive_number(item.get("rent"), f"第 {index} 个候选房源月租")
        environment = str(item.get("environment", "verify"))
        amenities = str(item.get("amenities", "verify"))
        if environment not in ENVIRONMENT_LABELS:
            raise ValueError(f"第 {index} 个候选房源的居住环境选项无效")
        if amenities not in AMENITY_LABELS:
            raise ValueError(f"第 {index} 个候选房源的生活配套选项无效")

        location = geocode(address)
        trip = route(location, workplace)
        raw_option = {
            "name": str(item.get("name") or address),
            "address": address,
            "rent": rent,
            "environment": environment,
            "environment_label": ENVIRONMENT_LABELS[environment],
            "amenities": amenities,
            "amenities_label": AMENITY_LABELS[amenities],
            "notes": str(item.get("notes", "")).strip(),
            "lat": location["lat"],
            "lon": location["lon"],
            **trip,
        }
        evaluated.append(option_status(raw_option, budget, max_commute))

    ranked_internal = sorted(evaluated, key=lambda row: ranking_key(row, priority))
    ranked = [clean_for_response(row) for row in ranked_internal]
    ai_summary, ai_warning = build_ai_summary(ranked, priority)

    return {
        "workplace": workplace,
        "priority": priority,
        "priority_label": PRIORITY_LABELS[priority],
        "ranked": ranked,
        "summary": ai_summary or build_fallback_summary(ranked, priority),
        "ai_warning": ai_warning,
        "method_note": "先检查预算和通勤硬条件，再按所选侧重及定性信息排序；内部排序值只用于比较，不作为公开分数。",
    }


def json_response(start_response, status: str, payload: dict[str, Any]) -> Iterable[bytes]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    start_response(
        status,
        [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-store"),
        ],
    )
    return [body]


def file_response(start_response, path: Path) -> Iterable[bytes]:
    resolved = path.resolve()
    if not resolved.is_file() or (resolved != BASE_DIR and BASE_DIR not in resolved.parents):
        return json_response(start_response, "404 Not Found", {"error": "资源不存在"})
    body = resolved.read_bytes()
    mime_type = mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
    headers = [
        ("Content-Type", f"{mime_type}; charset=utf-8" if mime_type.startswith("text/") else mime_type),
        ("Content-Length", str(len(body))),
    ]
    start_response("200 OK", headers)
    return [body]


def application(environ, start_response):
    method = environ.get("REQUEST_METHOD", "GET").upper()
    path = environ.get("PATH_INFO", "/")

    try:
        if method == "GET" and path == "/":
            return file_response(start_response, BASE_DIR / "templates" / "index.html")
        if method == "GET" and path == "/health":
            return json_response(start_response, "200 OK", {"status": "ok"})
        if method == "GET" and path == "/favicon.ico":
            start_response("204 No Content", [("Content-Length", "0")])
            return [b""]
        if method == "GET" and path.startswith("/static/"):
            relative = path.removeprefix("/static/")
            return file_response(start_response, BASE_DIR / "static" / relative)
        if method == "POST" and path == "/api/analyze":
            length = int(environ.get("CONTENT_LENGTH") or 0)
            if length <= 0:
                raise ValueError("请求内容为空")
            if length > 1_000_000:
                raise ValueError("请求体过大")
            body = environ["wsgi.input"].read(length)
            payload = json.loads(body.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("请求格式错误")
            return json_response(start_response, "200 OK", analyze(payload))
        return json_response(start_response, "404 Not Found", {"error": "接口不存在"})
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        return json_response(start_response, "400 Bad Request", {"error": str(exc)})
    except ExternalServiceError as exc:
        return json_response(start_response, "502 Bad Gateway", {"error": str(exc)})
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        return json_response(start_response, "500 Internal Server Error", {"error": f"服务异常：{exc}"})


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    print(f"Rent Decision App running on http://0.0.0.0:{port}")
    with make_server("0.0.0.0", port, application) as server:
        server.serve_forever()
