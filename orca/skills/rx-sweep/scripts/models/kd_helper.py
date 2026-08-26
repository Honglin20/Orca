"""rx-sweep KD 助手 —— FitNets 式知识蒸馏的运行时载体。

逐字实现 `orca/skills/rx-sweep/reference/contracts.md` §2 的接口契约：
    class KDHelper:
        def __init__(self, teacher_build_fn, teacher_ckpt, student_hook_names,
                     device, alpha_out=1.0, beta_feat=0.5): ...
        def __call__(self, student, x, task_loss_fn, y) -> Tensor: ...

设计约束（why）：
- 纯 torch，自包含 —— 本文件最终会被拷进用户工程，
  用户工程里没有 Orca 源码，任何 Orca import 都会让用户工程崩。
- teacher 永远 `eval()` + 全冻结 + forward 在 `torch.no_grad()` 下，三重保险，
  绝不污染 teacher 权重（teacher 是全 sweep 共享的参考物，一旦被改 KD 实验全废）。
- adapter **懒建**：构造期不知道 student/teacher 中间特征的张量形状，强行假设会让
  本文件和具体模型死耦合；首次 `__call__` 拿到真实 feature 才建。
- fail loud：ckpt 缺失 / load 失败 / hook 数 student≠teacher / 模块名找不到 →
  抛清晰异常，绝不让 silent broadcast 偷偷喂错 loss。
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, List, Sequence

import torch
import torch.nn as nn
import torch.nn.functional as F

__all__ = ["KDTeacherWrapper", "FeatureAlign", "KDHelper"]


# ---------------------------------------------------------------------------
# teacher 封装
# ---------------------------------------------------------------------------
class KDTeacherWrapper(nn.Module):
    """把 teacher 包成「只读推理机」：载 ckpt → eval → 全冻结 → 注册 feature hook。

    Why 单独包一层：
    - 把「冻结 + no_grad + hook 收集」这三件事集中在 teacher 侧，避免 KDHelper 的
      主流程里散落 teacher 状态管理代码，读起来一眼看穿「teacher 永不学习」。
    - forward 内部强制 `torch.no_grad()`，即便外部误把 teacher 放进优化器，权重
      也不会被改（参数 `requires_grad=False` 是第二重保险）。
    """

    def __init__(
        self,
        teacher_build_fn: Callable[[], nn.Module],
        teacher_ckpt: "str | Path",
        hook_names: Sequence[str],
        device,
    ) -> None:
        super().__init__()
        self.hook_names: List[str] = list(hook_names)
        self.device = torch.device(device)

        # ---- 1. 构造 teacher ----
        # teacher_build_fn() 应仅构图、不载权重（载权重是我们自己的事，避免双载）。
        self.teacher = teacher_build_fn()

        # ---- 2. 载 ckpt（fail loud）----
        # teacher 是全 sweep 的共享参考，载错会让所有 KD 实验对齐到错的 teacher，
        # 必须在初始化期就炸出来，不能拖到训练中。
        ckpt_path = Path(teacher_ckpt)
        if not ckpt_path.exists():
            raise FileNotFoundError(f"teacher_ckpt 不存在: {ckpt_path}")
        try:
            ckpt_obj = torch.load(ckpt_path, map_location=self.device)
        except Exception as e:  # noqa: BLE001 —— 任意 load 错都包装成清晰报因
            raise RuntimeError(
                f"torch.load teacher_ckpt 失败 ({ckpt_path}): {e}"
            ) from e

        # 兼容两种常见存盘格式：裸 state_dict，或 {'state_dict'|'model': state_dict, ...}。
        # 选错也没关系——strict=True 的 load_state_dict 会再挡一道。
        state_dict = ckpt_obj
        if isinstance(ckpt_obj, dict):
            for key in ("state_dict", "model", "model_state_dict"):
                val = ckpt_obj.get(key)
                if isinstance(val, dict):
                    state_dict = val
                    break

        try:
            self.teacher.load_state_dict(state_dict, strict=True)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(
                f"teacher load_state_dict 失败 (strict=True, ckpt={ckpt_path}): {e}"
            ) from e

        # ---- 3. 冻结 + eval ----
        self.teacher.to(self.device)
        self.teacher.eval()
        for p in self.teacher.parameters():
            p.requires_grad_(False)

        # ---- 4. 在 hook_names 指定的模块上注册 forward hook ----
        # feature_hook_names() 返回的是 named_modules() 里的模块名，逐个查表注册。
        self._t_feats_buf: List["torch.Tensor | None"] = [None] * len(self.hook_names)
        self._hook_handles: List[torch.utils.hooks.RemovableHandle] = []
        named = dict(self.teacher.named_modules())
        for idx, name in enumerate(self.hook_names):
            module = named.get(name)
            if module is None:
                raise ValueError(
                    f"teacher 模块 '{name}' 未找到（feature_hook_names 返回的名字"
                    f"必须在 teacher.named_modules() 中存在）。可用: "
                    f"{[k for k in named.keys() if k]}"
                )
            handle = module.register_forward_hook(self._make_hook(idx))
            self._hook_handles.append(handle)

    def _make_hook(self, idx: int):
        """构造一个把模块输出塞进 buffer[idx] 的 forward hook 闭包。"""

        def _hook(_module, _inputs, output):
            self._t_feats_buf[idx] = output

        return _hook

    def forward(self, x: torch.Tensor):
        """teacher 前向，强制 no_grad，返回 (t_out, t_feats)。"""
        # 每次前向重置 buffer，防止上次残留（若 teacher 有条件分支没触发某 hook）。
        self._t_feats_buf = [None] * len(self.hook_names)
        with torch.no_grad():
            t_out = self.teacher(x)
        t_feats = list(self._t_feats_buf)
        # fail loud：所有 hook 都该被触发，否则 feature 对齐会错位。
        missing = [
            self.hook_names[i] for i, f in enumerate(t_feats) if f is None
        ]
        if missing:
            raise RuntimeError(
                f"teacher forward 未触发以下 hook: {missing}（feature_hook_names"
                f"指向的模块未参与本次前向？）"
            )
        return t_out, t_feats


# ---------------------------------------------------------------------------
# feature 对齐 adapter（懒建）
# ---------------------------------------------------------------------------
class FeatureAlign(nn.Module):
    """逐对把 student 中间特征投影到 teacher 通道空间的懒建 adapter。

    Why 懒建：构造 KDHelper 时还不知道 student/teacher 中间特征的确切形状
    （取决于 forward 时的张量布局），强行假设会让本文件和具体模型耦合死。
    首次 `prepare(s_feats, t_feats)` 看到真实 shape 才建 adapter。

    投影策略：
    - 同形 → `nn.Identity`（零参数，直通）。
    - 仅通道维（dim=1）不同 → 按张量秩选 1×1 投影把 student 通道拉到 teacher：
        * 2D `[B, C]`    → `nn.Linear`
        * 3D `[B, C, L]` → `nn.Conv1d(kernel=1)`（保 L）
        * 4D `[B,C,H,W]` → `nn.Conv2d(kernel=1)`（保 H,W）
    - 非通道维不一致 → raise（adapter 不做空间 resize，那是用户模型设计问题）。
    """

    def __init__(self) -> None:
        super().__init__()
        self.adapters: "nn.ModuleList | None" = None
        self._built = False

    # -- public --
    def is_built(self) -> bool:
        return self._built

    def prepare(self, s_feats: Sequence[torch.Tensor],
                t_feats: Sequence[torch.Tensor]) -> None:
        """据 student/teacher feature 形状逐对建 adapter。仅应被调一次。"""
        if self._built:
            raise RuntimeError("FeatureAlign 已 prepare，不允许重复建 adapter")

        if len(s_feats) != len(t_feats):
            raise ValueError(
                f"feature 对数不一致: student={len(s_feats)} teacher={len(t_feats)}"
                "（FitNets 要求逐对对齐，请检查 feature_hook_names 长度）"
            )

        built = []
        for i, (s, t) in enumerate(zip(s_feats, t_feats)):
            s_shape = tuple(s.shape)
            t_shape = tuple(t.shape)
            if s_shape == t_shape:
                built.append(nn.Identity())
                continue

            # 形状不同：只允许通道维（dim=1）不一致，其它维必须相同。
            if len(s_shape) != len(t_shape):
                raise ValueError(
                    f"feature {i} 张量秩不一致: student {s_shape} vs teacher"
                    f" {t_shape}（adapter 无法对齐，请检查 hook 点）"
                )
            for d, (sd, td) in enumerate(zip(s_shape, t_shape)):
                if d in (0, 1):  # batch / 通道维单独处理
                    continue
                if sd != td:
                    raise ValueError(
                        f"feature {i} 非通道维 d={d} 不一致 (student={sd}, "
                        f"teacher={td})；adapter 仅做通道投影，不做空间 resize"
                    )

            s_c, t_c = s_shape[1], t_shape[1]
            if s_c == t_c:
                # 走到这里说明非通道维全相同 + 通道相同 → 实际上 s_shape==t_shape，
                # 理论不会到这；兜底用 Identity 防 silent。
                built.append(nn.Identity())
            else:
                built.append(self._build_proj(s_shape, t_c, s_c, i))

        self.adapters = nn.ModuleList(built)
        self._built = True

    @staticmethod
    def _build_proj(s_shape, t_c, s_c, idx):
        rank = len(s_shape)
        if rank == 2:  # [B, C]
            return nn.Linear(s_c, t_c)
        if rank == 3:  # [B, C, L]
            return nn.Conv1d(s_c, t_c, kernel_size=1)
        if rank == 4:  # [B, C, H, W]
            return nn.Conv2d(s_c, t_c, kernel_size=1)
        raise ValueError(
            f"feature {idx} 张量秩 {rank} 不支持（adapter 仅支持 2/3/4D 特征）"
        )

    def forward(self, s_feats: Sequence[torch.Tensor]) -> List[torch.Tensor]:
        if not self._built or self.adapters is None:
            raise RuntimeError(
                "FeatureAlign 未 prepare，请先 prepare(s_feats, t_feats)"
            )
        if len(s_feats) != len(self.adapters):
            raise RuntimeError(
                f"feature 数 {len(s_feats)} 与已建 adapter 数 {len(self.adapters)}"
                " 不一致（student 结构中途变了？）"
            )
        return [adapter(s) for adapter, s in zip(self.adapters, s_feats)]

    def kd_parameters(self):
        """adapter 参数（Identity 贡献空），供调用方加入 student 优化器。"""
        if not self._built or self.adapters is None:
            return []
        return list(self.adapters.parameters())


# ---------------------------------------------------------------------------
# 主入口：KDHelper
# ---------------------------------------------------------------------------
class KDHelper:
    """rx-sweep KD 蒸馏器，逐字对齐 contracts.md §2。

    使用范式：
        kd = KDHelper(teacher_build_fn, teacher_ckpt,
                      student_hook_names=["main.0", "main.<mid>"],
                      device="cuda", alpha_out=1.0, beta_feat=0.5)
        optimizer = SGD(list(student.parameters()) + kd.kd_parameters(), ...)
        for x, y in loader:
            optimizer.zero_grad()
            loss = kd(student, x, task_loss_fn, y)
            loss.backward()
            optimizer.step()

    Why 不是 nn.Module：KDHelper 若继承 nn.Module，其 `.parameters()` 会把冻结的
    teacher 参数也算进去，调用方一旦写 `SGD(kd_helper.parameters())` 就会试图更新
    teacher（虽有 requires_grad=False 挡着，但优化器仍会 push 状态、易踩坑）。
    保持普通类，调用方只能通过 `kd_parameters()` 拿 adapter 参数，安全。
    """

    def __init__(
        self,
        teacher_build_fn: Callable[[], nn.Module],
        teacher_ckpt: "str | Path",
        student_hook_names: Sequence[str],
        device,
        alpha_out: float = 1.0,
        beta_feat: float = 0.5,
    ) -> None:
        # ---- peek teacher 拿它的 feature_hook_names() ----
        # teacher_build_fn() 应仅构图（轻量），peek 一次拿 hook 列表后丢弃；
        # 真正载权重由 KDTeacherWrapper 内部那次 build 完成。
        peek = teacher_build_fn()
        peek_hook_fn = getattr(peek, "feature_hook_names", None)
        if not callable(peek_hook_fn):
            raise AttributeError(
                "teacher_build_fn() 返回的模型必须实现 "
                "feature_hook_names() -> list[str]"
            )
        teacher_hook_names = list(peek_hook_fn())
        del peek

        # ---- hook 数对齐校验（FitNets 要求逐对，等长）----
        self.student_hook_names: List[str] = list(student_hook_names)
        if len(teacher_hook_names) != len(self.student_hook_names):
            raise ValueError(
                f"student/teacher hook 数不匹配: student="
                f"{len(self.student_hook_names)} {self.student_hook_names} vs "
                f"teacher={len(teacher_hook_names)} {teacher_hook_names}。"
                f"FitNets 要求逐对对齐（contracts.md §1 KD hook 恒 2 个）。"
            )

        self.device = torch.device(device)
        self.alpha_out = float(alpha_out)
        self.beta_feat = float(beta_feat)

        self.teacher_wrapper = KDTeacherWrapper(
            teacher_build_fn=teacher_build_fn,
            teacher_ckpt=teacher_ckpt,
            hook_names=teacher_hook_names,
            device=self.device,
        )
        self.align = FeatureAlign()

    # -- adapter 参数（委托给 FeatureAlign）--
    def kd_parameters(self):
        """返回 adapter 参数 list，供调用方合入 student 优化器。"""
        return self.align.kd_parameters()

    # -- student 侧 hook 注册（每次 __call__ 一次性挂上，跑完即摘）--
    def _register_student_hooks(
        self, student: nn.Module, buf: List["torch.Tensor | None"]
    ):
        named = dict(student.named_modules())
        handles = []
        for idx, name in enumerate(self.student_hook_names):
            module = named.get(name)
            if module is None:
                raise ValueError(
                    f"student 模块 '{name}' 未找到（student_hook_names"
                    f" 必须在 student.named_modules() 中存在）。可用: "
                    f"{[k for k in named.keys() if k]}"
                )

            def _hook(_module, _inputs, output, _idx=idx):
                buf[_idx] = output

            handles.append(module.register_forward_hook(_hook))
        return handles

    # -- 主流程 --
    def __call__(
        self,
        student: nn.Module,
        x: torch.Tensor,
        task_loss_fn: Callable[[torch.Tensor, torch.Tensor], torch.Tensor],
        y: torch.Tensor,
    ) -> torch.Tensor:
        """一次 KD 前向 + loss 组装。返回标量 loss，梯度只流给 student + adapter。"""
        # === 1. student 前向（train 模式，挂一次性 hook 收 s_feats）===
        student.train()
        s_feats_buf: List["torch.Tensor | None"] = [None] * len(
            self.student_hook_names
        )
        handles = self._register_student_hooks(student, s_feats_buf)
        try:
            s_out = student(x)
        finally:
            # 跑完即摘，避免重复挂 hook 或污染 student 后续的非 KD 前向。
            for h in handles:
                h.remove()
        s_feats = list(s_feats_buf)
        missing = [
            self.student_hook_names[i] for i, f in enumerate(s_feats) if f is None
        ]
        if missing:
            raise RuntimeError(
                f"student forward 未触发以下 hook: {missing}"
            )

        # === 2. teacher no_grad 前向 ===
        t_out, t_feats = self.teacher_wrapper(x)

        # 输出级 MSE 要求同形；不挡会 silent broadcast 给出错误 loss。
        if s_out.shape != t_out.shape:
            raise ValueError(
                f"student/teacher 输出 shape 不一致: s_out {tuple(s_out.shape)}"
                f" vs t_out {tuple(t_out.shape)}（输出级蒸馏需同形）"
            )

        # === 3. 首次调用建 adapter（懒建，shape 此刻才确定）===
        if not self.align.is_built():
            self.align.prepare(s_feats, t_feats)
            # 把新建的 adapter 参数搬到 device（s_feats/t_feats 都在 device 上）。
            self.align.to(self.device)
        elif len(s_feats) != len(self.align.adapters):  # type: ignore[arg-type]
            raise RuntimeError(
                "feature 数与已建 adapter 数不一致（student 结构中途变了？）"
            )

        # === 4. 组装 loss ===
        # task_loss（对真值）+ 输出级蒸馏 + 特征级蒸馏
        aligned_s = self.align(s_feats)
        # t_feats / t_out 来自 no_grad 前向，本无 grad_fn；.detach() 是意图标记。
        feat_loss = sum(
            F.mse_loss(a, t.detach()) for a, t in zip(aligned_s, t_feats)
        )
        out_loss = F.mse_loss(s_out, t_out.detach())
        task_loss = task_loss_fn(s_out, y)
        total = task_loss + self.alpha_out * out_loss + self.beta_feat * feat_loss
        return total


# ---------------------------------------------------------------------------
# smoke：tiny Conv1d 自编码器 teacher / 更窄 student，自验 KD 机制
# ---------------------------------------------------------------------------
def _smoke() -> None:
    torch.manual_seed(0)

    class TinyTeacher(nn.Module):
        """2 层 Conv1d 自编码器，[B,4,48,64,1] → 同形。"""

        def __init__(self):
            super().__init__()
            self.enc = nn.Conv1d(4, 16, kernel_size=3, padding=1)  # 通道 4→16
            self.dec = nn.Conv1d(16, 4, kernel_size=3, padding=1)  # 通道 16→4

        def forward(self, x):
            B = x.shape[0]
            h = x.reshape(B, 4, -1)           # [B,4,48*64*1=3072]
            h = torch.relu(self.enc(h))       # [B,16,3072]  ← hook "enc"
            h = torch.relu(self.dec(h))       # [B,4,3072]   ← hook "dec"
            return h.reshape(B, 4, 48, 64, 1)

        def feature_hook_names(self):
            return ["enc", "dec"]

    class TinyStudent(nn.Module):
        """更窄的 student：enc 通道 8（teacher 是 16），触发 Conv1d adapter。"""

        def __init__(self):
            super().__init__()
            self.enc = nn.Conv1d(4, 8, kernel_size=3, padding=1)
            self.dec = nn.Conv1d(8, 4, kernel_size=3, padding=1)

        def forward(self, x):
            B = x.shape[0]
            h = x.reshape(B, 4, -1)
            h = torch.relu(self.enc(h))       # [B,8,3072]
            h = torch.relu(self.dec(h))       # [B,4,3072]
            return h.reshape(B, 4, 48, 64, 1)

        def feature_hook_names(self):
            return ["enc", "dec"]

    # ---- 存 teacher ckpt ----
    import os
    import tempfile

    teacher_ckpt = tempfile.NamedTemporaryFile(
        suffix=".pt", delete=False
    ).name
    torch.save(TinyTeacher().state_dict(), teacher_ckpt)

    try:
        x = torch.randn(2, 4, 48, 64, 1)
        y = torch.randn(2, 4, 48, 64, 1)
        student = TinyStudent()

        kd = KDHelper(
            teacher_build_fn=TinyTeacher,
            teacher_ckpt=teacher_ckpt,
            student_hook_names=["enc", "dec"],
            device="cpu",
            alpha_out=1.0,
            beta_feat=0.5,
        )

        loss = kd(student, x, F.mse_loss, y)
        loss.backward()

        # ---- 断言 ----
        assert loss.dim() == 0, f"loss 非标量: shape {tuple(loss.shape)}"
        assert torch.isfinite(loss).item(), f"loss 非有限: {loss.item()}"

        student_with_grad = [
            n for n, p in student.named_parameters() if p.grad is not None
        ]
        assert student_with_grad, "student 参数无 grad（KD 梯度未回流）"

        teacher_with_grad = [
            n
            for n, p in kd.teacher_wrapper.teacher.named_parameters()
            if p.grad is not None
        ]
        assert not teacher_with_grad, (
            f"teacher 参数意外拿到 grad: {teacher_with_grad}（teacher 必须完全冻结）"
        )

        assert kd.align.is_built(), "adapter 未建（懒建失败）"
        adapter_desc = [type(a).__name__ for a in kd.align.adapters]  # type: ignore[arg-type]
        # 期望: enc 对 (8 vs 16 通道) 建 Conv1d；dec 对 (4 vs 4 同形) 建 Identity。
        assert adapter_desc == ["Conv1d", "Identity"], (
            f"adapter 类型不符预期: {adapter_desc}"
        )

        kd_param_count = len(kd.kd_parameters())

        print(f"[SMOKE] loss              = {loss.item():.6f}")
        print(f"[SMOKE] student 有 grad 参数数 = {len(student_with_grad)}")
        print(f"[SMOKE] teacher 无 grad        = OK ({len(teacher_with_grad)} 个)")
        print(f"[SMOKE] adapter 类型           = {adapter_desc}")
        print(f"[SMOKE] kd_parameters 数量     = {kd_param_count}")
        print("[SMOKE] PASS")
    finally:
        os.unlink(teacher_ckpt)


if __name__ == "__main__":
    _smoke()
