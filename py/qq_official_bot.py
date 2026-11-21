"""QQ 官方机器人 WebSocket 接入（最小可用实现）。

只需 AppID/AppSecret，无需公网回调；启动后机器人会显示在线。
凭证从项目根目录 .env 读取：QQ_BOT_APPID / QQ_BOT_SECRET。
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

import botpy
from botpy.message import GroupMessage

ROOT = Path(__file__).resolve().parent.parent
API_BASE = os.environ.get("CWFLY_API_URL", "http://127.0.0.1:3000/api").rstrip("/")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("cwfly.qqbot")

HELP_TEXT = """✨ **CWFLY 青翼助手**

**✈️ 飞行查询**
- `/在线状态` 查看 FSD 在线机组/管制
- `/机组查询 呼号` 查询机组计划与位置
- `/管制查询 席位` 查询在线管制

**🌦️ 航行情报**
- `/机场查询 ICAO` 机场跑道与频率
- `/气象 ICAO` 查询 METAR
- `/预报 ICAO` 查询 TAF
- `/航路 起 降` 计算航路

**🎉 社区**
- `/活动` 查看近期连飞活动
- `/呼叫管制` 提醒在线管制员

**🛠️ 管理员**
- `/绑定播报 口令` 将本群设为管制上线/下线播报群

> 也可以直接 @机器人 提问。"""

def load_env() -> dict[str, str]:
    values: dict[str, str] = {}
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    values.update(os.environ)
    return values


NOTIFY_OPENID_FILE = ROOT / "data" / "qq_notify_group_openid.txt"


def _env_value(key: str, default: str = "") -> str:
    return str(load_env().get(key, default) or "").strip()


def load_notify_openid() -> str:
    """优先环境变量，其次持久化的群 openid。"""
    env_openid = _env_value("QQ_NOTIFY_GROUP_OPENID")
    if env_openid:
        return env_openid
    try:
        return NOTIFY_OPENID_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def save_notify_openid(group_openid: str) -> None:
    NOTIFY_OPENID_FILE.parent.mkdir(parents=True, exist_ok=True)
    NOTIFY_OPENID_FILE.write_text(group_openid.strip(), encoding="utf-8")


def _notify_skipped(controller: dict) -> bool:
    cid = str(controller.get("cid") or "").strip()
    callsign = str(controller.get("callsign") or "").strip().upper()
    skip_cids = {x.strip() for x in _env_value("NOTIFY_SKIP_CIDS").split(",") if x.strip()}
    skip_callsigns = {x.strip().upper() for x in _env_value("NOTIFY_SKIP_CALLSIGNS").split(",") if x.strip()}
    return (cid and cid in skip_cids) or (callsign and callsign in skip_callsigns)


def _format_notify_frequency(freq) -> str:
    raw = str(freq if freq is not None else "").strip()
    try:
        if "." in raw:
            return f"{float(raw):.3f} MHz"
        n = int(float(raw))
    except (TypeError, ValueError):
        return "未知"
    if n <= 0:
        return "未知"
    if n >= 100000:
        return f"{n / 1000:.3f} MHz"
    if n >= 1000:
        return f"{n / 1000:.3f} MHz"
    return f"1{n // 1000:02d}.{n % 1000:03d} MHz"


FACILITY_LABELS = {
    0: "观察员",
    1: "飞行服务",
    2: "放行",
    3: "地面",
    4: "塔台",
    5: "进近",
    6: "区调",
}
RATING_LABELS = {
    1: "OBS", 2: "S1", 3: "S2", 4: "S3", 5: "C1", 6: "C2", 7: "C3",
    8: "I1", 9: "I2", 10: "I3", 11: "SUP", 12: "ADM",
}


def _controller_role(controller: dict) -> str:
    facility = controller.get("facilityType")
    try:
        facility = int(facility)
    except (TypeError, ValueError):
        facility = None
    if facility in FACILITY_LABELS:
        return FACILITY_LABELS[facility]
    rating = controller.get("rating")
    try:
        rating = int(rating)
    except (TypeError, ValueError):
        rating = None
    return RATING_LABELS.get(rating, "管制席位")


def _format_notify_time(start) -> str:
    try:
        sec = float(start)
        if sec > 1e12:
            sec = sec / 1000
        sec = int(sec)
    except (TypeError, ValueError):
        sec = int(time.time())
    if sec <= 0:
        sec = int(time.time())
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(sec))


def build_controller_notice(controller: dict, online: bool) -> str:
    callsign = str(controller.get("callsign") or "").strip()
    cid = str(controller.get("cid") or "").strip()
    realname = str(controller.get("realname") or "").strip()
    role = _controller_role(controller)
    freq = _format_notify_frequency(controller.get("frequency"))
    name_part = f"CID {cid}"
    if realname and realname != cid and realname.upper() != callsign.upper():
        name_part = f"{name_part} · {realname}"
    if online:
        return "\n".join(
            [
                f"🛫 管制上线通知 | {callsign}",
                "━━━━━━━━━━━━━━",
                f"管制员：{name_part}",
                f"席位类型：{role}",
                f"联系频率：{freq}",
                f"上线时间：{_format_notify_time(controller.get('starttime') or controller.get('_detectedAt') or controller.get('lastSeen'))}",
                "",
                "🎧 请范围内机组自主联系",
            ]
        )
    return "\n".join(
        [
            f"🛬 管制下线通知 | {callsign}",
            "━━━━━━━━━━━━━━",
            f"管制员：{name_part}",
            f"席位类型：{role}",
            f"下线时间：{_format_notify_time(time.time())}",
            "",
            "🌙 祝飞行愉快～",
        ]
    )


def http_json(url: str, timeout: int = 15) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "CWFLY-Bot/1.0 (+https://www.cwflysim.cn)",
            "Referer": "http://127.0.0.1:5173/",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def api_get(path: str, **params) -> dict:
    timeout = int(params.pop("_timeout", 15))
    url = API_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return http_json(url, timeout=timeout)


def airport_position(icao: str) -> tuple[float, float] | None:
    try:
        data = api_get(f"/content/{urllib.parse.quote(icao.upper())}").get("data") or {}
        lat, lon = data.get("lat"), data.get("lon")
        if lat is None or lon is None:
            return None
        return float(lat), float(lon)
    except Exception:  # noqa: BLE001
        return None


# 主要机场跑道静态兜底（ICAO: [(跑道编号, 长度米), ...]）
# 仅用于 Overpass 不可达时按风向选择跑道，不用于真实飞行导航。
STATIC_RUNWAYS: dict[str, list[tuple[str, int]]] = {
    "ZBAA": [("18L/36R", 3800), ("18R/36L", 3200), ("01/19", 3800)],
    "ZBAD": [("01L/19R", 3400), ("01R/19L", 3800), ("17L/35R", 3800), ("17R/35L", 3800)],
    "ZBTJ": [("16L/34R", 3600), ("16R/34L", 3200)],
    "ZSPD": [("16L/34R", 4000), ("16R/34L", 3800), ("17L/35R", 4000), ("17R/35L", 3400)],
    "ZSSS": [("18L/36R", 3400), ("18R/36L", 3300)],
    "ZGGG": [("01/19", 3600), ("02L/20R", 3800), ("02R/20L", 3800)],
    "ZGSZ": [("15/33", 3400), ("16/34", 3600)],
    "ZGHA": [("18L/36R", 3800), ("18R/36L", 3800)],
    "ZUUU": [("02L/20R", 3600), ("02R/20L", 3600)],
    "ZUCK": [("02L/20R", 3600), ("02R/20L", 3600), ("03/21", 3800)],
    "ZPPP": [("03/21", 4500), ("04/22", 4000)],
    "ZSHC": [("06/24", 3400), ("07/25", 3600)],
    "ZSNJ": [("06/24", 3600), ("07/25", 3600)],
    "ZSAM": [("05/23", 3400)],
    "ZSFZ": [("03/21", 3600)],
    "ZHHH": [("04L/22R", 3600), ("04R/22L", 3600)],
    "ZLLL": [("18/36", 4000)],
    "ZLXY": [("05L/23R", 3800), ("05R/23L", 3800)],
    "ZWWW": [("07/25", 3600)],
    "ZYTX": [("05/23", 3200), ("06/24", 3200)],
    "ZYTL": [("10/28", 3300)],
    "ZYHB": [("05/23", 3200)],
    "ZJSY": [("08/26", 3400)],
    "ZJHK": [("09/27", 3600)],
    "VHHH": [("07L/25R", 3800), ("07R/25L", 3800)],
    "RCTP": [("05L/23R", 3660), ("05R/23L", 3800)],
    "RJTT": [("16R/34L", 3000), ("16L/34R", 3360), ("04/22", 2500), ("05/23", 2500)],
    "RJBB": [("06L/24R", 4000), ("06R/24L", 3500)],
    "RKSI": [("15L/33R", 3750), ("15R/33L", 3750), ("16/34", 4000)],
    "WSSS": [("02L/20R", 4000), ("02C/20C", 4000), ("02R/20L", 4000)],
    "KLAX": [("06L/24R", 2721), ("06R/24L", 3382), ("07L/25R", 3685), ("07R/25L", 3382)],
    "KSFO": [("01L/19R", 2286), ("01R/19L", 2637), ("10L/28R", 3618), ("10R/28L", 3231)],
    "KJFK": [("04L/22R", 3460), ("04R/22L", 2560), ("13L/31R", 3048), ("13R/31L", 4423)],
    "EGLL": [("09L/27R", 3902), ("09R/27L", 3660)],
}


def _parse_runway_refs(icao: str, refs: list[tuple[str, int]]) -> list[dict]:
    runways: list[dict] = []
    for ref, length in refs:
        for part in ref.split("/"):
            ident = part.strip().upper()
            match = re.fullmatch(r"(\d{1,2})([LRC]?)", ident)
            if not match:
                continue
            number = int(match.group(1)) % 36
            heading = 360 if number == 0 else number * 10
            runways.append({
                "ident": ident,
                "heading": heading,
                "length": length,
                "surface": "",
                "icao": icao,
            })
    return runways


_RUNWAY_CACHE: dict[str, list[dict]] = {}


def fetch_metar_wind(icao: str) -> tuple[int | None, int, bool, str]:
    """返回 (风向, 风速, 是否不定风, METAR 原文)。"""
    try:
        data = api_get("/weather", icao=icao.upper(), _timeout=10).get("data") or {}
        metar = str(data.get("metar") or "")
        match = re.search(r"\b(VRB|\d{3})(\d{2,3})(?:G\d{2,3})?(KT|MPS)\b", metar)
        if not match:
            return None, 0, False, metar
        direction, speed = match.group(1), int(match.group(2))
        variable = direction == "VRB" or speed == 0 or (direction == "000" and speed > 0)
        wind_dir = None if variable else int(direction) % 360
        return wind_dir, speed, variable, metar
    except Exception as exc:  # noqa: BLE001
        log.warning("metar wind query failed for %s: %s", icao, exc)
        return None, 0, False, ""


def fetch_runways(icao: str, lat: float, lon: float) -> list[dict]:
    if icao in _RUNWAY_CACHE:
        return _RUNWAY_CACHE[icao]
    # 先用静态表，避免公共 Overpass 限流影响普通查询
    if icao in STATIC_RUNWAYS:
        runways = _parse_runway_refs(icao, STATIC_RUNWAYS[icao])
        _RUNWAY_CACHE[icao] = runways
        return runways
    query = (
        f'[out:json][timeout:12];way["aeroway"="runway"]'
        f"(around:6000,{lat},{lon});out tags center;"
    )
    url = "https://overpass-api.de/api/interpreter?data=" + urllib.parse.quote(query)
    req = urllib.request.Request(url, headers={"User-Agent": "CWFLY-Bot/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=4) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        log.warning("runway query failed for %s: %s", icao, exc)
        return []
    runways: list[dict] = []
    for element in payload.get("elements") or []:
        tags = element.get("tags") or {}
        ref = str(tags.get("ref") or "").strip().upper()
        if "/" not in ref:
            continue
        try:
            length = int(tags.get("length") or 0)
        except (TypeError, ValueError):
            length = 0
        runways.extend(_parse_runway_refs(icao, [(ref, length)]))
    _RUNWAY_CACHE[icao] = runways
    return runways


def runway_for_wind(
    runways: list[dict], wind_dir: int | None, variable: bool
) -> dict | None:
    if not runways:
        return None
    target = 0 if variable or wind_dir is None else wind_dir

    def score(item: dict) -> tuple[float, int]:
        diff = abs(item["heading"] - target)
        diff = min(diff, 360 - diff)
        return diff, -int(item.get("length") or 0)

    return min(runways, key=score)


def choose_runways(dep: str, arr: str) -> dict:
    """根据 METAR 风向自动选择起降跑道；不定风/无风时向北。"""
    result: dict[str, str] = {}
    notes: list[str] = []
    for icao in (dep, arr):
        pos = airport_position(icao)
        wind_dir, speed, variable, _metar = fetch_metar_wind(icao)
        runway = None
        if pos:
            runway = runway_for_wind(fetch_runways(icao, *pos), wind_dir, variable)
        if runway:
            result["originRunway" if icao == dep else "destinationRunway"] = runway["ident"]
            if variable or wind_dir is None:
                wind = "不定风/无风"
            else:
                wind = f"{wind_dir:03d}°/{speed:02d}"
            notes.append(f"{icao} {runway['ident']}（{wind}）")
        else:
            notes.append(f"{icao} 未取到跑道")
    return {"options": result, "note": " / ".join(notes)}

def great_circle_nm(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * 3440.065 * math.asin(math.sqrt(h))


def initial_bearing(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    dlon = lon2 - lon1
    y = math.sin(dlon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(y, x)) + 360) % 360

def clean_content(content: str) -> str:
    content = re.sub(r"<@!?[0-9A-Za-z_-]+>", " ", content or "")
    return re.sub(r"\s+", " ", content).strip()


def truncate(text: str, limit: int = 1500) -> str:
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    # 截断落在代码块中时补上围栏，避免一条消息把后续 Markdown 全部带坏
    if cut.count("```") % 2:
        cut += "\n```"
    return cut + "…"


def md_error(title: str, hint: str = "") -> str:
    lines = [f"⚠️ **{title}**", ""]
    if hint:
        lines.append(f"> {hint}")
    return "\n".join(lines)


def md_usage(usage: str, example: str = "") -> str:
    lines = ["👉 **用法**", "", f"`{usage}`"]
    if example:
        lines.append(f"> 示例：`{example}`")
    return "\n".join(lines)


def plain_text(markdown: str) -> str:
    """Markdown 发送失败时的纯文本兜底。"""
    text = re.sub(r"```(?:text)?\n?", "", markdown or "")
    text = text.replace("```", "")
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"^###\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^>\s?", "", text, flags=re.MULTILINE)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


async def send_markdown_reply(message, text: str, msg_seq: int) -> None:
    """优先发送 QQ Markdown；失败时自动降级为纯文本。"""
    fallback = plain_text(text)
    try:
        # QQ 官方文档：传 markdown 时 content 必须为空，否则消息类型与内容不匹配
        await message.reply(
            msg_type=2,
            markdown={"content": text},
            msg_seq=msg_seq,
        )
    except Exception:  # noqa: BLE001
        log.exception("markdown reply failed, fallback to plain text")
        await message.reply(content=fallback, msg_seq=msg_seq)


NAV_ROUTE_API = "https://www.ccflysim.cn/api/bot/nav/route"


def query_bot_route(origin: str, destination: str, **options) -> dict:
    """调用 CTYFLY 官方机器人航路 API。"""
    params = {
        "origin": origin.upper(),
        "destination": destination.upper(),
    }
    params.update({k: v for k, v in options.items() if v})
    url = NAV_ROUTE_API + "?" + urllib.parse.urlencode(params)
    headers = {
        "Accept": "application/json",
        "User-Agent": "CWFLY-Bot/1.0 (+https://www.cwflysim.cn)",
    }
    token = load_env().get("BOT_API_TOKEN", "").strip()
    if token:
        headers["X-API-Key"] = token
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        message = ""
        try:
            body = json.loads(exc.read().decode("utf-8"))
            message = body.get("error") or body.get("message") or ""
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError(f"HTTP {exc.code}: {message or '航路服务错误'}") from exc
    if not isinstance(data, dict) or not data.get("ok"):
        raise RuntimeError(str((data or {}).get("error") or "航路服务返回失败"))
    return data

def handle_command(content: str) -> str:
    parts = content.split()
    if not parts:
        return md_error("缺少指令内容", "发送 `/帮助` 查看全部可用指令。")
    cmd = parts[0].lower()
    args = parts[1:]

    if cmd in {"/帮助", "/help"}:
        return HELP_TEXT

    if cmd in {"/在线状态", "/online"}:
        try:
            pilots = api_get("/flightplans/fsd/online").get("data") or []
            atcs = api_get("/flightplans/fsd/controllers").get("data") or []
            lines = [
                "📡 **FSD 在线状态**",
                "",
                f"- 🛫 **在线机组**：`{len(pilots)}`",
                f"- 🎧 **在线管制**：`{len(atcs)}`",
            ]
            if pilots:
                lines += ["", "**🛫 在线机组**"]
                for item in pilots[:8]:
                    lines.append(
                        f"- 👨‍✈️ **{item.get('callsign') or '-'}** · "
                        f"`{item.get('dep') or '?'} → {item.get('dest') or '?'}`"
                    )
            else:
                lines += ["", "> 🌙 当前没有在线机组。"]
            if atcs:
                lines += ["", "**🎧 在线管制**"]
                for item in atcs[:5]:
                    lines.append(
                        f"- 📡 **{item.get('callsign') or '-'}** · "
                        f"`{item.get('frequency') or '-'}`"
                    )
            else:
                lines += ["", "> 🌙 当前没有在线管制。"]
            return truncate("\n".join(lines))
        except Exception as exc:  # noqa: BLE001
            log.warning("online query failed: %s", exc)
            return md_error("暂时无法获取 FSD 在线数据", "请稍后重试。")

    if cmd in {"/气象", "/metar"}:
        if not args:
            return md_usage("/气象 ZBAA", "/气象 ZSPD")
        code = args[0].upper()
        try:
            data = api_get("/weather", icao=code).get("data") or {}
            metar = data.get("metar") or data.get("raw") or data.get("rawMetar") or "暂无报文"
            return truncate(
                f"🌦 **{code} METAR**\n\n```text\n{metar}\n```"
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("metar query failed: %s", exc)
            return md_error("气象查询失败", "请稍后重试，或检查 ICAO 代码是否正确。")

    if cmd in {"/预报", "/taf"}:
        if not args:
            return md_usage("/预报 ZBAA", "/预报 ZSPD")
        code = args[0].upper()
        try:
            data = api_get("/weather", icao=code).get("data") or {}
            taf = data.get("taf") or data.get("rawTaf") or "暂无预报"
            return truncate(
                f"🌤️ **{code} TAF**\n\n```text\n{taf}\n```"
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("taf query failed: %s", exc)
            return md_error("预报查询失败", "请稍后重试，或检查 ICAO 代码是否正确。")

    if cmd in {"/活动", "/event"}:
        try:
            events = api_get("/events").get("data") or []
            unique_events = []
            seen_event_keys = set()
            for event in events:
                # 后端活动数据可能重复且 id 不同，按展示内容去重
                key = (
                    str(event.get("title") or "").strip(),
                    str(event.get("date") or "").strip(),
                    str(event.get("dep") or "").strip(),
                    str(event.get("arr") or "").strip(),
                )
                if key in seen_event_keys:
                    continue
                seen_event_keys.add(key)
                unique_events.append(event)
            events = unique_events
            if not events:
                return "📅 **近期连飞活动**\n\n> 当前没有近期活动。"
            lines = ["📅 **近期连飞活动**", ""]
            for index, event in enumerate(events[:5], start=1):
                lines.append(f"{index}. 🎉 **{event.get('title') or '未命名活动'}**")
                lines.append(f"    - 🕒 时间：{event.get('date') or '-'}")
                lines.append(
                    f"    - 🗺️ 航路：`{event.get('dep') or '?'} → {event.get('arr') or '?'}`"
                )
            return truncate("\n".join(lines))
        except Exception as exc:  # noqa: BLE001
            log.warning("event query failed: %s", exc)
            return md_error("活动查询失败", "请稍后重试。")

    if cmd in {"/呼叫管制", "/call", "/callcontroller"}:
        try:
            atcs = api_get("/flightplans/fsd/controllers").get("data") or []
            if not atcs:
                return md_error("当前没有在线管制员", "请稍后再呼叫。")
            lines = ["📣 **在线管制**", ""]
            for atc in atcs[:8]:
                lines.append(
                    f"- **{atc.get('callsign') or '-'}** · `{atc.get('frequency') or '-'}`"
                )
            lines += [
                "",
                "> 🗣️ 请说明呼号、位置与需求，等待管制员回复。",
            ]
            return truncate("\n".join(lines))
        except Exception as exc:  # noqa: BLE001
            log.warning("call controller query failed: %s", exc)
            return md_error("已记录呼叫请求", "请在群内说明呼号、位置与需求。")

    if cmd in {"/机组查询", "/pilot"}:
        if not args:
            return md_usage("/机组查询 CCA123", "/机组查询 CCA123")
        try:
            pilots = api_get("/flightplans/fsd/online").get("data") or []
            target = args[0].upper()
            hit = next(
                (p for p in pilots if str(p.get("callsign") or "").upper() == target),
                None,
            )
            if not hit:
                return md_error(f"未找到在线机组 {target}", "请确认呼号后重试。")
            return "\n".join(
                [
                    f"✈️ **{hit.get('callsign')}**",
                    "",
                    f"- 🛩️ 机型：{hit.get('aircraft') or '未知机型'}",
                    f"- 🗺️ 航路：`{hit.get('dep') or '?'} → {hit.get('dest') or '?'}`",
                    f"- 📏 高度：`{hit.get('altitude') or 0} ft`",
                    f"- 💨 地速：`{hit.get('groundspeed') or 0} kt`",
                ]
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("pilot query failed: %s", exc)
            return md_error("机组查询失败", "请稍后重试。")

    if cmd in {"/管制查询", "/controller", "/atc"}:
        if not args:
            return md_usage("/管制查询 ZBAA_TWR", "/管制查询 ZBAA_TWR")
        try:
            atcs = api_get("/flightplans/fsd/controllers").get("data") or []
            target = args[0].upper()
            hit = next(
                (
                    atc
                    for atc in atcs
                    if target
                    in (
                        str(atc.get("callsign") or "").upper(),
                        str(atc.get("frequency") or ""),
                    )
                ),
                None,
            )
            if not hit:
                return md_error(f"未找到在线管制 {target}", "请确认席位或频率后重试。")
            return "\n".join(
                [
                    f"🎧 **{hit.get('callsign')}**",
                    "",
                    f"- 📻 频率：`{hit.get('frequency') or '-'}`",
                    f"- 📡 覆盖范围：`{hit.get('visualRange') or '-'} nm`",
                ]
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("controller query failed: %s", exc)
            return md_error("管制查询失败", "请稍后重试。")

    if cmd in {"/机场查询", "/airport"}:
        if not args:
            return md_usage("/机场查询 ZBAA", "/机场查询 ZSPD")
        code = args[0].upper()
        try:
            data = api_get(f"/content/{urllib.parse.quote(code)}").get("data") or {}
            runways = data.get("runways") or []
            freqs = data.get("frequencies") or []
            if not runways:
                # OpenAIP/本地库没有数据时，用内置主要机场跑道兜底
                runways = [{"ident": item[0]} for item in STATIC_RUNWAYS.get(code, [])]
            name = str(data.get("name") or "").strip()
            title = f"🛫 **{code}{(' ' + name) if name else ''}**"
            lines = [title]
            if runways:
                lines += ["", "**🛣️ 跑道**"]
                for runway in runways[:4]:
                    ident = str(runway.get("ident") or runway)
                    lines.append(f"- `{ident}`")
            else:
                lines += ["", "> ⚠️ 暂无跑道数据。"]
            if freqs:
                lines += ["", "**📡 频率**"]
                for freq in freqs[:6]:
                    label = str(freq.get("type") or freq.get("name") or "").strip()
                    value = str(freq.get("frequency") or freq.get("freq") or "").strip()
                    lines.append(f"- `{label} {value}`".strip())
            else:
                lines += ["", "> ℹ️ 暂无频率数据。"]
            return truncate("\n".join(lines))
        except Exception as exc:  # noqa: BLE001
            log.warning("airport query failed: %s", exc)
            return md_error(f"未找到机场 {code}", "请检查 ICAO 代码，或稍后重试。")

    if cmd in {"/航路", "/route"}:
        if len(args) < 2:
            return md_usage("/航路 ZBAA ZSPD", "/航路 ZBAA ZSPD [起飞跑道] [到达跑道]")
        dep, arr = args[0].upper(), args[1].upper()
        options = {}
        if len(args) >= 3:
            options["originRunway"] = args[2].upper()
        if len(args) >= 4:
            options["destinationRunway"] = args[3].upper()
        auto_note = ""
        if not options:
            auto = choose_runways(dep, arr)
            options = auto["options"]
            auto_note = auto["note"]
        try:
            data = query_bot_route(dep, arr, **options)
            costs = data.get("costs") or {}
            total = costs.get("total")
            lines = [f"🧭 **{dep} → {arr}**", "", f"- 🗓️ AIRAC：`{data.get('cycle') or '-'}`"]
            if auto_note:
                lines.append(f"- 🛫 跑道：{auto_note}")
            if total:
                lines.append(f"- 📏 总距离：`{round(float(total))} nm`")
            route = str(data.get("route") or "").strip()
            if route:
                lines += ["", "**🗺️ 航路**", "```text", route, "```"]
            sid = data.get("sid") or {}
            if sid.get("exit"):
                lines.append(
                    f"- 🛫 SID：`{sid.get('procedure') or '直飞'} → {sid.get('exit')}`"
                )
            star = data.get("star") or {}
            if star.get("entry"):
                lines.append(
                    f"- 🛬 STAR：`{star.get('entry')} → {star.get('procedure') or '直飞'}`"
                )
            alt = data.get("alternatives") or []
            if alt:
                lines += ["", f"**🔀 备选**：{str(alt[0])[:300]}"]
            lines += ["", "> ⚠️ 航路仅供飞行参考，以现场管制指令为准。"]
            return truncate("\n".join(lines))
        except Exception as exc:  # noqa: BLE001
            log.warning("bot nav route failed: %s", exc)
            try:
                local = api_get("/charts/route/local", dep=dep, arr=arr, _timeout=8).get("data") or {}
                return truncate(
                    "\n".join(
                        [
                            f"🧭 **{dep} → {arr}**",
                            "",
                            f"- 距离：`{local.get('distance')} nm`",
                            f"- 预计：`{local.get('flight_time')}`",
                            "",
                            "**🗺️ 航路**",
                            "```text",
                            str(local.get("route") or "DCT"),
                            "```",
                            "",
                            "> 官方航路服务暂不可用，以上为本地估算。",
                        ]
                    )
                )
            except Exception as local_exc:  # noqa: BLE001
                log.warning("local route fallback failed: %s", local_exc)
                return md_error("航路计算服务暂时不可用", "请稍后再试。")

    return md_error("未识别指令", "发送 `/帮助` 查看全部可用指令。")


class CWFLYClient(botpy.Client):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._notify_group_openid = load_notify_openid()
        self._notify_task = None
        self._notify_known = None

    async def _notify_controller_loop(self) -> None:
        """轮询 FSD 在线管制列表，做差量播报；首次快照只建基线，不刷屏。"""
        try:
            interval = max(5, int(_env_value("QQ_NOTIFY_POLL_SECONDS", "8") or "8"))
        except ValueError:
            interval = 8
        await asyncio.sleep(2)
        while True:
            try:
                data = await asyncio.to_thread(api_get, "/flightplans/fsd/controllers")
                rows = data.get("data") or []
                current = {}
                for row in rows:
                    callsign = str(row.get("callsign") or "").strip().upper()
                    if callsign:
                        item = dict(row)
                        item["_detectedAt"] = time.time()
                        current[callsign] = item
                if self._notify_known is None:
                    self._notify_known = current
                    print(f"[notify] 管制上线播报基线已建立: {len(current)} 个席位", flush=True)
                else:
                    for key, controller in current.items():
                        if key not in self._notify_known:
                            await self._send_controller_notice(controller, True)
                    for key, controller in self._notify_known.items():
                        if key not in current:
                            await self._send_controller_notice(controller, False)
                    self._notify_known = current
            except Exception as exc:  # noqa: BLE001
                log.warning("[notify] 管制在线轮询失败: %s", exc)
            await asyncio.sleep(interval)

    async def _send_controller_notice(self, controller: dict, online: bool) -> None:
        if not controller or _notify_skipped(controller):
            return
        group_openid = self._notify_group_openid or load_notify_openid()
        if not group_openid:
            log.warning("[notify] 未绑定播报群，跳过 %s", controller.get("callsign"))
            return
        self._notify_group_openid = group_openid
        text = build_controller_notice(controller, online)
        try:
            await self.api.post_group_message(
                group_openid=group_openid, msg_type=0, content=text
            )
            log.info(
                "[notify] 已播报 %s %s",
                controller.get("callsign"),
                "上线" if online else "下线",
            )
        except Exception:  # noqa: BLE001
            log.exception("[notify] 群播报发送失败 group=%s", group_openid)

    async def _bind_notify_group(self, message: GroupMessage, content: str) -> bool:
        """在目标群发送 `/绑定播报 <口令>`，持久化该群 openid。"""
        if not content.lstrip().startswith(("/绑定播报", "/bindnotify")):
            return False
        expected = _env_value("QQ_NOTIFY_BIND_TOKEN")
        parts = content.split(maxsplit=1)
        if not expected:
            await send_markdown_reply(
                message,
                md_error("播报群绑定未启用", "管理员需先在 .env 配置 QQ_NOTIFY_BIND_TOKEN。"),
                1,
            )
            return True
        if len(parts) < 2 or parts[1].strip() != expected:
            await send_markdown_reply(message, md_error("绑定口令错误"), 1)
            return True
        group_openid = str(getattr(message, "group_openid", "") or "").strip()
        if not group_openid:
            await send_markdown_reply(message, md_error("无法获取当前群 openid"), 1)
            return True
        self._notify_group_openid = group_openid
        save_notify_openid(group_openid)
        await send_markdown_reply(
            message,
            "✅ **播报群已绑定**\n\n> 后续管制上线/下线将自动播报到本群。",
            1,
        )
        log.info("[notify] 播报群已绑定 group=%s", group_openid)
        return True

    async def _bot_login(self, token) -> None:
        """兼容 botpy 1.2.1 尚未内置 GROUP_MESSAGE_CREATE 解析器的补丁。

        QQ 开放平台开启「接收所有消息」后，会下发 GROUP_MESSAGE_CREATE；
        未开启时仍然只有 @机器人 的 GROUP_AT_MESSAGE_CREATE。
        """
        await super()._bot_login(token)
        state = self._connection.state

        def parse_group_message_create(payload):
            message = GroupMessage(self.api, payload.get("id"), payload.get("d", {}))
            self.ws_dispatch("group_message_create", message)

        state.parsers["group_message_create"] = parse_group_message_create
        self._handled_group_message_ids = set()
        # botpy 版本差异下 on_ready 不一定会被分发；登录完成即启动轮询任务
        if self._notify_task is None or self._notify_task.done():
            self._notify_task = asyncio.create_task(self._notify_controller_loop())
        print(
            f"[notify] 管制播报轮询已启动 group={self._notify_group_openid or '未绑定'}",
            flush=True,
        )

    async def _handle_group_message(self, message: GroupMessage, require_command: bool = False) -> None:
        content = clean_content(message.content)
        if not content:
            return
        # 全量消息模式下只响应 / 指令，避免机器人在群里对普通聊天插话。
        if require_command and not content.lstrip().startswith("/"):
            return
        # 同一条消息可能同时触发 @事件 和全量事件，按 msg_id 去重。
        message_id = getattr(message, "id", None)
        handled = getattr(self, "_handled_group_message_ids", None)
        if handled is None:
            handled = set()
            self._handled_group_message_ids = handled
        if message_id and message_id in handled:
            return
        if message_id:
            handled.add(message_id)
            if len(handled) > 500:
                handled.clear()

        log.info("group=%s content=%s", message.group_openid, content)
        if await self._bind_notify_group(message, content):
            return
        is_route = content.lstrip().lower().startswith(("/航路", "/route"))
        if is_route:
            try:
                await send_markdown_reply(
                    message,
                    "⏳ **航路计算中**\n\n> 正在匹配航路、SID 与 STAR，请稍候…",
                    msg_seq=1,
                )
            except Exception:  # noqa: BLE001
                log.exception("route ack failed")
        try:
            reply = handle_command(content)
            await send_markdown_reply(message, reply, msg_seq=2 if is_route else 1)
            log.info("replied to group=%s", message.group_openid)
        except Exception:  # noqa: BLE001
            log.exception("reply failed")
            try:
                await send_markdown_reply(
                    message,
                    md_error("指令处理失败", "请稍后再试，或发送 `/帮助` 查看用法。"),
                    msg_seq=3 if is_route else 2,
                )
            except Exception:  # noqa: BLE001
                log.exception("fallback reply failed")

    async def on_group_at_message_create(self, message: GroupMessage):
        await self._handle_group_message(message)

    async def on_group_message_create(self, message: GroupMessage):
        """QQ 开放平台开启「接收所有消息」后，未 @ 的 /帮助 也能响应。"""
        await self._handle_group_message(message, require_command=True)

    async def on_ready(self):
        log.info("QQ 官方机器人已连接：%s", self.robot.name)
        if self._notify_task is None or self._notify_task.done():
            self._notify_task = asyncio.create_task(self._notify_controller_loop())
        if self._notify_group_openid:
            log.info("[notify] 管制播报已启用 group=%s", self._notify_group_openid)
        else:
            log.warning("[notify] 尚未绑定播报群，请在目标群发送 /绑定播报 <口令>")

    async def on_group_add_robot(self, event):
        log.info("bot added to group: %s", event)


def main() -> None:
    env = load_env()
    appid = env.get("QQ_BOT_APPID", "")
    secret = env.get("QQ_BOT_SECRET", "")
    if not appid or not secret:
        raise SystemExit("缺少 QQ_BOT_APPID / QQ_BOT_SECRET")
    intents = botpy.Intents(public_messages=True)
    client = CWFLYClient(intents=intents)
    client.run(appid=appid, secret=secret)


if __name__ == "__main__":
    main()
