// components/gate/post-gate-respond.ts —— gate 回答 POST 共用 helper（DRY）。
//
// 对齐后端 POST /gate/respond body 形状（orca/gates/http_endpoint.py gate_respond_endpoint）：
//   {gate_id, answer, source}
// 前端默认 source="web"（前端壳恒定）。前端不决策（铁律 2），纯 forward。
//
// 失败 fail loud（throw），由调用方决定如何提示用户（PermissionGate/AskGate 各自处理）。

interface GateRespondBody {
  gate_id: string;
  answer: string;
  source: "web";
}

/** POST /gate/respond。失败抛 Error（调用方 fail loud）。返回后端 {ok, gate_id}。 */
export async function postGateRespond(body: GateRespondBody): Promise<{ ok: boolean; gate_id: string }> {
  const resp = await fetch("/gate/respond", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    throw new Error(`POST /gate/respond HTTP ${resp.status}`);
  }
  const json = (await resp.json()) as { ok?: boolean; gate_id?: string };
  if (json.ok === false) {
    // 晚到（已被别的壳答了）—— 这是预期 race，不抛错。submitting 保持 true（调用方不重置），
    // resolved 事件随后到达 → store.gate=null → 弹窗自然关闭（SPEC §1.6 不乐观更新）。
    return { ok: false, gate_id: body.gate_id };
  }
  return { ok: Boolean(json.ok ?? false), gate_id: body.gate_id };
}
