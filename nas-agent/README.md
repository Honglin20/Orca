# NAS Agent 🚀

A Neural Architecture Search (NAS) toolkit for automated PyTorch supernet design, search, and curation.

Designed for high-performance neural architecture exploration, this project uses AI coding assistants (via skills and subagent workflows) to transform standard model definitions into searchable supernets.

## ✨ Features

- **Skill-Based Workflow**:
  - 🏛️ **pytorch-model-optimizer**: Flattens, optimizes, classifies, and generates supernets from user PyTorch models, with iterative evaluator verification.
  - 🏋️ **supernet-train-script**: Reads the generated supernet and the original training code, decides whether supernet training is viable, generates training scripts when applicable, and completes the summary.
  - 🔍 **nas-search-pipeline**: Generates an online latency estimator, architecture search scripts, and post-search scaffolds.
- **Pre-built Block Library**: A collection of ready-to-use Elastic blocks (Performer, BiFormer, CSWin, Swin, EfficientNet, MobileNetV3, etc.) with metadata-driven selection.

## 📦 Installation

This project requires Python 3.10+. For the code agent, we use `claude code`.

1. **Install System Dependencies**:

   Install `claude` and `jq`. These two are required commands.

   ```bash
   curl -fsSL https://claude.ai/install.sh | bash
   # For jq, use your system's package manager, e.g.:
   sudo apt install jq
   ```

2. **Clone the repository**:

   ```bash
   git clone <repository-url>
   cd nas-agent
   ```

3. **Create a virtual environment and install dependencies**:

   ```bash
   # Create a virtual env
   # use venv
   uv venv .venv
   source .venv/bin/activate
   # Or use conda env
   mamba create -n nas-agent python=3.10 -y
   mamba activate nas-agent

   # install requirements
   uv pip install -r <input-repo>/requirements.txt
   
   # To use nas-agent built-in onnx latency measurement
   # For CPU / NPU
   uv pip install -e ".[cpu]"
   # For Nvidia GPU (current onnx only support cu12.x)
   uv pip install -e ".[gpu]"
   ```

## 🚀 Usage

### Using the NAS Skills

Use Claude to run the NAS skills in three sessions. First, `/pytorch-model-optimizer` prepares the model artifact, applies approved optimizations, generates the NAS supernet, and refines the `SearchSpace`. Then, `/supernet-train-script` generates the supernet training scripts when training is viable. Finally, `/nas-search-pipeline` generates the online latency estimator, architecture search scripts, and a post-search `AGENTS.md` scaffold from the generated supernet artifacts and the original PyTorch project.

Example: Replace the model path, output directory, and project path for your own project.

```bash
# Make sure the python env is activated first!
source .venv/bin/activate
claude "/pytorch-model-optimizer optimize @examples/ViT_Imageclassfication/model.py into path llm_artifacts/swin_example/"

claude "/supernet-train-script Generate supernet training scripts for the supernet under @llm_artifacts/swin_example/ from the PyTorch project at @examples/ViT_Imageclassfication/."

claude "/nas-search-pipeline I have already generated a NAS supernet along with its training scripts and search space under @llm_artifacts/swin_example/ from the PyTorch project at @examples/ViT_Imageclassfication/. Please continue to generate the NAS search pipeline scripts for this supernet."
```

NOTE: Always activate the environment before running `claude`, for example `source .venv/bin/activate` for the `uv` environment or `mamba activate nas-agent` for the `mamba` environment. This ensures Python commands can be executed correctly.

The first skill runs the workflow defined in `.agents/skills/pytorch-model-optimizer/SKILL.md`:

1. Flatten the target PyTorch model and local runtime dependencies into a validated standalone file.
2. Recommend applicable optimization rules and ask for explicit approval before changing model behavior.
3. Apply approved optimizations when optimizations are used.
4. Classify the model against the supported NAS architecture labels.
5. Generate `supernet.py` in the output directory, verified by the `supernet-evaluator` subagent in an iterative repair loop.
6. Inspect and refine the generated `SearchSpace`.
7. Write the initial `supernet_summary.md` and present next steps.

The second skill runs the workflow defined in `.agents/skills/supernet-train-script/SKILL.md`:

1. Load context from `supernet_summary.md` and the user's training code.
2. Generate supernet training scripts (if supernet training is viable).
3. Complete `supernet_summary.md` with training viability, evaluation paradigm, and KD decisions.

The third skill runs the workflow defined in `.agents/skills/nas-search-pipeline/SKILL.md`:

1. Generate the project-specific online latency estimator `latency_estimator.py`.
2. Generate search config, architecture codec, evaluator, and search launcher.
3. Generate a post-search `AGENTS.md` scaffold for architecture selection and retrain/finetune script generation.

`/nas-search-pipeline` requires both the generated `<output_dir>` and the original `<user_project_root>`. It reads the supernet artifacts, `train_supernet.py` when it exists, `supernet_summary.md`, and the original project code to generate search-time latency estimation, search scripts, plus an `AGENTS.md` scaffold under `<output_dir>`. Generating these scripts locally does not require an existing trained checkpoint or precomputed latency data; latency is measured on demand through `latency_estimator.py` during search.

Each skill validates the artifacts it creates before handing off to the next session. The exact generated file set is recorded in `supernet_summary.md` and later reflected in `AGENTS.md`; `train_supernet.py` and `run_train_supernet.sh` are omitted when supernet training is not viable.

Typical generated artifacts include:

```text
<output_dir>/
├── <base_name>_flat.py
├── <base_name>_llm-optimized.py
├── supernet.py
├── inspect_supernet.py
├── supernet_summary.md
├── train_supernet.py              # Only when supernet training is viable
├── run_train_supernet.sh          # Only when supernet training is viable
├── latency_estimator.py
├── search_config.yaml
├── arch_codec.py
├── evaluator.py
├── run_search_supernet.sh
└── AGENTS.md
```

Run the generated launchers on the target hardware. If `run_train_supernet.sh` was generated, run it first to train the supernet checkpoint. Then run `run_search_supernet.sh`; the search process imports `latency_estimator.py` and measures latency online. After search completes, follow the `AGENTS.md` scaffold to select architectures and generate retrain/finetune scripts. Search runtime settings live in `search_config.yaml`; `run_search_supernet.sh` only calls `nas-search --config search_config.yaml`.

To use `AGENTS.md`, first `cd` into `<output_dir>`, create a `CLAUDE.md` symlink pointing to `AGENTS.md`, and start a new `claude` session. `AGENTS.md` guides Claude through running the NAS pipeline, selecting architectures from the Pareto front, and generating retrain/finetune scripts for final weight acquisition.

```bash
cd <output_dir>
ln -s AGENTS.md CLAUDE.md
claude
```

## 📂 Project Structure

```text
.
├── .agents/skills/
│   ├── nas-search-pipeline/      # Online latency estimation, search, and AGENTS.md scaffold generation
│   ├── pytorch-model-optimizer/  # PyTorch optimization and supernet generation
│   └── supernet-train-script/    # Supernet training script generation
├── .claude/agents/               # Subagent definitions (supernet-evaluator, workflow-verifier)
├── nas_agent/                    # Core package
│   ├── blocks/                   # Pre-built Elastic NAS blocks and block metadata
│   ├── cli/                      # nas-search and nas-select-architecture
│   ├── search/                   # Fixed NAS search framework
│   └── train/                    # Shared training utilities for generated workflows
├── pyproject.toml                # Project metadata, dependencies, and CLI entry points
└── README.md
```

## 🛠️ Development

### Add new prebuilt block from github repo

Use this skill in any code agents:

- It will extract the block-level code into `nas_agent/blocks` and update `metadata.json`
- Use the code generated by this skill as a starting point, and then manually review, check, and modify it.

```text
/paper-repo-to-nas-blocks Extract the SwinTransformer Block from https://github.com/microsoft/Swin-Transformer
```

## 📝 License

Internal Project - All Rights Reserved.
