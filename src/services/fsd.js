/**
 * FSD 在线状态查询服务（纯 HTTP）
 *
 * 数据来自后端 /api/online/snapshot（后端读 FSD 落库快照），
 * 机器人不再与 FSD 建立协议连接。
 */

import { getObserver, getObserverStatus } from './fsdHttp.js'

const API_BASE = (process.env.CWFLY_API_URL || 'http://127.0.0.1:3001/api').replace(/\/+$/, '')
const API_KEY = (process.env.CWFLY_API_KEY || '').trim()

// ========== 在线快照（后端 /api/online/snapshot） ==========

// 系统连接（CW-BOT/CW-MAP-*/CW-FP*）不计入在线统计
function isSystemCallsign(callsign) {
  return (callsign || '').toUpperCase().startsWith('CW-')
}

function getSnapshotStatus() {
  const status = getObserverStatus()
  if (!status) return null
  return { ...status, source: 'snapshot' }
}

// ========== HTTP API 后备 ==========

async function getApiStatus() {
  const headers = { Accept: 'application/json', 'User-Agent': 'Mozilla/5.0 (compatible; CWFLY-Bot/1.0; +https://www.cwflysim.cn)' }
  if (API_KEY) headers['X-API-Key'] = API_KEY
  const response = await fetch(`${API_BASE}/online/snapshot`, {
    headers,
    signal: AbortSignal.timeout(8000)
  })
  if (!response.ok) throw new Error(`snapshot HTTP ${response.status}`)
  const payload = await response.json()
  const data = payload?.data || {}
  const pilots = (data.pilots || data.clients?.filter?.(c => c.type === 'PILOT') || [])
    .filter(p => !isSystemCallsign(p.callsign))
  const controllers = (data.controllers || data.clients?.filter?.(c => c.type === 'ATC') || [])
    .filter(c => !isSystemCallsign(c.callsign))
  return { pilots, controllers, total: pilots.length + controllers.length }
}

// ========== 统一查询接口（观察器缓存 → HTTP 后备） ==========

export async function getFsdStatus() {
  const status = getSnapshotStatus()
  if (status) return { ...status, total: status.clients.length }
  const data = await getApiStatus()
  return { ...data, source: 'http', total: (data.pilots?.length || 0) + (data.controllers?.length || 0) }
}

export async function queryPilot(callsign) {
  const key = String(callsign || '').toUpperCase()
  const client = getObserver()
  if (client.isReady) return client.getPilot(key)
  try {
    const data = await getFsdStatus()
    return data.pilots.find(p => p.callsign?.toUpperCase() === key) || null
  } catch {
    return null
  }
}

export async function queryController(callsign) {
  const key = String(callsign || '').toUpperCase()
  const client = getObserver()
  if (client.isReady) return client.getController(key)
  try {
    const data = await getFsdStatus()
    return data.controllers.find(c => c.callsign?.toUpperCase() === key) || null
  } catch {
    return null
  }
}

// ========== 格式化输出 ==========

const FACILITY_NAMES = ['———', 'OBS', 'FSS', 'DEL', 'GND', 'TWR', 'APP', 'CTR']
const RATING_NAMES = ['SUS', 'OBS', 'S1', 'S2', 'S3', 'C1', 'C2', 'C3', 'I1', 'I2', 'I3', 'SUP', 'ADM']

export async function formatOnlineStatus() {
  try {
    const data = await getFsdStatus()
    const now = new Date().toLocaleString('zh-CN', { hour12: false })
    const source = data.source === 'snapshot' ? '后端快照' : 'HTTP API'
    return [
      `在线机组：${data.pilots.length}`,
      `在线管制：${data.controllers.length}`,
      `总连接数：${data.total}`,
      `查询时间：${now}`
    ].join('\n')
  } catch {
    return 'FSD 服务暂时不可用，请稍后重试。'
  }
}

export async function formatPilotQuery(callsign) {
  const pilot = await queryPilot(callsign)
  if (!pilot) return `未找到在线机组「${callsign}」`
  const plan = pilot.plan || pilot.flightplan || {}
  const lines = [`在线机组 ${pilot.callsign}`]
  if (pilot.cid) lines.push(`CID：${pilot.cid}`)
  if (pilot.realname) lines.push(`姓名：${pilot.realname}`)

  // ---- 飞行计划 ----
  if (plan.type) {
    const rules = String(plan.type).toUpperCase()
    lines.push(`计划类型：${rules === 'I' ? 'IFR 仪表' : rules === 'V' ? 'VFR 目视' : rules}`)
  }
  const ac = pilot.aircraft || plan.aircraft || ''
  if (ac) lines.push(`机型：${ac}`)
  const dep = plan.depairport || pilot.dep || ''
  const dest = plan.destairport || pilot.dest || ''
  if (dep && dest) lines.push(`航线：${dep} → ${dest}`)
  if (plan.altitude) lines.push(`计划高度：${plan.altitude}`)
  if (plan.tascruise) lines.push(`巡航速度：${plan.tascruise}`)
  if (plan.hrsenroute || plan.minenroute) {
    const hrs = plan.hrsenroute || 0
    const mins = plan.minenroute || 0
    lines.push(`预计时间：${hrs}h${mins}m`)
  }
  if (plan.altairport) lines.push(`备降场：${plan.altairport}`)
  const route = plan.route || pilot.route || ''
  if (route) {
    const r = route.length > 120 ? route.slice(0, 120) + '…' : route
    lines.push(`航路：${r}`)
  }
  if (plan.remarks) lines.push(`备注：${plan.remarks}`)

  // ---- 实时状态 ----
  if (pilot.altitude) {
    const alt = typeof pilot.altitude === 'string' ? pilot.altitude : `FL${Math.round(Number(pilot.altitude) / 100)}`
    lines.push(`当前高度：${alt}`)
  }
  if (pilot.groundspeed) lines.push(`地速：${pilot.groundspeed} kt`)
  if (pilot.transponder) lines.push(`应答机：${pilot.transponder}`)

  return lines.join('\n')
}

export async function formatControllerQuery(callsign) {
  const ctrl = await queryController(callsign)
  if (!ctrl) return `未找到在线管制「${callsign}」`
  const lines = [`在线管制 ${ctrl.callsign}`]
  if (ctrl.realname) lines.push(`姓名：${ctrl.realname}`)
  const ft = ctrl.facilitytype !== undefined ? Number(ctrl.facilitytype) : -1
  lines.push(`席位：${FACILITY_NAMES[ft] || ft}`)
  if (ctrl.frequency) {
    const freq = (Number(ctrl.frequency) / 1000).toFixed(3)
    lines.push(`频率：${freq}`)
  }
  const r = ctrl.rating !== undefined ? Number(ctrl.rating) : -1
  if (r >= 0) lines.push(`等级：${RATING_NAMES[r] || r}`)
  if (ctrl.starttime) {
    const t = new Date(Number(ctrl.starttime) * 1000)
    lines.push(`上线时间：${t.toLocaleTimeString('zh-CN', { hour12: false })}`)
  }
  return lines.join('\n')
}

export async function formatPilotList() {
  try {
    const data = await getFsdStatus()
    const pilots = data.pilots
    if (!pilots || pilots.length === 0) return '当前无在线机组。'
    const items = pilots.slice(0, 20).map(p => {
      const dep = p.plan?.depairport || p.flightplan?.depairport || ''
      const dest = p.plan?.destairport || p.flightplan?.destairport || ''
      return dep && dest ? `${p.callsign} ${dep}→${dest}` : p.callsign
    })
    const more = pilots.length > 20 ? `\n...及另外 ${pilots.length - 20} 人` : ''
    return `在线机组（${pilots.length}）\n${items.join('\n')}${more}`
  } catch {
    return 'FSD 服务暂时不可用。'
  }
}

export async function formatControllerList() {
  try {
    const data = await getFsdStatus()
    const controllers = data.controllers
    if (!controllers || controllers.length === 0) return '当前无在线管制。'
    const items = controllers.map(c => {
      const freq = c.frequency ? ` ${(Number(c.frequency) / 1000).toFixed(3)}` : ''
      return `${c.callsign}${freq}`
    })
    return `在线管制（${controllers.length}）\n${items.join('\n')}`
  } catch {
    return 'FSD 服务暂时不可用。'
  }
}
