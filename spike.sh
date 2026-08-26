#!/bin/bash
# spike: 对比 in-session 两条推进路径。orca log 走 stderr，2>/dev/null 丢弃，stdout 纯 JSON。
# v3 §8 step 1：命令上移顶层（orca <wf> / orca next），删 in-session namespace。
source /home/mozzie/miniconda3/etc/profile.d/conda.sh
conda activate orca
cd /mnt/d/Projects/Orca

echo "########## SPIKE B: 主 session 调 next（产出 = LLM 转述塞进 --output）##########"
BOOT=$(orca bootstrap examples/demo_insession.yaml --inputs '{"topic":"秋天"}' 2>/dev/null)
B_RUN=$(echo "$BOOT" | python -c "import sys,json;print(json.load(sys.stdin)['run_id'])")
echo "run_id=$B_RUN"
echo "--- next 1 (writer→reviewer) ---"
orca next --run-id "$B_RUN" --output '秋意浓，落叶舞秋风' 2>/dev/null | python -c "import sys,json;d=json.load(sys.stdin);print('done=',d['done']);p=d.get('prompt','');print('prompt 指向文件:', [l for l in p.split(chr(10)) if '.md' in l][:1]);print('prompt 含 writer 产出?', '秋意浓' in open(p.split('runs/')[-1].join(['runs/',''])).read() if 'runs/' in p else 'N/A')"
echo "--- next 2 (reviewer→summarizer) ---"
orca next --run-id "$B_RUN" --output '评分=8；建议更有画面感' 2>/dev/null | python -c "import sys,json;d=json.load(sys.stdin);print('done=',d['done'])"
echo "--- next 3 (summarizer→应 done=true) ---"
orca next --run-id "$B_RUN" --output '标语有画面感' 2>/dev/null | python -c "import sys,json;d=json.load(sys.stdin);print('done=',d['done'])"
echo "--- tape 事件序列 ---"
cat "runs/${B_RUN}.jsonl" 2>/dev/null | python -c "import sys,json;[print(' ',json.loads(l)['type']) for l in sys.stdin]"

echo
echo "########## SPIKE A: hook 调 next（产出 = 从 tool_response 结构化提取，等价 cc_hooks jq）##########"
BOOT=$(orca bootstrap examples/demo_insession.yaml --inputs '{"topic":"秋天"}' 2>/dev/null)
A_RUN=$(echo "$BOOT" | python -c "import sys,json;print(json.load(sys.stdin)['run_id'])")
echo "run_id=$A_RUN"
echo "--- PostToolUse 模拟：从 tool_response.content 结构化提取（非 LLM 转述）---"
echo '{"tool_response":{"content":[{"type":"text","text":"秋意浓，落叶舞秋风"}]}}' | python -c "import sys,json;d=json.load(sys.stdin);print(chr(10).join(c['text'] for c in d['tool_response']['content'] if c.get('type')=='text'))" > /tmp/orca_cache.txt
echo "cache=[$(cat /tmp/orca_cache.txt)]"
echo "--- Stop 模拟：shell 调 next（确定性触发，非 LLM）---"
orca next --run-id "$A_RUN" --output "$(cat /tmp/orca_cache.txt)" 2>/dev/null | python -c "import sys,json;d=json.load(sys.stdin);print('done=',d['done'])"

echo
echo "########## 对比结论 ##########"
echo "SPIKE B next 调用:  orca next --run-id <id> --output '<LLM 转述的产出>'"
echo "SPIKE A next 调用:  orca next --run-id <id> --output \"\$(cat cache)\""
echo "→ 两条 next CLI 完全一致；差异只在 --output 值的来源（LLM 转述 vs 结构化提取）+ 触发者（LLM vs shell）"
echo "语法糖：orca <wf> ≡ orca bootstrap <wf>"
