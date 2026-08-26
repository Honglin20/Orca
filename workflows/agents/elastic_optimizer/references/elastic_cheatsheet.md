# Elastic 原语 API 速查（nas-agent）

> slim agent 专用：只列超网生成必备的原语构造参数 + sample_config 机制 + 数据类字段。
> 实现见 `<nas_agent_root>/nas_agent/blocks/primitive_blocks.py` + `choice_layer.py`。
> 合法超网的完整结构基准见同目录 `supernet_template.py`（速查讲 API，模板讲组装）。

## 核心机制：sample_config

每个 Elastic 层维护 `sample_*` 状态（活跃子网参数），由 `set_sample_config(*, sample_xxx)` 设定：
- 超网（最大尺寸）先构造，`set_sample_config` 切换当前激活的子网切片。
- `get_active_subnet()` 按当前 `sample_*` 切出独立 `nn.Module`（权重深拷贝，可脱离超网单独跑）。
- `elastic_num_params`（property）= 当前激活子网的参数量（int，用于 push_describe 的 C2 表）。
- 约束：所有 `sample_*` 必须 ≤ 对应 `super_*`（`set_sample_config` 内 assert，违例 fail loud）。

## 原语

### ElasticConv2d（`from nas_agent.blocks.primitive_blocks import ElasticConv2d`）
动态 in/out channels、groups、kernel size 的 Conv2d。
```python
ElasticConv2d(
    *,  # 全关键字
    super_in_channels: int,
    super_out_channels: int,
    kernel_size: int,              # 最大 kernel（square）；候选由 candidate_kernel_sizes 给
    stride: int = 1,
    padding: int = 0,              # 通常 = max_kernel // 2（same）；切换 kernel 时自动重算 padding
    dilation: int = 1,
    groups: int = 1,
    bias: bool = True,
    candidate_kernel_sizes: tuple[int, ...] | None = None,  # 默认 (kernel_size,)；如 (3,5)
    use_kernel_transform: bool = True,   # True=小 kernel 用变换矩阵从大 kernel 导（更准）；False=中心裁剪
)
```
```python
.set_sample_config(*, sample_in_channels=None, sample_out_channels=None,
                   sample_groups=None, sample_kernel_size=None)  # 全可选，只传要改的
.get_active_subnet() -> nn.Conv2d   # 或 DepthwiseMultiplierConv2d（depthwise 放大时）
.elastic_num_params -> int
```
- kernel 切换：`sample_kernel_size` 必须在 `candidate_kernel_sizes` 内，否则 ValueError。
- `padding=max_kernel//2` 时，切小 kernel 自动调成 `sample_kernel//2`（保持 same）。

### ElasticLinear（`from nas_agent.blocks.primitive_blocks import ElasticLinear`）
动态 in/out 维度的 Linear（head 用）。
```python
ElasticLinear(*, super_in_dim: int, super_out_dim: int, **kwargs)  # kwargs 透传 nn.Linear（bias 等）
```
```python
.set_sample_config(*, sample_in_dim: int, sample_out_dim: int)   # 两个都必填
.get_active_subnet() -> nn.Linear
.elastic_num_params -> int
```
- 切片取权重前 N 行/列：`weight[:sample_out_dim, :sample_in_dim]`。

### ElasticBatchNorm2d（`from nas_agent.blocks.primitive_blocks import ElasticBatchNorm2d`）
动态 feature 数的 BatchNorm2d（跟随 conv 的 out channels）。
```python
ElasticBatchNorm2d(*, super_num_features: int, eps=1e-5, momentum=0.1,
                   affine=True, track_running_stats=True)
```
```python
.set_sample_config(*, sample_num_features: int)   # = 跟随的 conv 的 sample_out_channels
.get_active_subnet() -> nn.BatchNorm2d
.elastic_num_params -> int    # affine 时 = 2 * sample_num_features
```
- `forward` 按**输入实际 feature_dim**切片 running stats/affine（运行时弹性，不报错）。

### ChoiceLayer（`from nas_agent.blocks.choice_layer import ChoiceLayer`）
多分支选择：每个位置提供 ≥1 个 block 候选，搜索时选其一。
```python
ChoiceLayer(*, branches: dict[str, nn.Module])   # 至少 1 个分支；空 → ValueError
```
```python
.set_sample_config(*, choice_name: str, **choice_kwargs)  # 选分支 + 透传给该分支的 set_sample_config
.forward(*args, **kwargs)             # 跑当前选中分支
.get_active_subnet() -> nn.Module     # 当前选中分支的 active subnet
.elastic_num_params -> int            # 当前选中分支的参数量
```
- `choice_name` 必须是 `branches` 的 key。

## 数据类（超网契约 —— nas-search 消费的就是这两个）

### ArchConfig
```python
@dataclass
class ArchConfig:
    stage_depths: tuple[int, ...]                         # 每 stage 激活深度
    layer_configs: dict[str, tuple[dict, ...]]            # stage_name → 每位置 {choice, config}
    def validate(self) -> bool                            # 长度/choice/config 自洽
```
- `layer_configs[stage_name]` 长度必须 = 对应 `stage_depths[i]`。
- 每个元素 `{"choice": <block_name>, "config": {<param>: <value>, ...}}`。

### SearchSpace
```python
@dataclass
class SearchSpace:
    stage_names: tuple[str, ...]                          # 如 ("stage0","stage1","stage2")
    stage_widths: tuple[int, ...]                         # 每 stage 输出通道（固定，跨 stage 对齐）
    stage_depth_candidates: tuple[tuple[int, ...], ...]   # 每 stage 深度候选，如 ((1,2),(1,2))
    stage_layer_configs: tuple[dict[str, dict[str, tuple]], ...]  # 每 stage：{block_name: {param: candidates}}
    def sample(self) -> ArchConfig                        # 随机采一个 ArchConfig
    def validate(self) -> bool                            # 长度自洽 + 每 block_name 的 config 合法
```
- `stage_layer_configs[i]` 形如 `{"conv": {"kernel_size": (3,5)}, "res": {"kernel_size": (3,5), "hidden_channels": (16,32)}}`。
- 每个 block_name 需有对应 `is_valid_<block_name>_block(config) -> bool`，`ArchConfig.validate` / `SearchSpace.validate` 据此校验。

## 现成可复用 block（少写代码）

`from nas_agent.blocks.res_conv import ElasticResConvBlock, is_valid_res_conv_block` —— 2-conv 残差块，
kernel + hidden_channels 弹性：
```python
ElasticResConvBlock(in_channels=..., out_channels=..., stride=...,
                    super_hidden_channels=..., candidate_kernel_sizes=(3,5))
# config 字段：kernel_size（奇数>0）、hidden_channels（>0）
```
`from nas_agent.blocks.depthwise_separable_conv import ElasticDepthwiseSeparableConvBlock, is_valid_depthwise_separable_conv_block`
—— depthwise separable，kernel + expand_channels 弹性。

> 自定义 block 须实现：`__init__`、`set_sample_config(*, sample_xxx)`、`forward`、`get_active_subnet() -> nn.Module`、`elastic_num_params`（property）、配套 `is_valid_<name>_block(config) -> bool`。详见模板里 `ElasticConvBlock` 示例。

## SuperNet 组装要点（模板即基准）

1. `stem`：普通 `nn.Conv2d`（3 → stage0 宽度）。
2. `layers`：`nn.ModuleList`，每 stage 一个 `nn.ModuleList`，每个位置一个 `ChoiceLayer(branches={...})`，按 `max(stage_depth_candidates)` 建满，`stride=2` 仅首位（下采样）。
3. `head`：`ElasticLinear(super_in_dim=最后 stage 宽度, super_out_dim=num_classes)`。
4. `set_sample_config(arch_config)`：遍历激活 stage_depths，逐位置调 `ChoiceLayer.set_sample_config(choice_name=..., sample_<param>=...)`；`config` 字段前缀加 `sample_`。
5. `forward`：按 `_active_arch_config.stage_depths` 只跑激活位置。
6. `get_active_subnet()`：返回独立 `nn.Module`（deepcopy stem/head + 各位置 active subnet）；与超网前向输出一致性 < 1e-5（`python supernet.py` 自测断言）。

## push_describe.py 对 supernet.py 的读取契约（C2 表）

- `dataclasses.asdict(SearchSpace())` 取 `stage_names`/`stage_widths`/`stage_depth_candidates`/`stage_layer_configs` → 渲染每 stage 一行。
- `SuperNet(SearchSpace()).parameters()` 算总参数量。
- 所以 `SearchSpace` 必须是 `@dataclass` 且字段名如上（push_describe 按这些 key 取）。
