// components/gate/post-approval-respond.ts —— approval 回答 POST 共用 helper（DRY）。
//
// 对齐后端 POST /approval/respond body 形状（orca/iface/web/routes/approval.py）：
//   {approval_id, answer, source}
// 前端默认 source="web"。前端不决策（铁律 2，与 post-gate-respond 同源），纯 forward。
//
// 失败 fail loud（throw），由调用方决定如何提示用户。late respond（ok=false）属预期 race
// （已被 yolo/timeout/别处 user 抢答），不抛错。

interface ApprovalRespondBody {
  approval_id: string;
  answer: "allow" | "deny";
  source: "web";
}

/** POST /approval/respond。失败抛 Error。late respond（ok=false）不抛，原样返回。 */
export async function postApprovalRespond(
  body: ApprovalRespondBody,
): Promise<{ ok: boolean; approval_id: string; resolved_by?: string }> {
  const resp = await fetch("/approval/respond", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    throw new Error(`POST /approval/respond HTTP ${resp.status}`);
  }
  const json = (await resp.json()) as {
    ok?: boolean;
    approval_id?: string;
    resolved_by?: string;
  };
  return {
    ok: Boolean(json.ok ?? false),
    approval_id: json.approval_id ?? body.approval_id,
    resolved_by: json.resolved_by,
  };
}
