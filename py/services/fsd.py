"""FSD 在线状态查询服务（纯 HTTP）。

数据来自后端 /api/online/snapshot；机器人不再与 FSD 建立协议连接。
"""
import asyncio
import time

from .. import config
from . import http
from .fsd_http import get_observer, get_observer_status

API_BASE = (getattr(config, 'CWFLY_API_URL', '') or 'http://127.0.0.1:3001/api').rstrip('/')
API_KEY = (getattr(config, 'CWFLY_API_KEY', '') or '').strip()


def is_system_callsign(callsign):
    """系统连接（CW-BOT/CW-MAP-*/CW-FP*）不计入在线统计。"""
    return (callsign or '').upper().startswith('CW-')


def get_snapshot_status():
    status = get_observer_status()
    if not status:
        return None
    return {**status, 'source': 'snapshot'}


async def get_api_status():
    """HTTP 快照后备。"""
    headers = {'Accept': 'application/json', 'User-Agent': 'Mozilla/5.0 (compatible; CWFLY-Bot/1.0; +https://www.cwflysim.cn)'}
    if API_KEY:
        headers['X-API-Key'] = API_KEY
    resp = await http.http_get(f'{API_BASE}/online/snapshot', timeout=8, headers=headers)
    if resp.status_code != 200:
        raise RuntimeError(f'snapshot HTTP {resp.status_code}')
    data = (resp.json() or {}).get('data') or {}
    pilots = data.get('pilots') or [
        c for c in data.get('clients') or [] if c.get('type') == 'PILOT']
    controllers = data.get('controllers') or [
        c for c in data.get('clients') or [] if c.get('type') == 'ATC']
    pilots = [p for p in pilots if not is_system_callsign(p.get('callsign'))]
    controllers = [c for c in controllers if not is_system_callsign(c.get('callsign'))]
    return {'pilots': pilots, 'controllers': controllers, 'total': len(pilots) + len(controllers)}


async def get_fsd_status():
    """统一查询入口：观察器缓存优先，失败回退 HTTP。"""
    status = get_snapshot_status()
    if status:
        return {**status, 'total': len(status['clients'])}
    data = await get_api_status()
    return {**data, 'source': 'http',
            'total': len(data.get('pilots') or []) + len(data.get('controllers') or [])}


async def query_pilot(callsign):
    key = str(callsign or '').upper()
    observer = get_observer()
    if observer.is_ready:
        return observer.get_pilot(key)
    try:
        data = await get_fsd_status()
    except Exception:  # noqa: BLE001
        return None
    return next((p for p in data.get('pilots') or []
                 if str(p.get('callsign') or '').upper() == key), None)


async def query_controller(callsign):
    key = str(callsign or '').upper()
    observer = get_observer()
    if observer.is_ready:
        return observer.get_controller(key)
    try:
        data = await get_fsd_status()
    except Exception:  # noqa: BLE001
        return None
    return next((c for c in data.get('controllers') or []
                 if str(c.get('callsign') or '').upper() == key), None)


# ========== 格式化输出 ==========
FACILITY_NAMES = ['———', 'OBS', 'FSS', 'DEL', 'GND', 'TWR', 'APP', 'CTR']
RATING_NAMES = ['SUS', 'OBS', 'S1', 'S2', 'S3', 'C1', 'C2', 'C3',
                'I1', 'I2', 'I3', 'SUP', 'ADM']


def _freq_label(freq):
    try:
        return f'{int(freq) / 1000:.3f}'
    except (TypeError, ValueError):
        return ''


async def format_online_status():
    try:
        data = await get_fsd_status()
        now = time.strftime('%Y-%m-%d %H:%M:%S')
        source = '后端快照' if data['source'] == 'snapshot' else 'HTTP API'
        return '\n'.join([
            f'在线机组：{len(data["pilots"])}',
            f'在线管制：{len(data["controllers"])}',
            f'总连接数：{data["total"]}',
            f'查询时间：{now}',
        ])
    except Exception:
        return 'FSD 服务暂时不可用，请稍后重试。'


async def format_pilot_query(callsign):
    pilot = await query_pilot(callsign)
    if not pilot:
        return f'未找到在线机组「{callsign}」'
    plan = pilot.get('plan') or {}
    lines = [f'在线机组 {pilot.get("callsign")}']
    if pilot.get('cid'):
        lines.append(f'CID：{pilot["cid"]}')
    if pilot.get('realname'):
        lines.append(f'姓名：{pilot["realname"]}')

    # ---- 飞行计划 ----
    if plan.get('type'):
        rules = str(plan['type']).upper()
        lines.append(f'计划类型：{"IFR 仪表" if rules == "I" else "VFR 目视" if rules == "V" else rules}')
    ac = pilot.get('aircraft') or plan.get('aircraft') or ''
    if ac:
        lines.append(f'机型：{ac}')
    dep = plan.get('depairport') or pilot.get('dep') or ''
    dest = plan.get('destairport') or pilot.get('dest') or ''
    if dep and dest:
        lines.append(f'航线：{dep} → {dest}')
    if plan.get('altitude'):
        lines.append(f'计划高度：{plan["altitude"]}')
    if plan.get('tascruise'):
        lines.append(f'巡航速度：{plan["tascruise"]}')
    if plan.get('hrsenroute') or plan.get('minenroute'):
        hrs = plan.get('hrsenroute') or 0
        mins = plan.get('minenroute') or 0
        lines.append(f'预计时间：{hrs}h{mins}m')
    if plan.get('altairport'):
        lines.append(f'备降场：{plan["altairport"]}')
    route = plan.get('route') or pilot.get('route') or ''
    if route:
        lines.append(f'航路：{route[:120] + "…" if len(route) > 120 else route}')
    if plan.get('remarks'):
        lines.append(f'备注：{plan["remarks"]}')

    # ---- 实时状态 ----
    if pilot.get('altitude'):
        alt = pilot['altitude']
        if isinstance(alt, str):
            label = alt
        else:
            try:
                label = f'FL{round(int(alt) / 100)}'
            except (TypeError, ValueError):
                label = str(alt)
        lines.append(f'当前高度：{label}')
    if pilot.get('groundspeed'):
        lines.append(f'地速：{pilot["groundspeed"]} kt')
    if pilot.get('transponder'):
        lines.append(f'应答机：{pilot["transponder"]}')

    return '\n'.join(lines)


async def format_controller_query(callsign):
    ctrl = await query_controller(callsign)
    if not ctrl:
        return f'未找到在线管制「{callsign}」'
    lines = [f'在线管制 {ctrl.get("callsign")}']
    if ctrl.get('realname'):
        lines.append(f'姓名：{ctrl["realname"]}')
    try:
        ft = int(ctrl.get('facilitytype', -1))
    except (TypeError, ValueError):
        ft = -1
    lines.append(f'席位：{FACILITY_NAMES[ft] if 0 <= ft < len(FACILITY_NAMES) else ft}')
    if ctrl.get('frequency'):
        lines.append(f'频率：{_freq_label(ctrl["frequency"])}')
    try:
        r = int(ctrl.get('rating', -1))
    except (TypeError, ValueError):
        r = -1
    if r >= 0:
        lines.append(f'等级：{RATING_NAMES[r] if 0 <= r < len(RATING_NAMES) else r}')
    if ctrl.get('starttime'):
        t = time.strftime('%H:%M:%S', time.localtime(int(ctrl['starttime'])))
        lines.append(f'上线时间：{t}')
    return '\n'.join(lines)


async def format_pilot_list():
    try:
        data = await get_fsd_status()
        pilots = data['pilots']
        if not pilots:
            return '当前无在线机组。'
        items = []
        for p in pilots[:20]:
            plan = p.get('plan') or {}
            dep = plan.get('depairport') or ''
            dest = plan.get('destairport') or ''
            items.append(f'{p.get("callsign")} {dep}→{dest}' if dep and dest else p.get('callsign'))
        more = f'\n...及另外 {len(pilots) - 20} 人' if len(pilots) > 20 else ''
        return f'在线机组（{len(pilots)}）\n' + '\n'.join(items) + more
    except Exception:
        return 'FSD 服务暂时不可用。'


async def format_controller_list():
    try:
        data = await get_fsd_status()
        controllers = data['controllers']
        if not controllers:
            return '当前无在线管制。'
        items = []
        for c in controllers:
            freq = f' {_freq_label(c.get("frequency"))}' if c.get('frequency') else ''
            items.append(f'{c.get("callsign")}{freq}')
        return f'在线管制（{len(controllers)}）\n' + '\n'.join(items)
    except Exception:
        return 'FSD 服务暂时不可用。'
