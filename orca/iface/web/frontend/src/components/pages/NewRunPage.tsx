// components/pages/NewRunPage.tsx —— `/runs/new` 表单（SPEC §6.2）。
//
// 提交 → POST /api/run → 拿 run_id → navigate(`/runs/<new_id>`)（push，后退回表单）。
// 纯渲染 + forward（铁律 6）：不含编排，只把表单 forward 给后端。

import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";

interface StartRunResponse {
  run_id: string;
  status: string;
}

export function NewRunPage() {
  const navigate = useNavigate();
  const [yamlPath, setYamlPath] = useState("");
  const [task, setTask] = useState("");
  const [maxIter, setMaxIter] = useState("");
  const [inputsJson, setInputsJson] = useState("{}");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setSubmitting(true);
    setError(null);

    let inputs: Record<string, unknown> = {};
    try {
      inputs = inputsJson.trim() ? JSON.parse(inputsJson) : {};
    } catch {
      setError("inputs 不是合法 JSON");
      setSubmitting(false);
      return;
    }

    try {
      const body: Record<string, unknown> = {
        yaml_path: yamlPath,
        inputs,
      };
      if (task.trim()) body.task = task.trim();
      if (maxIter.trim()) {
        const n = Number(maxIter);
        if (!Number.isFinite(n) || n < 1) {
          setError("max_iter 必须是 ≥1 的整数");
          setSubmitting(false);
          return;
        }
        body.max_iter = n;
      }

      const resp = await fetch("/api/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!resp.ok) {
        const detail = await resp.text();
        throw new Error(`HTTP ${resp.status}: ${detail}`);
      }
      const data = (await resp.json()) as StartRunResponse;
      // 后端返回 {run_id, status:"queued"} → 跳转详情页（push，后退回表单）
      navigate(`/runs/${data.run_id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="flex h-full flex-col">
      <div className="border-b border-slate-200 p-4">
        <h1 className="text-lg font-semibold">New Run</h1>
      </div>
      <form onSubmit={onSubmit} className="flex-1 space-y-4 p-4">
        <label className="block">
          <span className="text-sm font-medium text-slate-700">
            YAML path <span className="text-orca-failed">*</span>
          </span>
          <input
            type="text"
            required
            value={yamlPath}
            onChange={(e) => setYamlPath(e.target.value)}
            placeholder="workflows/demo.yaml"
            className="mt-1 block w-full rounded border border-slate-300 px-2 py-1 text-sm"
          />
        </label>
        <label className="block">
          <span className="text-sm font-medium text-slate-700">Task</span>
          <input
            type="text"
            value={task}
            onChange={(e) => setTask(e.target.value)}
            placeholder="(可选) 覆盖 workflow 的 task"
            className="mt-1 block w-full rounded border border-slate-300 px-2 py-1 text-sm"
          />
        </label>
        <label className="block">
          <span className="text-sm font-medium text-slate-700">Max iter</span>
          <input
            type="number"
            min={1}
            value={maxIter}
            onChange={(e) => setMaxIter(e.target.value)}
            placeholder="(可选) 最大迭代数"
            className="mt-1 block w-full rounded border border-slate-300 px-2 py-1 text-sm"
          />
        </label>
        <label className="block">
          <span className="text-sm font-medium text-slate-700">
            Inputs (JSON)
          </span>
          <textarea
            value={inputsJson}
            onChange={(e) => setInputsJson(e.target.value)}
            rows={4}
            className="mt-1 block w-full rounded border border-slate-300 px-2 py-1 font-mono text-xs"
          />
        </label>
        {error && <p className="text-sm text-orca-failed">{error}</p>}
        <button
          type="submit"
          disabled={submitting}
          className="rounded bg-slate-900 px-4 py-2 text-sm text-white hover:bg-slate-700 disabled:opacity-50"
        >
          {submitting ? "提交中…" : "启动 Run"}
        </button>
      </form>
    </div>
  );
}
