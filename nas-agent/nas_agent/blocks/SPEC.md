# SPECIFICATION

* `primitive_blocks.py`: Stores all elastic primitives and underlying layers (linear, normalization, convolution, embedding, and projection modules).
* `common.py`: Stores standard (non-elastic) modules and helper functions that are shared across blocks.
* `choice_layer.py`: Implements the choice routing logic for layer-level branching.
* `metadata.json`: Block metadata registry. Organizes pre-built blocks by model type (e.g., isotropic_transformer, hierarchical_transformer, cnn) along with their descriptions and searchable fields. The `name` field in each entry must match the block's file name stem (e.g., `"fnet_fourier_mixer"` for `fnet_fourier_mixer.py`).
* **Other files**: Pre-built layer-level Elastic blocks. Each block file's name stem must match its `name` entry in `metadata.json`.
